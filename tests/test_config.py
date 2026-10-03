import pytest
from pydantic import ValidationError

from src.config import AppConfig, PrinterConfig, ServerConfig


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


class TestPrinterConfig:
    def test_valid_network_printer(self, sample_printer_config):
        assert sample_printer_config.connectionType == "network"
        assert sample_printer_config.ipAddress == "192.168.1.100"
        assert sample_printer_config.paperWidth == 80

    def test_valid_usb_printer(self, sample_usb_printer_config):
        assert sample_usb_printer_config.connectionType == "usb"
        assert sample_usb_printer_config.usbVendorId == "0x04b8"

    def test_missing_local_id(self):
        with pytest.raises(ValidationError, match="localId"):
            PrinterConfig(
                name="Bad Printer",
                connectionType="network",
            )

    def test_connection_type_rejects_invalid_value(self):
        # connectionType is now a Literal — a typo like "serial" must fail
        # loudly at startup instead of silently producing a printer that
        # never connects.
        with pytest.raises(ValidationError, match="connectionType"):
            PrinterConfig(localId="p1", name="Bad Printer", connectionType="serial")

    def test_connection_type_accepts_valid_values(self):
        for value in ("usb", "network", "bluetooth"):
            cfg = PrinterConfig(localId="p1", name="Printer", connectionType=value)
            assert cfg.connectionType == value

    def test_paper_width_rejects_invalid_value(self):
        # paperWidth is now constrained to 58/80 mm — anything else must
        # fail loudly at startup instead of producing a garbled layout.
        with pytest.raises(ValidationError, match="paperWidth"):
            PrinterConfig(localId="p1", name="Bad Printer", connectionType="network", paperWidth=72)

    def test_paper_width_accepts_valid_values(self):
        for value in (58, 80):
            cfg = PrinterConfig(localId="p1", name="Printer", connectionType="usb", paperWidth=value)
            assert cfg.paperWidth == value

    def test_default_paper_width(self):
        cfg = PrinterConfig(localId="p1", name="P", connectionType="usb")
        assert cfg.paperWidth == 80

    def test_58mm_paper(self):
        cfg = PrinterConfig(localId="p1", name="P", connectionType="usb", paperWidth=58)
        assert cfg.paperWidth == 58


class TestAppConfig:
    def test_full_config(self, sample_config):
        assert sample_config.agent.id == "test-agent"
        assert len(sample_config.printers) == 1
        assert sample_config.printers[0].name == "Test Printer"

    def test_defaults(self):
        cfg = AppConfig(
            server=ServerConfig(url="http://localhost:3000", device_token="tok"),
        )
        assert cfg.agent.id == "agent-01"
        assert cfg.logging.level == "INFO"
        assert cfg.local_server.enabled is True
        assert cfg.sentry.enabled is False

    def test_no_printers(self):
        cfg = AppConfig(
            server=ServerConfig(url="http://localhost:3000", device_token="tok"),
        )
        assert cfg.printers == []
