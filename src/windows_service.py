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

# WM_POWERBROADCAST event types (winuser.h)
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012

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

        def GetAcceptedControls(self) -> int:
            # Ask for power events: after sleep the service is told right
            # away (PBT_APMRESUMEAUTOMATIC) instead of discovering dead
            # sockets on its own.
            return int(super().GetAcceptedControls() | win32service.SERVICE_ACCEPT_POWEREVENT)

        def SvcOtherEx(self, control: int, event_type: int, data: Any) -> None:
            if control == win32service.SERVICE_CONTROL_POWEREVENT and event_type in (
                PBT_APMRESUMEAUTOMATIC,
                PBT_APMRESUMESUSPEND,
            ):
                if self._agent is not None:
                    self._agent.notify_resume_threadsafe("Windows power event")

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
        # The desktop window runs as the logged-in user and offers "restart
        # service" - allow exactly that without an admin prompt.
        allow_users_to_control()
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


# Interactive Users (S-1-5-4): everyone logged on at the console or via RDP.
INTERACTIVE_USERS_SID = "S-1-5-4"
# SERVICE_QUERY_STATUS | SERVICE_START | SERVICE_STOP | SERVICE_INTERROGATE
USER_CONTROL_RIGHTS = 0x0004 | 0x0010 | 0x0020 | 0x0080


def allow_users_to_control(service_name: str = SERVICE_NAME) -> bool:
    """Add one ACE to the service's DACL: Interactive Users may query,
    start and stop it. Changing the configuration, deleting the service or
    the files stays with administrators. Best effort; idempotent."""
    try:
        import win32security  # type: ignore[import-not-found]

        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            access = win32security.READ_CONTROL | win32security.WRITE_DAC  # type: ignore[attr-defined]
            hs = win32service.OpenService(hscm, service_name, access)
            try:
                sd = win32service.QueryServiceObjectSecurity(hs, win32security.DACL_SECURITY_INFORMATION)
                dacl = sd.GetSecurityDescriptorDacl()
                sid = win32security.ConvertStringSidToSid(INTERACTIVE_USERS_SID)
                for i in range(dacl.GetAceCount()):
                    (ace_type, _flags), mask, ace_sid = dacl.GetAce(i)
                    if ace_type == win32security.ACCESS_ALLOWED_ACE_TYPE and ace_sid == sid:
                        if mask & USER_CONTROL_RIGHTS == USER_CONTROL_RIGHTS:
                            return True
                dacl.AddAccessAllowedAce(win32security.ACL_REVISION, USER_CONTROL_RIGHTS, sid)
                sd.SetSecurityDescriptorDacl(1, dacl, 0)
                win32service.SetServiceObjectSecurity(hs, win32security.DACL_SECURITY_INFORMATION, sd)
                return True
            finally:
                win32service.CloseServiceHandle(hs)
        finally:
            win32service.CloseServiceHandle(hscm)
    except Exception as e:
        logger.warning(f"Could not allow users to start/stop the service: {e}")
        print(f"Note: users cannot restart the service without admin rights ({e})", file=sys.stderr)
        return False


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
