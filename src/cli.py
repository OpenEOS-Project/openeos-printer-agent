"""Command line interface.

    openeos-printer-agent run [--server-url URL] [--config FILE] [--dummy-printer]
    openeos-printer-agent pair [--server-url URL]
    openeos-printer-agent status
    openeos-printer-agent install-service [--server-url URL]   (Windows)
    openeos-printer-agent remove-service                       (Windows)

Without a subcommand ``run`` is assumed, so the systemd unit's
``python -m src.main --config ...`` keeps working unchanged. Without any
config file and without ``--server-url`` the agent talks to the hosted
service (``config.DEFAULT_SERVER_URL``), so double-clicking the Windows
EXE is enough to get a pairing code.
"""

import argparse
import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import AppConfig, find_config_file, load_config, write_minimal_config
from .paths import default_data_dir, is_windows
from .version import get_version

RUN_FLAGS = ("--config", "-c", "--server-url", "--dummy-printer", "--log-level", "--data-dir", "--subnet-scan")

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openeos-printer-agent", description="OpenEOS Printer Agent")
    parser.add_argument("--version", "-v", action="version", version=f"openeos-printer-agent {get_version()}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", "-c", type=str, default=None, help="Path to config.yaml")
        p.add_argument("--server-url", type=str, default=None, help="OpenEOS API base URL (incl. /api)")
        p.add_argument("--data-dir", type=str, default=None, help="Override the data directory")
        p.add_argument("--log-level", type=str, default=None, help="DEBUG, INFO, WARNING or ERROR")

    run = sub.add_parser("run", help="Run the agent in the foreground (default)")
    add_common(run)
    run.add_argument(
        "--dummy-printer",
        action="store_true",
        help="Test helper: print to escpos Dummy and log the output as text instead of using hardware",
    )
    run.add_argument(
        "--subnet-scan",
        action="store_true",
        help="Also run the port-9100 subnet scan on the scheduled discovery runs",
    )

    pair = sub.add_parser("pair", help="Register with the server, show the pairing code and exit once linked")
    add_common(pair)

    status = sub.add_parser("status", help="Show the state of a running agent (local status API)")
    status.add_argument("--port", type=int, default=None, help="Status page port (default from config, 8080)")
    status.add_argument("--json", action="store_true", help="Print the raw status JSON")

    install = sub.add_parser("install-service", help="Install and start the Windows service (run as Administrator)")
    add_common(install)
    sub.add_parser("remove-service", help="Stop and remove the Windows service (run as Administrator)")
    # Hidden: the Windows service control manager starts the executable with
    # this argument (see windows_service.install_service).
    sub.add_parser("service", help=argparse.SUPPRESS)
    return parser


def _normalize_argv(argv: list[str]) -> list[str]:
    """Insert ``run`` when the first argument is a run flag instead of a subcommand."""
    if not argv:
        return ["run"]
    first = argv[0]
    if first.startswith("-") and first not in ("--version", "-v", "--help", "-h"):
        return ["run", *argv]
    return argv


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    if getattr(args, "server_url", None):
        overrides["server_url"] = args.server_url
    if getattr(args, "data_dir", None):
        overrides["data_dir"] = args.data_dir
    if getattr(args, "dummy_printer", False):
        overrides["dummy_printer"] = True
    if getattr(args, "log_level", None):
        overrides["logging"] = {"level": args.log_level}
    if getattr(args, "subnet_scan", False):
        overrides["discovery"] = {"subnet_scan_on_schedule": True}
    return overrides


def _load(args: argparse.Namespace) -> AppConfig:
    config = load_config(getattr(args, "config", None), _overrides(args))
    _persist_server_url_if_new(args, config)
    return config


def _persist_server_url_if_new(args: argparse.Namespace, config: AppConfig) -> None:
    """``--server-url`` on a machine without any config file: write the
    minimal config so the next start (service, reboot) needs no flags."""
    if not getattr(args, "server_url", None):
        return
    if find_config_file(getattr(args, "config", None)) is not None:
        return
    target = config.data_path / "config.yaml"
    try:
        write_minimal_config(target, config.server.url)
        print(f"Wrote {target}")
    except OSError as e:
        print(f"Note: could not write {target} ({e}); pass --server-url again next time", file=sys.stderr)


def cmd_run(args: argparse.Namespace) -> int:
    from .main import PrinterAgent

    config = _load(args)
    agent = PrinterAgent(config=config)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    """Register + wait for the admin to enter the code, then exit. Handy on
    Windows before ``install-service`` and on headless boxes."""
    from .device_registrar import DeviceRegistrar
    from .utils import setup_logging

    config = _load(args)
    setup_logging(config)
    logger.info(config.describe_server_url())

    async def _pair() -> int:
        registrar = DeviceRegistrar(config)
        await registrar.ensure_registered()
        if registrar.status != "verified":
            await registrar.wait_for_verification()
        print(f"Paired. Organization: {registrar.organization_name or '-'} (device {registrar.device_id})")
        print(f"Token stored in {registrar.token_file}")
        return 0

    try:
        return asyncio.run(_pair())
    except KeyboardInterrupt:
        return 130


def cmd_status(args: argparse.Namespace) -> int:
    port = args.port
    if port is None:
        try:
            port = load_config(None).local_server.port
        except SystemExit:
            port = 8080
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"Agent not reachable at {url} ({e}). Is it running?")
        if is_windows():
            _print_windows_service_state()
        return 1

    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    if data.get("mode") == "setup":
        print(f"Status: {data.get('status')} - pairing code: {data.get('verificationCode') or '-'}")
        return 0

    agent = data.get("agent", {})
    conn = data.get("connection", {})
    print(f"OpenEOS Printer Agent v{agent.get('version')} on {agent.get('hostname')} (device {agent.get('deviceId')})")
    print(f"Server: {conn.get('serverUrl')} - {'connected' if conn.get('connected') else 'DISCONNECTED'}")
    print(f"Organization: {agent.get('organization') or '-'}")
    printers = data.get("printers", [])
    print(f"Printers ({len(printers)}):")
    for p in printers:
        err = f" - {p['lastError']}" if p.get("lastError") else ""
        print(f"  [{p['status']:7}] {p['name']} ({p['connectionType']}){err}")
    discovery = data.get("discovery") or {}
    found = discovery.get("printers", [])
    print(f"Discovered ({len(found)}):")
    for d in found:
        print(f"  {d['displayName']} ({d['connectionType']}, {d['discoveredVia']}) {d['fingerprint']}")
    if is_windows():
        _print_windows_service_state()
    return 0


def _print_windows_service_state() -> None:
    try:
        from .windows_service import service_state

        print(f"Windows service: {service_state()}")
    except Exception as e:  # pragma: no cover - Windows only
        print(f"Windows service: unknown ({e})")


def _windows_only(command: str) -> int:
    print(
        f"'{command}' is only available on Windows. On Linux use install.sh "
        "(systemd unit 'openeos-printer') - see README.",
        file=sys.stderr,
    )
    return 2


def cmd_install_service(args: argparse.Namespace) -> int:
    if not is_windows():
        return _windows_only("install-service")
    from .windows_service import install_service

    config = _load(args)
    print(config.describe_server_url())
    return install_service(config_path=getattr(args, "config", None), server_url=config.server.url)


def cmd_remove_service(args: argparse.Namespace) -> int:
    if not is_windows():
        return _windows_only("remove-service")
    from .windows_service import remove_service

    return remove_service()


def cmd_service(args: argparse.Namespace) -> int:
    if not is_windows():
        return _windows_only("service")
    from .windows_service import run_service_dispatcher

    return run_service_dispatcher()


COMMANDS = {
    "run": cmd_run,
    "pair": cmd_pair,
    "status": cmd_status,
    "install-service": cmd_install_service,
    "remove-service": cmd_remove_service,
    "service": cmd_service,
}


def main(argv: Optional[list[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(list(sys.argv[1:] if argv is None else argv)))
    command = args.command or "run"
    if command == "run" and not hasattr(args, "dummy_printer"):
        args = parser.parse_args(["run"])
    code = COMMANDS[command](args)
    if code:
        sys.exit(code)


def data_dir_hint() -> str:
    """Used by help texts and the README generator."""
    return str(default_data_dir())


if __name__ == "__main__":
    main()
