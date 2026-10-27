import sys

import pytest

from src import cli


class TestArgv:
    def test_no_args_means_run(self):
        assert cli._normalize_argv([]) == ["run"]

    def test_legacy_config_flag_means_run(self):
        # systemd unit: `python -m src.main --config /path` (pre-subcommand form)
        assert cli._normalize_argv(["--config", "/x.yaml"]) == ["run", "--config", "/x.yaml"]

    def test_subcommand_untouched(self):
        assert cli._normalize_argv(["status", "--json"]) == ["status", "--json"]

    def test_parser_accepts_run_flags(self):
        args = cli.build_parser().parse_args(["run", "--dummy-printer", "--server-url", "http://x", "--subnet-scan"])
        assert args.command == "run"
        assert args.dummy_printer is True
        assert cli._overrides(args) == {
            "server_url": "http://x",
            "dummy_printer": True,
            "discovery": {"subnet_scan_on_schedule": True},
        }


@pytest.mark.skipif(sys.platform.startswith("win"), reason="Linux/macOS behaviour of the Windows-only commands")
class TestWindowsOnlyCommands:
    @pytest.mark.parametrize("command", ["install-service", "remove-service", "service"])
    def test_explain_and_exit_2(self, command, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main([command])
        assert exc.value.code == 2
        assert "only available on Windows" in capsys.readouterr().err


class TestStatus:
    def test_unreachable_agent(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["status", "--port", "1"])
        assert exc.value.code == 1
        assert "not reachable" in capsys.readouterr().out

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.main(["--version"])
        assert exc.value.code == 0
        assert "openeos-printer-agent 0.2.1" in capsys.readouterr().out
