import asyncio
import logging
from typing import Any, Callable, Optional

import socketio

from .config import AppConfig
from .printer_manager import PrinterManager
from .system_monitor import SystemMonitor, agent_identity
from .version import get_version

logger = logging.getLogger(__name__)

CapabilitiesProvider = Callable[[], dict[str, bool]]


class WebSocketClient:
    """Socket.io client for communication with the OpenEOS backend."""

    def __init__(
        self,
        config: AppConfig,
        device_token: str,
        device_id: str,
        system_monitor: SystemMonitor,
        on_print_job: Optional[Callable[..., Any]] = None,
        on_template_update: Optional[Callable[..., Any]] = None,
        on_config_update: Optional[Callable[..., Any]] = None,
        on_cash_drawer: Optional[Callable[..., Any]] = None,
        on_discover: Optional[Callable[..., Any]] = None,
        on_ready: Optional[Callable[..., Any]] = None,
        capabilities: Optional[CapabilitiesProvider] = None,
    ) -> None:
        self._config = config
        self._device_token = device_token
        # ``agentId`` in job events; the backend identifies the socket by its
        # token anyway, the id is for log correlation.
        self._device_id = device_id
        self._system_monitor = system_monitor
        self._on_print_job = on_print_job
        self._on_template_update = on_template_update
        self._on_config_update = on_config_update
        self._on_cash_drawer = on_cash_drawer
        self._on_discover = on_discover
        self._on_ready = on_ready
        self._capabilities = capabilities

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
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            # Let the agent flush unreported job outcomes etc.
            if self._on_ready:
                asyncio.create_task(self._run_on_ready())

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

        @sio.on("printerDiscover")
        async def on_printer_discover(data: Optional[dict[str, Any]] = None) -> dict[str, bool]:
            """Web app asked for a fresh scan; ``subnetScan`` adds the port sweep."""
            subnet_scan = bool((data or {}).get("subnetScan", False))
            logger.info(f"Received discovery request from server (subnetScan={subnet_scan})")
            if self._on_discover:
                await self._on_discover(subnet_scan)
            return {"received": True}

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

    async def _wait_connected(self, timeout: float = 5.0) -> bool:
        """The server emits ``connected`` from inside its connect handler,
        which can reach us a tick before our own ``connect`` event marks the
        namespace as joined - emitting in that window fails with "/ is not
        a connected namespace". Give it a moment."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not self._connected:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return True

    async def _run_on_ready(self) -> None:
        if await self._wait_connected() and self._on_ready:
            await self._on_ready()

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeat for all printers."""
        interval = self._config.server.heartbeat_interval
        await self._wait_connected()

        while True:
            try:
                await self._send_heartbeat()
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                await asyncio.sleep(interval)

    def build_agent_heartbeat(self) -> dict[str, Any]:
        """``agentHeartbeat`` payload (spec section 4)."""
        identity = agent_identity()
        payload: dict[str, Any] = {
            "version": get_version(),
            "os": identity["os"],
            "arch": identity["arch"],
            "hostname": identity["hostname"],
            "pythonVersion": identity["pythonVersion"],
            "capabilities": self._capabilities() if self._capabilities else {},
            "printers": self._printer_manager.get_heartbeat_entries() if self._printer_manager else [],
        }
        return payload

    async def _send_heartbeat(self) -> None:
        """Send the agent heartbeat plus the (older) per-printer heartbeat."""
        if self._printer_manager:
            # printerHeartbeat predates agentHeartbeat and is kept for backends
            # that still rely on it (spec: "kann bleiben, ist dann redundant").
            for printer in self._printer_manager.get_all_printers():
                await self._sio.emit(
                    "printerHeartbeat",
                    {
                        "printerId": printer.printer_id,
                        "agentId": self._device_id,
                        "isOnline": printer.is_online,
                    },
                )

        await self._sio.emit("agentHeartbeat", self.build_agent_heartbeat())

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
                    "agentId": self._device_id,
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
                    "agentId": self._device_id,
                    "errorCode": error_code,
                    "errorMessage": error_message,
                },
            )
            logger.debug(f"Reported job {job_id} failed: {error_code}")
            return True
        except Exception as e:
            logger.warning(f"Failed to report job {job_id} failure: {e}")
            return False
