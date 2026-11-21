"""Keep-awake wrapper (src/power.py) with mocked OS APIs."""

import subprocess
import threading
from unittest.mock import MagicMock

from src.config import AppConfig, PowerConfig
from src.power import (
    ES_CONTINUOUS,
    ES_SYSTEM_REQUIRED,
    KeepAwake,
    SystemdInhibit,
    WindowsExecutionState,
)


class FakeKernel32:
    def __init__(self, result: int = 0x80000000):
        self.calls: list[tuple[int, int]] = []
        self.result = result

    def SetThreadExecutionState(self, flags: int) -> int:
        self.calls.append((flags, threading.get_ident()))
        return self.result


class FakeBackend:
    name = "fake"

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.held = False
        self.acquires = 0
        self.releases = 0
        self.dead = False

    def acquire(self) -> bool:
        self.acquires += 1
        self.held = self.ok
        return self.ok

    def release(self) -> None:
        self.releases += 1
        self.held = False

    def alive(self) -> bool:
        return self.held and not self.dead


class TestWindowsExecutionState:
    def test_system_required_without_display(self):
        k = FakeKernel32()
        backend = WindowsExecutionState(kernel32=k)
        assert backend.acquire() is True
        flags = k.calls[0][0]
        assert flags == ES_CONTINUOUS | ES_SYSTEM_REQUIRED
        assert flags & 0x2 == 0  # ES_DISPLAY_REQUIRED not set
        assert backend.alive()
        backend.release()
        assert k.calls[1][0] == ES_CONTINUOUS  # clears the continuous request
        assert not backend.alive()

    def test_failure_returns_false(self):
        backend = WindowsExecutionState(kernel32=FakeKernel32(result=0))
        assert backend.acquire() is False
        assert not backend.alive()

    def test_release_without_acquire_is_noop(self):
        k = FakeKernel32()
        WindowsExecutionState(kernel32=k).release()
        assert k.calls == []

    def test_same_thread(self):
        k = FakeKernel32()
        backend = WindowsExecutionState(kernel32=k)
        backend.acquire()
        backend.release()
        assert k.calls[0][1] == k.calls[1][1] == threading.get_ident()


class TestSystemdInhibit:
    def test_command_line(self):
        proc = MagicMock()
        proc.poll.return_value = None
        popen = MagicMock(return_value=proc)
        inhibit = SystemdInhibit(executable="/usr/bin/systemd-inhibit", popen=popen)
        assert inhibit.acquire()
        argv = popen.call_args.args[0]
        assert argv[0] == "/usr/bin/systemd-inhibit"
        assert "--what=sleep" in argv
        assert "--mode=block" in argv
        assert argv[-2:] == ["sleep", "infinity"]
        assert popen.call_args.kwargs["stdin"] is subprocess.DEVNULL
        assert inhibit.alive()
        inhibit.release()
        proc.terminate.assert_called_once()
        assert not inhibit.alive()

    def test_exited_helper_is_not_alive(self):
        proc = MagicMock()
        proc.poll.return_value = 1  # "Failed to inhibit: Access denied"
        inhibit = SystemdInhibit(executable="/x", popen=MagicMock(return_value=proc))
        inhibit.acquire()
        assert not inhibit.alive()

    def test_not_available(self):
        inhibit = SystemdInhibit(executable="", popen=MagicMock())
        inhibit._exe = None
        assert not inhibit.available
        assert inhibit.acquire() is False

    def test_popen_error(self):
        inhibit = SystemdInhibit(executable="/x", popen=MagicMock(side_effect=OSError("nope")))
        assert inhibit.acquire() is False


class TestKeepAwake:
    def test_follows_connection(self):
        b = FakeBackend()
        ka = KeepAwake(True, b)
        ka.update(False)
        assert b.acquires == 0 and not ka.active
        ka.update(True)
        assert ka.active and b.acquires == 1
        ka.update(True)  # idempotent
        assert b.acquires == 1
        ka.update(False)
        assert not ka.active and b.releases == 1
        assert ka.status_dict() == {"keepAwake": True, "active": False, "backend": "fake", "error": None}

    def test_disabled_never_acquires(self):
        b = FakeBackend()
        ka = KeepAwake(False, b)
        ka.update(True)
        assert b.acquires == 0
        assert ka.status_dict()["keepAwake"] is False

    def test_no_backend(self):
        ka = KeepAwake(True, None)
        ka.update(True)
        assert not ka.active
        assert ka.status_dict()["backend"] is None

    def test_refused_is_not_retried_until_state_changes(self):
        b = FakeBackend(ok=False)
        ka = KeepAwake(True, b)
        ka.update(True)
        ka.update(True)
        ka.update(True)
        assert b.acquires == 1
        assert ka.status_dict()["error"] == "fake: request refused"
        ka.update(False)
        b.ok = True
        ka.update(True)
        assert b.acquires == 2 and ka.active
        assert ka.status_dict()["error"] is None

    def test_dead_helper_detected(self):
        b = FakeBackend()
        ka = KeepAwake(True, b)
        ka.update(True)
        b.dead = True
        ka.update(True)
        assert not ka.active
        assert "ended unexpectedly" in ka.status_dict()["error"]
        assert b.acquires == 1  # no tight retry loop

    def test_release_on_shutdown(self):
        b = FakeBackend()
        ka = KeepAwake(True, b)
        ka.update(True)
        ka.release()
        assert b.releases == 1 and not ka.active


class TestConfig:
    def test_default_depends_on_platform(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        assert PowerConfig().keep_awake_enabled is True
        monkeypatch.setattr("sys.platform", "linux")
        assert PowerConfig().keep_awake_enabled is False

    def test_explicit_value_wins(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        assert PowerConfig(keep_awake=False).keep_awake_enabled is False
        monkeypatch.setattr("sys.platform", "linux")
        assert AppConfig(power={"keep_awake": True}).power.keep_awake_enabled is True

    def test_web_app_url(self):
        assert AppConfig(server_url="https://api.openeos.de/api").resolved_web_app_url == "https://app.openeos.de"
        assert (
            AppConfig(server_url="https://api.staging.openeos.de/api").resolved_web_app_url
            == "https://app.staging.openeos.de"
        )
        assert AppConfig(server_url="https://kasse.verein.de/api").resolved_web_app_url == "https://kasse.verein.de"
        assert AppConfig(web_app_url="https://x.example/").resolved_web_app_url == "https://x.example"
