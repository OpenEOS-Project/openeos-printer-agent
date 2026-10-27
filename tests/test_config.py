import pytest

from src.config import AppConfig, ServerConfig, load_config, write_minimal_config


class TestServerConfig:
    def test_valid_config(self, sample_server_config):
        assert sample_server_config.url == "http://localhost:3000"
        assert sample_server_config.device_token == "test-token-123"
        assert sample_server_config.heartbeat_interval_ms == 30000

    def test_heartbeat_interval_seconds(self, sample_server_config):
        assert sample_server_config.heartbeat_interval == 30.0

    def test_reconnect_interval_seconds(self, sample_server_config):
        assert sample_server_config.reconnect_interval == 5.0

    def test_device_token_optional(self):
        # device_token is Optional[str] = None on ServerConfig — it is filled
        # in later via self-registration, so omitting it must not raise.
        cfg = ServerConfig(url="http://localhost:3000")
        assert cfg.device_token is None


class TestAppConfig:
    def test_flat_server_url_wins(self):
        # The minimal config is just `server_url:`; it must land in server.url
        # (and a trailing slash must not end up doubled in request URLs).
        cfg = AppConfig(server_url="https://api.example.com/api/")
        assert cfg.server.url == "https://api.example.com/api"
        assert cfg.server_url == "https://api.example.com/api/"

    def test_nested_server_block_still_works(self):
        cfg = AppConfig(server=ServerConfig(url="http://nested:3000"))
        assert cfg.server.url == "http://nested:3000"
        assert cfg.server_url == "http://nested:3000"

    def test_flat_device_token(self):
        cfg = AppConfig(server_url="http://x", device_token="tok")
        assert cfg.server.device_token == "tok"

    def test_defaults(self):
        cfg = AppConfig(server_url="http://localhost:3000")
        assert cfg.logging.level == "INFO"
        assert cfg.local_server.enabled is True
        assert cfg.discovery.interval_s == 300
        assert cfg.discovery.subnet_scan_on_schedule is False
        assert cfg.dummy_printer is False
        assert cfg.cash_drawer_pin == 2

    def test_derived_paths_follow_data_dir(self, tmp_path):
        cfg = AppConfig(server_url="http://x", data_dir=str(tmp_path / "d"))
        assert cfg.device_token_path == tmp_path / "d" / "device.json"
        assert cfg.job_store_path == tmp_path / "d" / "print-jobs.db"
        assert cfg.template_cache_path == tmp_path / "d" / "templates-cache"

    def test_explicit_file_overrides(self, tmp_path):
        cfg = AppConfig(
            server_url="http://x",
            device_token_file=str(tmp_path / "t.json"),
            job_store_file=str(tmp_path / "j.db"),
        )
        assert cfg.device_token_path == tmp_path / "t.json"
        assert cfg.job_store_path == tmp_path / "j.db"

    def test_log_file_from_dir_and_from_file(self, tmp_path):
        cfg = AppConfig(server_url="http://x", logging={"dir": str(tmp_path)})
        assert cfg.log_file == tmp_path / "agent.log"
        cfg2 = AppConfig(server_url="http://x", logging={"file": str(tmp_path / "x" / "a.log")})
        assert cfg2.log_file == tmp_path / "x" / "a.log"
        assert cfg2.log_path == tmp_path / "x"

    def test_env_data_dir_default(self, tmp_path):
        # conftest points OPENEOS_PRINTER_DATA_DIR at tmp_path/data
        cfg = AppConfig(server_url="http://x")
        assert cfg.data_path == tmp_path / "data"


class TestLoadConfig:
    def test_minimal_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("server_url: https://api.test/api\n")
        cfg = load_config(str(path))
        assert cfg.server.url == "https://api.test/api"

    def test_legacy_printers_block_is_ignored(self, tmp_path, capsys):
        # Old installs carry a printers list; it must neither crash nor be used.
        path = tmp_path / "config.yaml"
        path.write_text(
            "server:\n  url: http://legacy:3000\n"
            "agent:\n  id: pr01\n  name: X\n"
            "sentry:\n  enabled: false\n"
            "printers:\n  - localId: a\n    name: A\n    connectionType: usb\n"
        )
        cfg = load_config(str(path))
        assert cfg.server.url == "http://legacy:3000"
        assert not hasattr(cfg, "printers")
        assert "printers" in capsys.readouterr().err

    def test_overrides_beat_file(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("server_url: http://file\n")
        cfg = load_config(str(path), {"server_url": "http://flag", "dummy_printer": True})
        assert cfg.server.url == "http://flag"
        assert cfg.dummy_printer is True

    def test_missing_explicit_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nope.yaml"))

    def test_no_file_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None)
        assert cfg.server.url == "http://localhost:3000"

    def test_data_dir_config_is_found_first(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "config.yaml").write_text("server_url: http://from-data-dir\n")
        cfg = load_config(None)
        assert cfg.server.url == "http://from-data-dir"

    def test_write_minimal_config_roundtrip(self, tmp_path):
        target = tmp_path / "sub" / "config.yaml"
        write_minimal_config(target, "https://api.example.com/api")
        assert load_config(str(target)).server.url == "https://api.example.com/api"
