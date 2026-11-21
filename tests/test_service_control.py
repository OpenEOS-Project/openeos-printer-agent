"""Service control for the desktop window and the *-service CLI commands."""

import subprocess
from types import SimpleNamespace

import pytest

from src import cli
from src import service_control as sc


class WinErr(Exception):
    def __init__(self, code: int):
        super().__init__(code, "fn", "msg")
        self.winerror = code


class FakeWin32Service:
    """Minimal SCM: one service with a state; records requested access."""

    def __init__(self, state: int = 1, exists: bool = True, deny: int = 0):
        self.state = state
        self.exists = exists
        self.deny = deny  # access mask bits that raise ERROR_ACCESS_DENIED
        self.opened: list[int] = []
        self.closed = 0

    def OpenSCManager(self, machine, db, access):
        assert access == sc.SC_MANAGER_CONNECT  # never asks for more
        return "scm"

    def OpenService(self, hscm, name, access):
        if not self.exists:
            raise WinErr(sc.ERROR_SERVICE_DOES_NOT_EXIST)
        if access & self.deny:
            raise WinErr(sc.ERROR_ACCESS_DENIED)
        self.opened.append(access)
        return "svc"

    def CloseServiceHandle(self, h):
        self.closed += 1

    def QueryServiceStatus(self, hs):
        return (16, self.state, 0, 0, 0, 0, 0)

    def StartService(self, hs, args):
        if self.state == 4:
            raise WinErr(sc.ERROR_SERVICE_ALREADY_RUNNING)
        self.state = 4

    def ControlService(self, hs, code):
        assert code == sc.SERVICE_CONTROL_STOP
        if self.state == 1:
            raise WinErr(sc.ERROR_SERVICE_NOT_ACTIVE)
        self.state = 1

    def QueryServiceConfig(self, hs):
        return (16, 2, 1, r'"C:\Program Files\OpenEOS Printer Agent\openeos-printer-agent.exe" service', "", 0, [], "")


def _win(**kw):
    return sc.WindowsServiceController(win32service=FakeWin32Service(**kw), sleep=lambda s: None, timeout_s=1)


class TestWindows:
    def test_state(self):
        assert _win(state=4).state() == sc.RUNNING
        assert _win(state=1).state() == sc.STOPPED
        assert _win(state=2).state() == sc.STARTING
        assert _win(exists=False).state() == sc.NOT_INSTALLED

    def test_start_stop_restart_minimal_rights(self):
        ctl = _win(state=1)
        ctl.start()
        assert ctl.state() == sc.RUNNING
        ctl.stop()
        assert ctl.state() == sc.STOPPED
        ctl.start()
        ctl.restart()
        assert ctl.state() == sc.RUNNING
        ws = ctl._ws
        assert all(a & ~(sc.SERVICE_START | sc.SERVICE_STOP | sc.SERVICE_QUERY_STATUS) == 0 for a in ws.opened)
        assert ws.closed == 2 * len(ws.opened)

    def test_start_when_running_is_fine(self):
        ctl = _win(state=4)
        ctl.start()
        assert ctl.state() == sc.RUNNING

    def test_access_denied(self):
        ctl = _win(state=4, deny=sc.SERVICE_STOP)
        with pytest.raises(sc.ServiceControlError) as exc:
            ctl.stop()
        assert exc.value.permission_denied

    def test_timeout(self):
        ws = FakeWin32Service(state=1)
        ws.StartService = lambda hs, args: None  # never reaches RUNNING
        ctl = sc.WindowsServiceController(win32service=ws, sleep=lambda s: None, timeout_s=0)
        with pytest.raises(sc.ServiceControlError):
            ctl.start()

    def test_executable(self):
        assert _win().executable() == r"C:\Program Files\OpenEOS Printer Agent\openeos-printer-agent.exe"

    @pytest.mark.parametrize(
        "cmd,exe",
        [
            (r'"C:\a b\x.exe" service', r"C:\a b\x.exe"),
            (r"C:\x.exe service", r"C:\x.exe"),
            (r"C:\x.exe", r"C:\x.exe"),
        ],
    )
    def test_split_command_line(self, cmd, exe):
        assert sc.split_command_line(cmd) == exe


class FakeRun:
    def __init__(self, show: str = "LoadState=loaded\nActiveState=active\n", rc: int = 0, err: str = ""):
        self.show = show
        self.rc = rc
        self.err = err
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        if argv[1] == "show":
            return subprocess.CompletedProcess(argv, 0, stdout=self.show, stderr="")
        return subprocess.CompletedProcess(argv, self.rc, stdout="", stderr=self.err)


class TestSystemd:
    @pytest.mark.parametrize(
        "show,state",
        [
            ("LoadState=loaded\nActiveState=active\n", sc.RUNNING),
            ("LoadState=loaded\nActiveState=inactive\n", sc.STOPPED),
            ("LoadState=loaded\nActiveState=failed\n", sc.STOPPED),
            ("LoadState=loaded\nActiveState=activating\n", sc.STARTING),
            ("LoadState=not-found\nActiveState=inactive\n", sc.NOT_INSTALLED),
            ("", sc.UNKNOWN),
        ],
    )
    def test_state(self, show, state):
        assert sc.SystemdServiceController(run=FakeRun(show), systemctl="systemctl").state() == state

    def test_actions(self):
        run = FakeRun()
        ctl = sc.SystemdServiceController(run=run, systemctl="systemctl")
        ctl.restart()
        ctl.stop()
        ctl.start()
        assert [c[1] for c in run.calls] == ["restart", "stop", "start"]
        assert all(c[2] == "openeos-printer" for c in run.calls)

    def test_permission_denied(self):
        run = FakeRun(rc=1, err="Failed to restart openeos-printer.service: Access denied")
        with pytest.raises(sc.ServiceControlError) as exc:
            sc.SystemdServiceController(run=run, systemctl="systemctl").restart()
        assert exc.value.permission_denied

    def test_other_failure(self):
        run = FakeRun(rc=5, err="Unit openeos-printer.service not loaded.")
        with pytest.raises(sc.ServiceControlError) as exc:
            sc.SystemdServiceController(run=run, systemctl="systemctl").start()
        assert not exc.value.permission_denied

    def test_run_action_rejects_unknown(self):
        with pytest.raises(ValueError):
            sc.run_action(sc.SystemdServiceController(run=FakeRun(), systemctl="x"), "kill")


class TestCli:
    def test_restart_service(self, monkeypatch, capsys):
        run = FakeRun()
        monkeypatch.setattr(sc, "default_controller", lambda: sc.SystemdServiceController(run=run, systemctl="sc"))
        assert cli.cmd_control_service(SimpleNamespace(command="restart-service")) == 0
        assert ["sc", "restart", "openeos-printer"] in run.calls

    def test_not_installed(self, monkeypatch):
        run = FakeRun(show="LoadState=not-found\n")
        monkeypatch.setattr(sc, "default_controller", lambda: sc.SystemdServiceController(run=run, systemctl="sc"))
        assert cli.cmd_control_service(SimpleNamespace(command="start-service")) == 2

    def test_denied_exit_code(self, monkeypatch):
        run = FakeRun(rc=1, err="Interactive authentication required.")
        monkeypatch.setattr(sc, "default_controller", lambda: sc.SystemdServiceController(run=run, systemctl="sc"))
        assert cli.cmd_control_service(SimpleNamespace(command="stop-service")) == 3

    def test_parser_knows_commands(self):
        parser = cli.build_parser()
        for name in ("start-service", "stop-service", "restart-service"):
            assert parser.parse_args([name]).command == name
