import pytest

from src.config import (
    AppConfig,
    DiscoveryConfig,
    LocalServerConfig,
    LoggingConfig,
    ServerConfig,
)


@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path, monkeypatch):
    """Keep every test away from the real data/log directories.

    src/paths.py honours these environment variables, so the device token,
    job store, template cache and log files of a test all land in tmp_path.
    """
    monkeypatch.setenv("OPENEOS_PRINTER_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OPENEOS_PRINTER_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def _detach_agent_log_handlers():
    """setup_logging() adds a console handler bound to pytest's captured
    stdout; once the test ends that stream is closed and later log calls
    would raise "I/O operation on closed file"."""
    yield
    from src.utils import remove_agent_handlers

    remove_agent_handlers()


@pytest.fixture(autouse=True)
def _isolate_template_cache(tmp_path, monkeypatch):
    """Point TemplateEngine's on-disk template cache at a throwaway dir.

    Without this, TemplateEngine.update_server_templates() (used by the
    server-template-override tests) writes .jinja2 files into the shared
    cache dir. Those files then persist across test runs and get loaded by
    every subsequent TemplateEngine() instance, silently overriding
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
def sample_config(sample_server_config, tmp_path):
    return AppConfig(
        server=sample_server_config,
        logging=LoggingConfig(level="DEBUG", dir=str(tmp_path / "logs")),
        local_server=LocalServerConfig(enabled=False),
        discovery=DiscoveryConfig(interval_s=3600, mdns_timeout_s=0.1),
        data_dir=str(tmp_path / "data"),
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
