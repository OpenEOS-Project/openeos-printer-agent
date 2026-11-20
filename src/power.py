"""Keep the computer from going to sleep while the agent is connected.

Windows
    ``SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)``. Works
    from a service (session 0) as well: the request shows up in
    ``powercfg /requests`` under SYSTEM and stops the *idle* sleep timer.
    ``ES_DISPLAY_REQUIRED`` is deliberately not set - the screen may turn
    off. The state belongs to the calling thread and is dropped when that
    thread ends, so acquire and release must happen on the same thread
    (the agent calls both from its event loop thread). What it does not do:
    stop sleep the user asks for - closing the lid (if the power options say
    "sleep"), the power button, Start > Sleep - and on Modern Standby
    (S0ix) laptops Windows may still enter standby once the display is off.

Linux
    A logind ``sleep`` inhibitor in ``block`` mode via ``systemd-inhibit``,
    held by a child process for as long as we are connected. Best effort:
    without systemd-logind, or if polkit refuses, nothing happens apart from
    a log line. logind ignores inhibitors for the lid switch by default
    (``LidSwitchIgnoreInhibited=yes``), so closing the lid still suspends.
"""

import logging
import shutil
import subprocess
import sys
import threading
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

WHO = "OpenEOS Printer Agent"
WHY = "Verbunden mit OpenEOS - wartet auf Druckauftraege"


class InhibitBackend(Protocol):
    name: str

    def acquire(self) -> bool: ...

    def release(self) -> None: ...

    def alive(self) -> bool: ...


class WindowsExecutionState:
    """``SetThreadExecutionState`` wrapper. ``kernel32`` is injectable for tests."""

    name = "windows"

    def __init__(self, kernel32: Any = None) -> None:
        if kernel32 is None:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            kernel32.SetThreadExecutionState.restype = ctypes.c_uint32
            kernel32.SetThreadExecutionState.argtypes = [ctypes.c_uint32]
        self._kernel32 = kernel32
        self._thread: Optional[int] = None

    def acquire(self) -> bool:
        previous = self._kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        if not previous:  # 0 = failure
            return False
        self._thread = threading.get_ident()
        return True

    def release(self) -> None:
        if self._thread is None:
            return
        if self._thread != threading.get_ident():
            # Would clear nothing - the flag sits on the other thread.
            logger.warning("Keep-awake released from a different thread than it was set on")
        self._kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        self._thread = None

    def alive(self) -> bool:
        return self._thread is not None


class SystemdInhibit:
    """Holds ``systemd-inhibit --what=sleep --mode=block sleep infinity``."""

    name = "systemd-inhibit"

    def __init__(self, executable: Optional[str] = None, popen: Any = None) -> None:
        self._exe = executable or shutil.which("systemd-inhibit")
        self._popen = popen or subprocess.Popen
        self._proc: Any = None

    @property
    def available(self) -> bool:
        return bool(self._exe)

    def acquire(self) -> bool:
        if not self._exe:
            return False
        try:
            self._proc = self._popen(
                [
                    self._exe,
                    "--what=sleep",
                    f"--who={WHO}",
                    f"--why={WHY}",
                    "--mode=block",
                    "sleep",
                    "infinity",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as e:
            logger.warning(f"systemd-inhibit could not be started: {e}")
            self._proc = None
            return False
        return True

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def alive(self) -> bool:
        """False once the helper exited (polkit said no, logind missing)."""
        return self._proc is not None and self._proc.poll() is None


def default_backend() -> Optional[InhibitBackend]:
    if sys.platform.startswith("win"):
        try:
            return WindowsExecutionState()
        except Exception as e:  # pragma: no cover - Windows only
            logger.warning(f"Keep-awake unavailable: {e}")
            return None
    if sys.platform.startswith("linux"):
        inhibit = SystemdInhibit()
        return inhibit if inhibit.available else None
    return None


def keep_awake_from_config(enabled: bool) -> "KeepAwake":
    return KeepAwake(enabled, default_backend() if enabled else None)


class KeepAwake:
    """Holds the sleep inhibitor exactly while ``update(True)`` says so.

    ``update`` is cheap and idempotent - the agent calls it every few
    seconds with "connected to the server?". A backend that fails (or whose
    helper process dies) is not retried in a tight loop: one warning, then a
    new attempt only after the connection state changed.
    """

    def __init__(self, enabled: bool, backend: Optional[InhibitBackend]) -> None:
        self._enabled = enabled
        self._backend = backend
        self._active = False
        self._error: Optional[str] = None
        self._failed_while_connected = False
        self._last_wanted: Optional[bool] = None

    @property
    def active(self) -> bool:
        return self._active

    def update(self, connected: bool) -> None:
        wanted = self._enabled and connected and self._backend is not None
        if wanted != self._last_wanted:
            self._failed_while_connected = False
        self._last_wanted = wanted

        if self._active and self._backend is not None and not self._backend.alive():
            self._active = False
            self._error = f"{self._backend.name}: inhibitor ended unexpectedly"
            logger.warning(f"Keep-awake stopped working ({self._error}); sleep is no longer blocked")
            self._failed_while_connected = True

        if wanted and not self._active and not self._failed_while_connected:
            assert self._backend is not None
            if self._backend.acquire():
                self._active = True
                self._error = None
                logger.info(f"Keep-awake on ({self._backend.name}): the computer stays awake while connected")
            else:
                self._error = f"{self._backend.name}: request refused"
                self._failed_while_connected = True
                logger.warning(f"Keep-awake could not be enabled ({self._error})")
        elif not wanted and self._active:
            self.release()

    def release(self) -> None:
        if self._active and self._backend is not None:
            self._backend.release()
            logger.info("Keep-awake off: the computer may sleep again")
        self._active = False

    def status_dict(self) -> dict[str, Any]:
        return {
            "keepAwake": self._enabled,
            "active": self._active,
            "backend": self._backend.name if self._backend is not None else None,
            "error": self._error,
        }
