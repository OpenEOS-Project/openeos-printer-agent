import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field

from .paths import default_config_search_paths, default_data_dir, default_log_dir

# The hosted OpenEOS service. Used when neither a config file nor
# ``--server-url`` names a server, so a freshly downloaded binary can pair
# without any arguments (double-click on Windows). Self-hosters override it
# with ``--server-url`` or ``server_url:`` in config.yaml.
DEFAULT_SERVER_URL = "https://api.openeos.de/api"


class ServerConfig(BaseModel):
    """Connection tuning. ``url`` is the only value a deployment may have to
    set (it defaults to the hosted service); everything else has defaults
    that match the backend's expectations."""

    url: str = DEFAULT_SERVER_URL
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
    # Directory for the rotating agent.log; None = platform default
    # (see src/paths.py). ``file`` is kept for older configs that named the
    # log file explicitly.
    dir: Optional[str] = None
    file: Optional[str] = None
    max_bytes: int = 10_485_760  # 10 MB
    backup_count: int = 5


class LocalServerConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080


class DiscoveryConfig(BaseModel):
    """Printer discovery (USB, Windows spooler, mDNS, optional port scan)."""

    # Seconds between automatic scans. The spec says 5 minutes; the socket
    # event ``printerDiscover`` triggers an extra scan at any time.
    interval_s: int = 300
    # mDNS browse duration per scan. Printers answer within a second or two,
    # a bit of slack catches the slow ones without stalling the scan.
    mdns_timeout_s: float = 3.0
    # Port-9100 scan: TCP connect timeout per host and parallelism.
    portscan_timeout_ms: int = 300
    portscan_concurrency: int = 64
    # Subnet to scan in CIDR notation. Default: the /24 of the interface that
    # carries the default route. Set this when the agent sits on a machine
    # with several interfaces (VPN, Docker bridges) and the wrong one wins.
    subnet: Optional[str] = None
    # Also run the port scan on the automatic scans, not only when the web
    # app asks for it. Off by default - it is the noisy part of discovery.
    subnet_scan_on_schedule: bool = False


class AppConfig(BaseModel):
    """Everything the agent needs. In practice a config.yaml contains just

        server_url: https://api.example.com/api

    and the rest is filled from defaults. The nested blocks exist for the few
    deployments that need to tune them (status page port, log level, ...).
    """

    # Flat alias for server.url - the one setting every install needs, so it
    # should not require knowing about the nested ``server`` block.
    server_url: Optional[str] = None
    # Optional pre-provisioned device token (otherwise the agent pairs itself
    # on first start and persists the token in the data directory).
    device_token: Optional[str] = None
    server: ServerConfig = Field(default_factory=ServerConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    local_server: LocalServerConfig = Field(default_factory=LocalServerConfig)
    discovery: DiscoveryConfig = Field(default_factory=DiscoveryConfig)
    # Data directory override (device token, job store, template cache).
    data_dir: Optional[str] = None
    # Explicit file overrides; both default to files inside ``data_dir``.
    device_token_file: Optional[str] = None
    job_store_file: Optional[str] = None
    # Cash-drawer kick connector pin. Most Epson drawers are wired to pin 2,
    # some to pin 5 - set to whichever physically opens the drawer.
    cash_drawer_pin: int = 2
    # Test helper: every adopted printer is backed by escpos.printer.Dummy and
    # the rendered ESC/POS output is written to the log instead of hardware.
    dummy_printer: bool = False
    # Where server.url came from (set by load_config): "--server-url",
    # "config file <path>" or "default". Only used for the startup log line
    # that tells the operator how to point the agent somewhere else.
    server_url_source: str = "default"

    def model_post_init(self, __context: Any) -> None:
        # Let the flat keys win over the nested block so a minimal YAML
        # (``server_url: ...``) and a legacy YAML (``server: {url: ...}``)
        # both end up in server.url / server.device_token.
        if self.server_url:
            self.server.url = self.server_url.rstrip("/")
        else:
            self.server_url = self.server.url
        if self.device_token and not self.server.device_token:
            self.server.device_token = self.device_token

    # -- derived paths -----------------------------------------------------

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).expanduser() if self.data_dir else default_data_dir()

    @property
    def log_path(self) -> Path:
        if self.logging.dir:
            return Path(self.logging.dir).expanduser()
        if self.logging.file:
            return Path(self.logging.file).expanduser().parent
        return default_log_dir()

    @property
    def log_file(self) -> Path:
        if self.logging.file:
            return Path(self.logging.file).expanduser()
        return self.log_path / "agent.log"

    @property
    def device_token_path(self) -> Path:
        if self.device_token_file:
            return Path(self.device_token_file).expanduser()
        return self.data_path / "device.json"

    @property
    def job_store_path(self) -> Path:
        if self.job_store_file:
            return Path(self.job_store_file).expanduser()
        return self.data_path / "print-jobs.db"

    @property
    def template_cache_path(self) -> Path:
        return self.data_path / "templates-cache"

    def describe_server_url(self) -> str:
        """One line for the log/console: which server, chosen how, and how
        to change it. Shown at every start so a self-hoster who forgot the
        flag sees immediately why the agent talks to the hosted service."""
        if self.server_url_source == "default":
            return (
                f"Server: {self.server.url} (default: hosted OpenEOS service). "
                f"Self-hosting? Start with --server-url https://<your-host>/api "
                f"or set server_url in {self.data_path / 'config.yaml'}"
            )
        return f"Server: {self.server.url} (from {self.server_url_source})"


def find_config_file(path: Optional[str] = None) -> Optional[Path]:
    """Resolve the config file to load (explicit path or first default hit)."""
    if path:
        return Path(path).expanduser()
    for candidate in default_config_search_paths():
        if candidate.exists():
            return candidate
    return None


def load_config(path: Optional[str] = None, overrides: Optional[dict[str, Any]] = None) -> AppConfig:
    """Load configuration from YAML file.

    Search order:
    1. Explicit path argument
    2. <data_dir>/config.yaml (written by install-service / install.sh)
    3. config/config.yaml (relative to CWD)
    4. /etc/openeos-printer/config.yaml, ~/.config/openeos-printer/config.yaml (Linux)

    ``overrides`` (CLI flags such as ``--server-url``) take precedence over
    the file. With neither a file nor overrides the defaults apply - in
    particular ``DEFAULT_SERVER_URL`` (the hosted service), so a plain
    ``openeos-printer-agent run`` without any arguments can pair.
    """
    config_path = find_config_file(path)

    raw: dict[str, Any] = {}
    source = "default"
    if config_path is not None:
        if not config_path.exists():
            print(f"ERROR: Configuration file not found: {config_path}", file=sys.stderr)
            sys.exit(1)
        with open(config_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        if isinstance(loaded, dict):
            raw = loaded
            # Older configs carried a printer list; printers are now managed
            # exclusively in the web app, so the block is ignored on purpose.
            if raw.pop("printers", None):
                print(
                    "WARNING: 'printers' in config.yaml is ignored - printers are adopted in the web app now.",
                    file=sys.stderr,
                )
            raw.pop("agent", None)
            raw.pop("sentry", None)
            if raw.get("server_url") or (isinstance(raw.get("server"), dict) and raw["server"].get("url")):
                source = f"config file {config_path}"

    for key, value in (overrides or {}).items():
        if value is not None:
            raw[key] = value
    if (overrides or {}).get("server_url"):
        source = "--server-url"
    raw["server_url_source"] = source

    return AppConfig(**raw)


def write_minimal_config(path: Path, server_url: str) -> None:
    """Persist the one required setting so later starts (service) need no flags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# OpenEOS Printer Agent\n"
        "# Base URL of the OpenEOS API (including the /api prefix).\n"
        f"server_url: {server_url!r}\n",
        encoding="utf-8",
    )
