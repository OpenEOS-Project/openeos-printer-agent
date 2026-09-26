import logging
from typing import TYPE_CHECKING, Optional

from aiohttp import web

from .version import __version__

if TYPE_CHECKING:
    from .config import AppConfig
    from .device_registrar import DeviceRegistrar
    from .job_queue import JobQueue
    from .printer_manager import PrinterManager
    from .system_monitor import SystemMonitor
    from .websocket_client import WebSocketClient

logger = logging.getLogger(__name__)

SETUP_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>OpenEOS Printer Agent - Setup</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f5f5f5; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
  .container {{ text-align: center; padding: 2em; max-width: 500px; }}
  h1 {{ color: #1a1a1a; font-size: 1.5em; margin-bottom: 0.5em; }}
  .code {{ font-size: 4em; font-weight: bold; letter-spacing: 0.3em; color: #2563eb; font-family: 'Courier New', monospace; margin: 0.5em 0; padding: 0.3em 0.5em; background: white; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); display: inline-block; }}
  .instructions {{ color: #555; font-size: 1.1em; margin: 1em 0; }}
  .meta {{ color: #888; font-size: 0.85em; margin-top: 2em; }}
  .spinner {{ display: inline-block; width: 20px; height: 20px; border: 3px solid #ddd; border-top: 3px solid #2563eb; border-radius: 50%; animation: spin 1s linear infinite; vertical-align: middle; margin-right: 0.5em; }}
  @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
  .status {{ color: #888; font-size: 0.95em; }}
</style>
</head>
<body>
<div class="container">
  <h1>OpenEOS Printer Agent</h1>
  <p class="instructions">Geben Sie diesen Code im OpenEOS Dashboard ein:</p>
  <div class="code">{verification_code}</div>
  <p class="status"><span class="spinner"></span>Warte auf Verknuepfung...</p>
  <p class="meta">Server: {server_url}</p>
  <p class="meta">v{version} &middot; Auto-Refresh alle 3 Sekunden</p>
</div>
</body>
</html>"""

WAITING_FOR_ORG_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>OpenEOS Printer Agent - Warte auf Zuweisung</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; background: #f5f5f5; color: #333; display: flex; justify-content: center; align-items: center; min-height: 100vh; }}
  .container {{ text-align: center; padding: 2em; max-width: 500px; }}
  h1 {{ color: #1a1a1a; font-size: 1.5em; margin-bottom: 0.5em; }}
  .device-id {{ font-family: 'Courier New', monospace; background: white; padding: 0.5em 1em; border-radius: 8px; font-size: 0.85em; color: #555; box-shadow: 0 1px 3px rgba(0,0,0,0.1); display: inline-block; margin: 0.5em 0; }}
  .instructions {{ color: #555; font-size: 1.1em; margin: 1em 0; }}
  .meta {{ color: #888; font-size: 0.85em; margin-top: 2em; }}
  .spinner {{ display: inline-block; width: 20px; height: 20px; border: 3px solid #ddd; border-top: 3px solid #f59e0b; border-radius: 50%; animation: spin 1s linear infinite; vertical-align: middle; margin-right: 0.5em; }}
  @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
  .status {{ color: #888; font-size: 0.95em; }}
  .connected {{ color: #28a745; }}
  .disconnected {{ color: #dc3545; }}
</style>
</head>
<body>
<div class="container">
  <h1>OpenEOS Printer Agent</h1>
  <p class="instructions">Dieses Geraet wartet auf eine Organisationszuweisung.</p>
  <div class="device-id">{device_id}</div>
  <p class="status"><span class="spinner"></span>Warte auf Zuweisung...</p>
  <p>Server-Verbindung: <span class="{conn_class}">{conn_status}</span></p>
  <p class="meta">Server: {server_url}</p>
  <p class="meta">v{version} &middot; Auto-Refresh alle 5 Sekunden</p>
</div>
</body>
</html>"""

STATUS_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>OpenEOS Printer Agent</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 2em; background: #f5f5f5; color: #333; }}
  h1 {{ color: #1a1a1a; }}
  .card {{ background: white; border-radius: 8px; padding: 1.5em; margin-bottom: 1em; box-shadow: 0 1px 3px rgba(0,0,0,0.12); }}
  .card h2 {{ margin-top: 0; font-size: 1.1em; color: #555; text-transform: uppercase; letter-spacing: 0.05em; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th, td {{ text-align: left; padding: 0.5em 1em; border-bottom: 1px solid #eee; }}
  th {{ color: #888; font-weight: 500; }}
  .status {{ display: inline-block; padding: 0.2em 0.8em; border-radius: 12px; font-weight: 500; font-size: 0.9em; }}
  .status-online {{ background: #d4edda; color: #155724; }}
  .status-offline {{ background: #f8d7da; color: #721c24; }}
  .status-error {{ background: #fff3cd; color: #856404; }}
  .status-busy {{ background: #cce5ff; color: #004085; }}
  .connected {{ color: #28a745; font-weight: bold; }}
  .disconnected {{ color: #dc3545; font-weight: bold; }}
  .meta {{ color: #888; font-size: 0.85em; }}
</style>
</head>
<body>
<h1>OpenEOS Printer Agent</h1>
<p class="meta">v{version} &middot; {agent_name} ({agent_id})</p>

<div class="card">
  <h2>Server Connection</h2>
  <p class="{conn_class}">{conn_status}</p>
  <p class="meta">Server: {server_url}</p>
  <p class="meta">Organisation: {org_name}</p>
</div>

<div class="card">
  <h2>Printers</h2>
  {printers_table}
</div>

<div class="card">
  <h2>Queue Statistics</h2>
  {queue_table}
</div>

<div class="card">
  <h2>System</h2>
  <table>
    <tr><td>CPU Temperature</td><td>{cpu_temp}</td></tr>
    <tr><td>CPU Usage</td><td>{cpu_percent}%</td></tr>
    <tr><td>Memory</td><td>{mem_used} / {mem_total} MB ({mem_percent}%)</td></tr>
    <tr><td>Disk</td><td>{disk_used} / {disk_total} GB ({disk_percent}%)</td></tr>
    <tr><td>Network</td><td>{network_ip}</td></tr>
    <tr><td>Uptime</td><td>{uptime}</td></tr>
  </table>
</div>

<p class="meta">Auto-refresh every 10 seconds</p>
</body>
</html>"""


class LocalServer:
    """Local HTTP status server using aiohttp."""

    def __init__(
        self,
        config: "AppConfig",
        system_monitor: "SystemMonitor",
        device_registrar: Optional["DeviceRegistrar"] = None,
        printer_manager: Optional["PrinterManager"] = None,
        job_queue: Optional["JobQueue"] = None,
        ws_client: Optional["WebSocketClient"] = None,
    ) -> None:
        self._config = config
        self._system_monitor = system_monitor
        self._device_registrar = device_registrar
        self._printer_manager = printer_manager
        self._job_queue = job_queue
        self._ws_client = ws_client
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._setup_routes()

    def set_components(
        self,
        printer_manager: "PrinterManager",
        job_queue: "JobQueue",
        ws_client: "WebSocketClient",
    ) -> None:
        """Set components after they are initialized."""
        self._printer_manager = printer_manager
        self._job_queue = job_queue
        self._ws_client = ws_client

    def _setup_routes(self) -> None:
        self._app.router.add_get("/", self._handle_status_page)
        self._app.router.add_get("/api/status", self._handle_api_status)
        self._app.router.add_get("/api/printers", self._handle_api_printers)
        self._app.router.add_get("/api/jobs", self._handle_api_jobs)
        self._app.router.add_get("/api/system", self._handle_api_system)
        self._app.router.add_get("/health", self._handle_health)

    async def start(self) -> None:
        cfg = self._config.local_server
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, cfg.host, cfg.port)
        await site.start()
        logger.info(f"Local status server started on http://{cfg.host}:{cfg.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            logger.info("Local status server stopped")

    async def _get_system_info(self) -> dict:
        try:
            return await self._system_monitor.get_system_info()
        except Exception:
            return {}

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_api_status(self, request: web.Request) -> web.Response:
        # Setup mode
        if self._device_registrar and not self._printer_manager:
            return web.json_response(
                {
                    "mode": "setup",
                    "status": self._device_registrar.status,
                    "verificationCode": self._device_registrar.verification_code,
                    "deviceId": self._device_registrar.device_id,
                }
            )

        sys_info = await self._get_system_info()
        data = {
            "agent": {
                "id": self._config.agent.id,
                "name": self._config.agent.name,
                "version": __version__,
            },
            "connection": {
                "connected": self._ws_client.is_connected if self._ws_client else False,
                "serverUrl": self._config.server.url,
            },
            "printers": self._printer_manager.get_all_statuses() if self._printer_manager else [],
            "queue": self._job_queue.get_queue_stats() if self._job_queue else {},
            "system": sys_info,
        }
        return web.json_response(data)

    async def _handle_api_printers(self, request: web.Request) -> web.Response:
        if self._printer_manager:
            return web.json_response(self._printer_manager.get_all_statuses())
        return web.json_response([])

    async def _handle_api_jobs(self, request: web.Request) -> web.Response:
        if self._job_queue:
            return web.json_response(self._job_queue.get_queue_stats())
        return web.json_response({})

    async def _handle_api_system(self, request: web.Request) -> web.Response:
        return web.json_response(await self._get_system_info())

    async def _handle_status_page(self, request: web.Request) -> web.Response:
        # Setup mode: show verification code
        if self._device_registrar and self._device_registrar.status == "pending":
            html = SETUP_HTML.format(
                verification_code=self._device_registrar.verification_code or "------",
                server_url=self._config.server.url,
                version=__version__,
            )
            return web.Response(text=html, content_type="text/html")

        # Waiting for org assignment
        if self._device_registrar and self._device_registrar.status == "waiting_for_org":
            html = WAITING_FOR_ORG_HTML.format(
                device_id=self._device_registrar.device_id or "unknown",
                conn_class="connected" if (self._ws_client and self._ws_client.is_connected) else "disconnected",
                conn_status="Verbunden" if (self._ws_client and self._ws_client.is_connected) else "Getrennt",
                server_url=self._config.server.url,
                version=__version__,
            )
            return web.Response(text=html, content_type="text/html")

        # Normal operational mode
        sys_info = await self._get_system_info()
        printers = self._printer_manager.get_all_statuses() if self._printer_manager else []
        queue_stats = self._job_queue.get_queue_stats() if self._job_queue else {}

        # Build printers table
        if printers:
            rows = ""
            for p in printers:
                status_cls = f"status-{p['status']}"
                rows += f"<tr><td>{p['name']}</td><td>{p['connectionType']}</td>"
                rows += f"<td><span class='status {status_cls}'>{p['status']}</span></td>"
                rows += f"<td>{p.get('lastError') or '-'}</td></tr>"
            printers_table = f"<table><tr><th>Name</th><th>Type</th><th>Status</th><th>Error</th></tr>{rows}</table>"
        else:
            printers_table = "<p>No printers configured</p>"

        # Build queue table
        if queue_stats:
            q_rows = ""
            for pid, stats in queue_stats.items():
                printer_name = pid
                for p in printers:
                    if p["printerId"] == pid:
                        printer_name = p["name"]
                        break
                q_rows += f"<tr><td>{printer_name}</td><td>{stats.get('pending', 0)}</td>"
                q_rows += f"<td>{stats.get('completed', 0)}</td><td>{stats.get('failed', 0)}</td></tr>"
            queue_table = (
                f"<table><tr><th>Printer</th><th>Pending</th><th>Completed</th><th>Failed</th></tr>{q_rows}</table>"
            )
        else:
            queue_table = "<p>No queue data</p>"

        # Format values
        cpu_temp = f"{sys_info.get('cpu_temp', 'N/A')}°C" if sys_info.get("cpu_temp") else "N/A"
        uptime_s = sys_info.get("uptime_seconds", 0)
        hours, remainder = divmod(uptime_s, 3600)
        minutes, _ = divmod(remainder, 60)
        uptime = f"{hours}h {minutes}m"

        mem = sys_info.get("memory", {})
        disk = sys_info.get("disk", {})
        net = sys_info.get("network", {})

        org_name = "N/A"
        if self._device_registrar:
            org_name = self._device_registrar.organization_name or "N/A"

        html = STATUS_HTML.format(
            version=__version__,
            agent_name=self._config.agent.name,
            agent_id=self._config.agent.id,
            conn_class="connected" if (self._ws_client and self._ws_client.is_connected) else "disconnected",
            conn_status="Connected" if (self._ws_client and self._ws_client.is_connected) else "Disconnected",
            server_url=self._config.server.url,
            org_name=org_name,
            printers_table=printers_table,
            queue_table=queue_table,
            cpu_temp=cpu_temp,
            cpu_percent=sys_info.get("cpu_percent", "N/A"),
            mem_used=mem.get("used_mb", "N/A"),
            mem_total=mem.get("total_mb", "N/A"),
            mem_percent=mem.get("percent", "N/A"),
            disk_used=disk.get("used_gb", "N/A"),
            disk_total=disk.get("total_gb", "N/A"),
            disk_percent=disk.get("percent", "N/A"),
            network_ip=net.get("ip_address", "N/A"),
            uptime=uptime,
        )
        return web.Response(text=html, content_type="text/html")
