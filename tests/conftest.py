import pytest

from src.config import (
    AgentConfig,
    AppConfig,
    LocalServerConfig,
    LoggingConfig,
    PrinterConfig,
    SentryConfig,
    ServerConfig,
)


@pytest.fixture(autouse=True)
def _isolate_template_cache(tmp_path, monkeypatch):
    """Point TemplateEngine's on-disk template cache at a throwaway dir.

    Without this, TemplateEngine.update_server_templates() (used by the
    server-template-override tests) writes .jinja2 files into the real
    repo-root cache/templates/ dir. Those files then persist across test
    runs (cache/ is gitignored, so nothing cleans them up) and get loaded
    by every subsequent TemplateEngine() instance, silently overriding
    built-in templates like "receipt" in unrelated tests.
    """
    from src import template_engine

    monkeypatch.setattr(template_engine, "CACHE_DIR", tmp_path / "template_cache")


@pytest.fixture
def sample_server_config():
    return ServerConfig(
        url="http://localhost:3000",
        device_token="test-token-123",
    )


@pytest.fixture
def sample_printer_config():
    return PrinterConfig(
        localId="printer-001",
        name="Test Printer",
        connectionType="network",
        paperWidth=80,
        ipAddress="192.168.1.100",
        port=9100,
    )


@pytest.fixture
def sample_usb_printer_config():
    return PrinterConfig(
        localId="printer-002",
        name="USB Printer",
        connectionType="usb",
        paperWidth=80,
        usbVendorId="0x04b8",
        usbProductId="0x0202",
    )


@pytest.fixture
def sample_config(sample_server_config, sample_printer_config):
    return AppConfig(
        agent=AgentConfig(id="test-agent", name="Test Agent"),
        server=sample_server_config,
        printers=[sample_printer_config],
        logging=LoggingConfig(level="DEBUG"),
        local_server=LocalServerConfig(enabled=False),
        sentry=SentryConfig(enabled=False),
    )


@pytest.fixture
def sample_print_job():
    return {
        "jobId": "job-001",
        "printerId": "printer-001",
        "templateName": "receipt",
        "copies": 1,
        "payload": {
            "organization": {"name": "Test Verein"},
            "order_number": "42",
            "created_at": "2024-06-15T14:30:00Z",
            "items": [
                {"quantity": 2, "name": "Bratwurst", "total": 7.00},
                {"quantity": 1, "name": "Cola 0.5l", "total": 3.50},
            ],
            "total": 10.50,
            "payment_method": "Bar",
            "paid_amount": 20.00,
            "change": 9.50,
        },
    }


@pytest.fixture
def sample_kitchen_job():
    return {
        "jobId": "job-002",
        "printerId": "printer-001",
        "templateName": "kitchen",
        "payload": {
            "order_number": "42",
            "daily_number": 42,
            "table_number": "5",
            "created_at": "2024-06-15T14:30:00Z",
            "items": [
                {"quantity": 2, "name": "Bratwurst", "notes": "ohne Senf"},
                {"quantity": 1, "name": "Pommes", "kitchen_notes": "extra knusprig"},
            ],
        },
    }
