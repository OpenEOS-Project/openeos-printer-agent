"""Start, stop and query the background service from a normal user session.

Used by the CLI (``start-service`` / ``stop-service`` / ``restart-service``)
and by the desktop window. Two implementations:

* Windows: the service control manager via pywin32, asking only for the
  rights the action needs (query/start/stop). The installer grants
  "Interactive Users" start/stop on this one service (see
  ``windows_service.allow_users_to_control``), so the window can restart it
  without a UAC prompt. Without that grant the caller gets
  ``ServiceControlError(permission_denied=True)`` and can elevate.
* Linux: ``systemctl`` on the ``openeos-printer`` unit. systemd asks polkit,
  which shows the desktop's password dialog when the user is not allowed.

Nothing in here imports pywin32 at module level, so the module is safe to
import (and test) everywhere.
"""

import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Protocol

WINDOWS_SERVICE_NAME = "OpenEOSPrinterAgent"
SYSTEMD_UNIT = "openeos-printer"

RUNNING = "running"
STOPPED = "stopped"
STARTING = "starting"
STOPPING = "stopping"
NOT_INSTALLED = "not_installed"
UNKNOWN = "unknown"

ACTIONS = ("start", "stop", "restart")

# winerror codes
ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062

# Service access rights (winsvc.h)
SC_MANAGER_CONNECT = 0x0001
SERVICE_QUERY_CONFIG = 0x0001
SERVICE_QUERY_STATUS = 0x0004
SERVICE_START = 0x0010
SERVICE_STOP = 0x0020
SERVICE_CONTROL_STOP = 0x00000001

_WIN_STATES = {1: STOPPED, 2: STARTING, 3: STOPPING, 4: RUNNING, 5: STARTING, 6: STOPPING, 7: STOPPED}


class ServiceControlError(Exception):
    def __init__(self, message: str, permission_denied: bool = False) -> None:
        super().__init__(message)
        self.permission_denied = permission_denied


class ServiceController(Protocol):
    kind: str

    def state(self) -> str: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def restart(self) -> None: ...


class WindowsServiceController:
    kind = "windows"

    def __init__(
        self,
        name: str = WINDOWS_SERVICE_NAME,
        win32service: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout_s: float = 30.0,
    ) -> None:
        if win32service is None:
            import win32service as _win32service

            win32service = _win32service
        self._ws = win32service
        self._name = name
        self._sleep = sleep
        self._timeout = timeout_s

    def _open(self, access: int) -> tuple[Any, Any]:
        hscm = self._ws.OpenSCManager(None, None, SC_MANAGER_CONNECT)
        try:
            hs = self._ws.OpenService(hscm, self._name, access)
        except Exception:
            self._ws.CloseServiceHandle(hscm)
            raise
        return hscm, hs

    def _close(self, hscm: Any, hs: Any) -> None:
        self._ws.CloseServiceHandle(hs)
        self._ws.CloseServiceHandle(hscm)

    def _query(self, hs: Any) -> str:
        status = self._ws.QueryServiceStatus(hs)
        return _WIN_STATES.get(int(status[1]), UNKNOWN)

    def state(self) -> str:
        try:
            hscm, hs = self._open(SERVICE_QUERY_STATUS)
        except Exception as e:
            if _winerror(e) == ERROR_SERVICE_DOES_NOT_EXIST:
                return NOT_INSTALLED
            return UNKNOWN
        try:
            return self._query(hs)
        except Exception:
            return UNKNOWN
        finally:
            self._close(hscm, hs)

    def _wait_for(self, hs: Any, target: str) -> None:
        deadline = time.monotonic() + self._timeout
        while self._query(hs) != target:
            if time.monotonic() >= deadline:
                raise ServiceControlError(f"Service did not reach state '{target}' in time")
            self._sleep(0.5)

    def _run(self, access: int, action: Callable[[Any], None]) -> None:
        try:
            hscm, hs = self._open(access)
        except Exception as e:
            raise _translate(e) from e
        try:
            action(hs)
        except ServiceControlError:
            raise
        except Exception as e:
            raise _translate(e) from e
        finally:
            self._close(hscm, hs)

    def start(self) -> None:
        def do(hs: Any) -> None:
            try:
                self._ws.StartService(hs, None)
            except Exception as e:
                if _winerror(e) != ERROR_SERVICE_ALREADY_RUNNING:
                    raise
            self._wait_for(hs, RUNNING)

        self._run(SERVICE_START | SERVICE_QUERY_STATUS, do)

    def stop(self) -> None:
        def do(hs: Any) -> None:
            try:
                self._ws.ControlService(hs, SERVICE_CONTROL_STOP)
            except Exception as e:
                if _winerror(e) != ERROR_SERVICE_NOT_ACTIVE:
                    raise
            self._wait_for(hs, STOPPED)

        self._run(SERVICE_STOP | SERVICE_QUERY_STATUS, do)

    def restart(self) -> None:
        if self.state() == RUNNING:
            self.stop()
        self.start()

    def executable(self) -> Optional[str]:
        """Path of the agent exe the service runs (for an elevated retry)."""
        try:
            hscm, hs = self._open(SERVICE_QUERY_CONFIG)
        except Exception:
            return None
        try:
            config = self._ws.QueryServiceConfig(hs)
            return split_command_line(str(config[3]))
        except Exception:
            return None
        finally:
            self._close(hscm, hs)


class SystemdServiceController:
    kind = "systemd"

    def __init__(
        self,
        unit: str = SYSTEMD_UNIT,
        run: Callable[..., "subprocess.CompletedProcess[str]"] = subprocess.run,
        systemctl: Optional[str] = None,
    ) -> None:
        self._unit = unit
        self._run_cmd = run
        self._systemctl = systemctl or shutil.which("systemctl") or "systemctl"

    def state(self) -> str:
        try:
            proc = self._run_cmd(
                [self._systemctl, "show", "--property=LoadState,ActiveState", self._unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return UNKNOWN
        props = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return NOT_INSTALLED
        return {
            "active": RUNNING,
            "reloading": RUNNING,
            "inactive": STOPPED,
            "failed": STOPPED,
            "activating": STARTING,
            "deactivating": STOPPING,
        }.get(props.get("ActiveState", ""), UNKNOWN)

    def _action(self, action: str) -> None:
        try:
            proc = self._run_cmd(
                [self._systemctl, action, self._unit],
                capture_output=True,
                text=True,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise ServiceControlError(str(e)) from e
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip() or f"systemctl {action} failed"
            denied = any(
                s in message.lower() for s in ("access denied", "interactive authentication required", "not authorized")
            )
            raise ServiceControlError(message, permission_denied=denied)

    def start(self) -> None:
        self._action("start")

    def stop(self) -> None:
        self._action("stop")

    def restart(self) -> None:
        self._action("restart")


def default_controller() -> Optional[ServiceController]:
    if sys.platform.startswith("win"):
        try:
            return WindowsServiceController()
        except Exception:
            return None
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        return SystemdServiceController()
    return None


def run_action(controller: ServiceController, action: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    getattr(controller, action)()


def split_command_line(command: str) -> str:
    """First token of a Windows command line ("C:\\a b\\x.exe" service -> C:\\a b\\x.exe)."""
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end != -1 else command[1:]
    return command.split(" ", 1)[0]


def _winerror(e: BaseException) -> Optional[int]:
    code = getattr(e, "winerror", None)
    if code is None and e.args and isinstance(e.args[0], int):
        code = e.args[0]
    return code


def _translate(e: BaseException) -> ServiceControlError:
    code = _winerror(e)
    if code == ERROR_ACCESS_DENIED:
        return ServiceControlError("Access denied", permission_denied=True)
    if code == ERROR_SERVICE_DOES_NOT_EXIST:
        return ServiceControlError("Service is not installed")
    return ServiceControlError(str(e))
