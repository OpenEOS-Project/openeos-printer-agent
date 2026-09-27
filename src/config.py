import sys
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel


class AgentConfig(BaseModel):
    id: str = "agent-01"
    name: str = "OpenEOS Printer Agent"


class PrinterConfig(BaseModel):
    """Hardware-fixed configuration for a printer attached to this RPi.

    The agent treats this as the source of truth and pushes the values to the
    backend on every connect. Keep `localId` stable across restarts — the
    backend uses it to match the on-disk config to the existing Printer row.
    """

    localId: str
    name: str
    type: str = "receipt"  # receipt | kitchen | label
    # Enger als ein einfaches str: ein Tippfehler hier (z.B. "USB",
    # "netwerk") soll beim Start hart fehlschlagen statt erst am Kassentisch
    # als Drucker, der nie druckt, aufzufallen.
    connectionType: Literal["usb", "network", "bluetooth"] = "usb"
    usbVendorId: Optional[str] = None  # e.g. "0x04b8"
    usbProductId: Optional[str] = None  # e.g. "0x0202"
    ipAddress: Optional[str] = None  # for network printers
    port: Optional[int] = None
    # Nur 58/80 mm Thermorollen werden unterstützt; alles andere ergibt ein
    # Layout, das auf der echten Rolle nicht passt.
    paperWidth: Literal[58, 80] = 80


class ServerConfig(BaseModel):
    url: str = "http://localhost:3000"
    device_token: Optional[str] = None
    heartbeat_interval_ms: int = 30000
    reconnect_interval_ms: int = 5000
    reconnect_max_interval_ms: int = 60000

    @property
    def heartbeat_interval(self) -> float:
        return self.heartbeat_interval_ms / 1000.0

    @property
    def reconnect_interval(self) -> float:
        return self.reconnect_interval_ms / 1000.0

    @property
    def reconnect_max_interval(self) -> float:
        return self.reconnect_max_interval_ms / 1000.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: Optional[str] = None
    max_bytes: int = 10_485_760  # 10 MB
    backup_count: int = 5


class LocalServerConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


class SentryConfig(BaseModel):
    enabled: bool = False
    dsn: Optional[str] = None
    environment: str = "production"
    traces_sample_rate: float = 0.1


class AppConfig(BaseModel):
    agent: AgentConfig = AgentConfig()
    server: ServerConfig = ServerConfig()
    logging: LoggingConfig = LoggingConfig()
    local_server: LocalServerConfig = LocalServerConfig()
    sentry: SentryConfig = SentryConfig()
    printers: list[PrinterConfig] = []
    device_token_file: str = "/var/lib/openeos-printer/device.json"
    # SQLite file for the crash-safe print-job queue; defaults to
    # print-jobs.db next to the device token file.
    job_store_file: Optional[str] = None
    # Cash-drawer kick connector pin. Most Epson drawers are wired to pin 2,
    # some to pin 5 — set to whichever physically opens the drawer.
    cash_drawer_pin: int = 2


def load_config(path: Optional[str] = None) -> AppConfig:
    """Load configuration from YAML file.

    Search order:
    1. Explicit path argument
    2. config/config.yaml (relative to CWD)
    3. /etc/openeos-printer/config.yaml
    4. ~/.config/openeos-printer/config.yaml
    """
    search_paths = [
        Path("config/config.yaml"),
        Path("/etc/openeos-printer/config.yaml"),
        Path.home() / ".config" / "openeos-printer" / "config.yaml",
    ]

    if path:
        config_path = Path(path)
    else:
        config_path = None
        for p in search_paths:
            if p.exists():
                config_path = p
                break

    if config_path is None:
        # No config file found - use defaults (self-registration mode)
        return AppConfig()

    if not config_path.exists():
        print(f"ERROR: Configuration file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path, "r") as f:
        raw = yaml.safe_load(f)

    if not raw:
        # Empty config file - use defaults
        return AppConfig()

    config = AppConfig(**raw)
    return config
