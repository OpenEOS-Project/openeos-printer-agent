from pathlib import Path

from src import paths
from src.version import __version__, get_version


class TestPaths:
    def test_env_override(self, tmp_path):
        # conftest sets the env vars to tmp_path/data and tmp_path/logs
        assert paths.default_data_dir() == tmp_path / "data"
        assert paths.default_log_dir() == tmp_path / "logs"

    def test_windows_layout(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENEOS_PRINTER_DATA_DIR")
        monkeypatch.delenv("OPENEOS_PRINTER_LOG_DIR")
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.setenv("ProgramData", str(tmp_path / "ProgramData"))
        assert paths.default_data_dir() == tmp_path / "ProgramData" / "OpenEOS Printer Agent"
        assert paths.default_log_dir() == tmp_path / "ProgramData" / "OpenEOS Printer Agent" / "logs"
        search = paths.default_config_search_paths()
        assert search[0] == tmp_path / "ProgramData" / "OpenEOS Printer Agent" / "config.yaml"
        assert all("/etc/" not in str(p) for p in search)

    def test_linux_falls_back_to_user_dir_when_var_lib_unwritable(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENEOS_PRINTER_DATA_DIR")
        monkeypatch.setattr(paths.sys, "platform", "linux")
        monkeypatch.setattr(paths, "_writable", lambda p: False)
        monkeypatch.setattr(paths.platformdirs, "user_data_dir", lambda name: str(tmp_path / "user" / name))
        assert paths.default_data_dir() == tmp_path / "user" / "openeos-printer"

    def test_resource_dir_contains_templates(self):
        assert (paths.resource_dir() / "templates" / "receipt.jinja2").exists()
        assert Path(paths.resource_dir()).name == "src"

    def test_version(self):
        assert __version__ == "0.2.0"
        assert get_version() == "0.2.0"
