import asyncio
import logging
from typing import Any, Callable, Optional

import aiohttp
import socketio

from .config import AppConfig, PrinterConfig
from .printer_manager import PrinterManager
from .system_monitor import SystemMonitor

logger = logging.getLogger(__name__)


def _build_connection_config(printer: PrinterConfig) -> dict[str, Any]:
    """Build the connection_config dict the backend stores for a printer based
    on the printer's local hardware fields (USB IDs, IP/port). Strips empty
    values so we don't write `null`s to the DB."""
    cfg: dict[str, Any] = {}
    if printer.connectionType == "usb":
        if printer.usbVendorId:
            cfg["usbVendorId"] = printer.usbVendorId
        if printer.usbProductId:
            cfg["usbProductId"] = printer.usbProductId
    elif printer.connectionType == "network":
        if printer.ipAddress:
            cfg["ipAddress"] = printer.ipAddress
        if printer.port is not None:
            cfg["port"] = printer.port
    return cfg


class WebSocketClient:
    """Socket.io client for communication with the OpenEOS backend."""

    def __init__(
        self,
        config: AppConfig,
        device_token: str,
        system_monitor: SystemMonitor,
        on_print_job: Optional[Callable[..., Any]] = None,
        on_template_update: Optional[Callable[..., Any]] = None,
        on_config_update: Optional[Callable[..., Any]] = None,
        on_cash_drawer: Optional[Callable[..., Any]] = None,
        on_ready: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._config = config
        self._device_token = device_token
        self._system_monitor = system_monitor
        self._on_print_job = on_print_job
        self._on_template_update = on_template_update
        self._on_config_update = on_config_update
        self._on_cash_drawer = on_cash_drawer
        self._on_ready = on_ready

        self._sio = socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,  # unlimited
            reconnection_delay=config.server.reconnect_interval,
            reconnection_delay_max=config.server.reconnect_max_interval,
            logger=False,
        )
        self._connected = False
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._server_config: dict[str, Any] = {}

        # Set lazily after PrinterManager is created
        self._printer_manager: Optional[PrinterManager] = None

        self._register_handlers()

    def set_printer_manager(self, printer_manager: PrinterManager) -> None:
        """Set the printer manager after it is initialized."""
        self._printer_manager = printer_manager

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        async def connect() -> None:
            self._connected = True
            logger.info(f"Connected to server: {self._config.server.url}")

        @sio.event
        async def disconnect() -> None:
            self._connected = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            logger.warning("Disconnected from server")

        @sio.on("connected")
        async def on_connected(data: dict[str, Any]) -> None:
            logger.info(f"Server confirmed authentication: {data}")
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            # Let the agent flush unreported job outcomes etc.
            if self._on_ready:
                asyncio.create_task(self._on_ready())

        @sio.on("error")
        async def on_error(data: dict[str, Any]) -> None:
            logger.error(f"Server error: {data}")

        @sio.on("printerJob")
        async def on_printer_job(data: dict[str, Any]) -> dict[str, bool]:
            """The return value is sent back as the socket.io ack — the server
            marks the job PRINTING once we confirm receipt (after the job has
            been persisted to the local store)."""
            logger.info(f"Received print job: {data.get('jobId', 'unknown')}")
            received = False
            if self._on_print_job:
                received = bool(await self._on_print_job(data))
            return {"received": received}

        @sio.on("templateUpdate")
        async def on_template_update(data: dict[str, Any]) -> None:
            logger.info("Received template update from server")
            if self._on_template_update:
                await self._on_template_update(data)

        @sio.on("openCashDrawer")
        async def on_open_cash_drawer(data: dict[str, Any]) -> None:
            logger.info(f"Received cash drawer open: printer {data.get('printerId')}")
            if self._on_cash_drawer:
                await self._on_cash_drawer(data)

        @sio.on("printerConfigUpdate")
        async def on_printer_config_update(data: dict[str, Any]) -> None:
            logger.info("Received printer config update from server")
            if self._on_config_update:
                await self._on_config_update(data)

        @sio.on("configUpdate")
        async def on_config_update(data: dict[str, Any]) -> None:
            """Server can override agent config (e.g., heartbeat interval)."""
            logger.info(f"Received config update: {data}")
            self._server_config = data

    async def connect(self) -> None:
        """Connect to the backend server."""
        url = self._config.server.url
        logger.info(f"Connecting to {url}...")

        await self._sio.connect(
            url,
            auth={"deviceToken": self._device_token},
            transports=["websocket"],
            wait_timeout=10,
        )

    async def disconnect(self) -> None:
        """Disconnect from server."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if self._connected:
            await self._sio.disconnect()
        self._connected = False

    async def wait(self) -> None:
        """Wait until disconnected."""
        await self._sio.wait()

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def fetch_printer_config(self) -> list[dict[str, Any]]:
        """Sync the agent's local printer config (config.yaml) to the backend
        and return the canonical printer descriptors with backend IDs.

        POST /device-api/printers/sync with the local config, x-device-token header.
        Falls back to the legacy GET /device-api/printers endpoint if no local
        printers are configured (so older deployments keep working).
        """
        local_printers = self._config.printers
        if not local_printers:
            return await self._legacy_fetch_printer_config()

        sync_payload = {
            "printers": [
                {
                    "localId": p.localId,
                    "name": p.name,
                    "type": p.type,
                    "connectionType": p.connectionType,
                    "connectionConfig": _build_connection_config(p),
                    "paperWidth": p.paperWidth,
                }
                for p in local_printers
            ]
        }

        url = f"{self._config.server.url}/device-api/printers/sync"
        headers = {"x-device-token": self._device_token, "Content-Type": "application/json"}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=sync_payload) as resp:
                if resp.status not in (200, 201):
                    body = await resp.text()
                    logger.error(f"Failed to sync printer config ({resp.status}): {body}")
                    return []

                result = await resp.json()
                payload = result.get("data", result) if isinstance(result, dict) else result
                synced = payload.get("printers", []) if isinstance(payload, dict) else []

        # Map backend IDs back to the printer manager's expected shape.
        id_by_local = {item.get("localId"): item.get("id") for item in synced}
        printers: list[dict[str, Any]] = []
        for p in local_printers:
            backend_id = id_by_local.get(p.localId)
            if not backend_id:
                logger.warning(f"Backend did not return an id for localId={p.localId}")
                continue
            printers.append(
                {
                    "id": backend_id,
                    "name": p.name,
                    "type": p.type,
                    "connectionType": p.connectionType,
                    "connectionConfig": _build_connection_config(p),
                    "paperWidth": p.paperWidth,
                    # hasCashDrawer stays under admin control; we let the backend's
                    # next /printers fetch determine its current value.
                    "hasCashDrawer": False,
                    "isActive": True,
                }
            )

        logger.info(f"Synced {len(printers)} printer(s) with backend (local config is source of truth)")
        return printers

    async def _legacy_fetch_printer_config(self) -> list[dict[str, Any]]:
        """Old GET-based path for installs without local printer config."""
        url = f"{self._config.server.url}/device-api/printers"
        headers = {"x-device-token": self._device_token}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Failed to fetch printer config ({resp.status}): {body}")
                    return []

                result = await resp.json()
                payload = result.get("data", result) if isinstance(result, dict) else result
                if isinstance(payload, list):
                    printers = payload
                elif isinstance(payload, dict):
                    printers = payload.get("printers", [])
                else:
                    printers = []
                logger.info(f"Fetched {len(printers)} printer(s) from backend (legacy path)")
                return printers

    async def fetch_templates(self) -> dict[str, str]:
        """Fetch templates from backend REST API.

        GET /device-api/templates with x-device-token header.
        """
        url = f"{self._config.server.url}/device-api/templates"
        headers = {"x-device-token": self._device_token}

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Failed to fetch templates ({resp.status}): {body}")
                    return {}

                result = await resp.json()
                # API wraps responses in {"data": {...}} — accept both that and the bare shape.
                payload = result.get("data", result) if isinstance(result, dict) else result
                templates = payload.get("templates", {}) if isinstance(payload, dict) else {}
                logger.info(f"Fetched {len(templates)} template(s) from backend")
                return templates

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat for all printers."""
        interval = self._config.server.heartbeat_interval

        while True:
            try:
                await self._send_heartbeat()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(interval)

    async def _send_heartbeat(self) -> None:
        """Send heartbeat for each printer + system info."""
        agent_id = self._config.agent.id

        if self._printer_manager:
            # Send heartbeat per printer (matches backend PrinterHeartbeatEvent)
            for printer in self._printer_manager.get_all_printers():
                await self._sio.emit(
                    "printerHeartbeat",
                    {
                        "printerId": printer.printer_id,
                        "agentId": agent_id,
                        "isOnline": printer.status.value in ("online", "busy"),
                    },
                )

            # Also send system info periodically
            try:
                sys_info = await self._system_monitor.get_system_info()
                await self._sio.emit(
                    "agentHeartbeat",
                    {
                        "agentId": agent_id,
                        "agentName": self._config.agent.name,
                        "printers": self._printer_manager.get_all_statuses(),
                        "system": sys_info,
                    },
                )
            except Exception as e:
                logger.debug(f"Failed to collect system info for heartbeat: {e}")

    async def report_job_complete(self, job_id: str) -> bool:
        """Report successful job completion to server.

        Returns True when the report reached the server; False lets the
        caller keep the outcome in the job store for redelivery on reconnect.
        """
        if not self._connected:
            return False
        try:
            await self._sio.emit(
                "printerJobComplete",
                {
                    "jobId": job_id,
                    "agentId": self._config.agent.id,
                },
            )
            logger.debug(f"Reported job {job_id} complete")
            return True
        except Exception as e:
            logger.warning(f"Failed to report job {job_id} complete: {e}")
            return False

    async def report_job_failed(self, job_id: str, error_code: str, error_message: str) -> bool:
        """Report job failure to server. Returns True when delivered."""
        if not self._connected:
            return False
        try:
            await self._sio.emit(
                "printerJobFailed",
                {
                    "jobId": job_id,
                    "agentId": self._config.agent.id,
                    "errorCode": error_code,
                    "errorMessage": error_message,
                },
            )
            logger.debug(f"Reported job {job_id} failed: {error_code}")
            return True
        except Exception as e:
            logger.warning(f"Failed to report job {job_id} failure: {e}")
            return False
