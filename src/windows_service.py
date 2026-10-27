"""Windows service wrapper (pywin32 ``win32serviceutil``).

Only imported on Windows (the CLI guards every call). The installed service
runs ``<exe> service``; the service control manager hands control to
``run_service_dispatcher`` which hosts ``AgentService``. Logs land in
``%ProgramData%\\OpenEOS Printer Agent\\logs\\agent.log`` because a service
has no console - the pairing code is in that file and on the status page.
"""

import asyncio
import ctypes
import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

from .config import load_config, write_minimal_config
from .paths import default_data_dir, is_frozen

logger = logging.getLogger(__name__)

SERVICE_NAME = "OpenEOSPrinterAgent"
SERVICE_DISPLAY_NAME = "OpenEOS Printer Agent"
SERVICE_DESCRIPTION = (
    "Druckt Bons fuer OpenEOS auf USB- und Netzwerkdrucker und meldet gefundene Drucker an die Plattform."
)

try:
    import servicemanager  # type: ignore[import-not-found]
    import win32event  # type: ignore[import-not-found]
    import win32service  # type: ignore[import-not-found]
    import win32serviceutil  # type: ignore[import-not-found]

    _PYWIN32_AVAILABLE = True
except Exception:  # pragma: no cover - only on Windows
    _PYWIN32_AVAILABLE = False


def pywin32_available() -> bool:
    return _PYWIN32_AVAILABLE


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        return False


def service_command_line() -> tuple[str, str]:
    """(exe, args) the SCM should start. Frozen: the exe itself; from a
    venv: python.exe -m src.main."""
    if is_frozen():
        return sys.executable, "service"
    return sys.executable, "-m src.main service"


if _PYWIN32_AVAILABLE:

    class AgentService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY_NAME
        _svc_description_ = SERVICE_DESCRIPTION

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._agent: Any = None

        def SvcStop(self) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            if self._agent is not None:
                self._agent.request_shutdown_threadsafe()
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self) -> None:
            from .main import PrinterAgent

            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            # Services start in %SystemRoot%\System32; relative paths in the
            # config lookup would be meaningless there.
            os.chdir(str(default_data_dir()))
            config = load_config(None)
            self._agent = PrinterAgent(config=config)
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            try:
                asyncio.run(self._agent.run())
            except Exception as e:
                servicemanager.LogErrorMsg(f"OpenEOS Printer Agent stopped with error: {e}")
                raise
            finally:
                servicemanager.LogMsg(
                    servicemanager.EVENTLOG_INFORMATION_TYPE,
                    servicemanager.PYS_SERVICE_STOPPED,
                    (self._svc_name_, ""),
                )


def install_service(config_path: Optional[str], server_url: str) -> int:
    """Create (or update) the service and start it. Requires elevation."""
    if not _PYWIN32_AVAILABLE:
        print("pywin32 is not installed; cannot manage Windows services.", file=sys.stderr)
        return 2
    if not is_admin():
        print("install-service needs Administrator rights (right-click > Run as administrator).", file=sys.stderr)
        return 3

    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)
    config_file = Path(config_path) if config_path else data_dir / "config.yaml"
    if not config_file.exists():
        write_minimal_config(config_file, server_url)
        print(f"Wrote {config_file}")

    exe, args = service_command_line()
    try:
        try:
            win32serviceutil.StopService(SERVICE_NAME)
        except Exception:
            pass
        try:
            win32serviceutil.RemoveService(SERVICE_NAME)
        except Exception:
            pass
        win32serviceutil.InstallService(
            "src.windows_service.AgentService",
            SERVICE_NAME,
            SERVICE_DISPLAY_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=exe,
            exeArgs=args,
            description=SERVICE_DESCRIPTION,
        )
        # Restart automatically after a crash (1 min delay), like systemd's Restart=always.
        _set_failure_actions()
        win32serviceutil.StartService(SERVICE_NAME)
    except Exception as e:
        print(f"Service installation failed: {e}", file=sys.stderr)
        return 1

    print(f"Service '{SERVICE_DISPLAY_NAME}' installed and started.")
    print(f"Logs: {data_dir / 'logs' / 'agent.log'}")
    print("Pairing code: see the log file or http://localhost:8080")
    return 0


def _set_failure_actions() -> None:
    """``sc failure`` equivalent via the API, best effort."""
    try:
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
        try:
            hs = win32service.OpenService(hscm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
            try:
                actions = [(win32service.SC_ACTION_RESTART, 60_000)] * 3
                win32service.ChangeServiceConfig2(
                    hs,
                    win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
                    {"ResetPeriod": 86_400, "RebootMsg": "", "Command": "", "Actions": actions},
                )
            finally:
                win32service.CloseServiceHandle(hs)
        finally:
            win32service.CloseServiceHandle(hscm)
    except Exception as e:
        logger.debug(f"Could not set service failure actions: {e}")


def remove_service() -> int:
    if not _PYWIN32_AVAILABLE:
        print("pywin32 is not installed; cannot manage Windows services.", file=sys.stderr)
        return 2
    if not is_admin():
        print("remove-service needs Administrator rights.", file=sys.stderr)
        return 3
    try:
        try:
            win32serviceutil.StopService(SERVICE_NAME)
        except Exception:
            pass
        win32serviceutil.RemoveService(SERVICE_NAME)
    except Exception as e:
        print(f"Service removal failed: {e}", file=sys.stderr)
        return 1
    print(f"Service '{SERVICE_DISPLAY_NAME}' removed. Data in {default_data_dir()} was kept.")
    return 0


_STATE_NAMES = {
    1: "stopped",
    2: "start pending",
    3: "stop pending",
    4: "running",
    5: "continue pending",
    6: "pause pending",
    7: "paused",
}


def service_state() -> str:
    if not _PYWIN32_AVAILABLE:
        return "pywin32 not installed"
    try:
        status = win32serviceutil.QueryServiceStatus(SERVICE_NAME)
    except Exception:
        return "not installed"
    return _STATE_NAMES.get(int(status[1]), f"state {status[1]}")


def run_service_dispatcher() -> int:
    """Entry used by the SCM (``<exe> service``)."""
    if not _PYWIN32_AVAILABLE:
        print("pywin32 is not installed.", file=sys.stderr)
        return 2
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(AgentService)
    servicemanager.StartServiceCtrlDispatcher()
    return 0
