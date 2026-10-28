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
# Called (synchronously, from inside a socket event) when the server turns
# the connection away or reports a status change; the agent decides what
# that means by asking GET /devices/status. ``urgent`` marks the one event
# that must not be throttled: the server hung up on an authenticated
# session (e.g. "Agent entfernen" in the web app).
AuthErrorCallback = Callable[[str, bool], None]

# python-socketio does not reconnect after a server-initiated disconnect
# ("io server disconnect"), so the client has to do it itself: first wait
# this long (a removal check should be prompt, a restarting server needs a
# moment), then double the delay up to ``reconnect_max_interval``.
SERVER_DISCONNECT_RETRY_S = 2.0
SERVER_DISCONNECT = socketio.AsyncClient.reason.SERVER_DISCONNECT


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
        on_auth_error: Optional[AuthErrorCallback] = None,
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
        self._on_auth_error = on_auth_error

        self._sio = self._new_client()
        self._connected = False
        self._heartbeat_task: Optional[asyncio.Task[None]] = None
        self._server_config: dict[str, Any] = {}
        # True once the server confirmed the token (``connected`` event);
        # a server-side hang-up of such a session is worth an immediate
        # status check, repeated kicks of a stale token are not.
        self._authenticated = False
        # Set by disconnect(): a disconnect we asked for must not trigger
        # the reconnect loop.
        self._closing = False
        self._reconnect_task: Optional[asyncio.Task[None]] = None
        self._reconnect_wanted = False

        # Set lazily after PrinterManager is created
        self._printer_manager: Optional[PrinterManager] = None

        self._register_handlers()

    def _new_client(self) -> socketio.AsyncClient:
        return socketio.AsyncClient(
            reconnection=True,
            reconnection_attempts=0,  # unlimited
            reconnection_delay=self._config.server.reconnect_interval,
            reconnection_delay_max=self._config.server.reconnect_max_interval,
            logger=False,
        )

    def set_printer_manager(self, printer_manager: PrinterManager) -> None:
        """Set the printer manager after it is initialized."""
        self._printer_manager = printer_manager

    def _auth_error(self, reason: str, urgent: bool = False) -> None:
        if self._on_auth_error:
            self._on_auth_error(reason, urgent)

    def _register_handlers(self) -> None:
        sio = self._sio

        @sio.event
        async def connect() -> None:
            self._connected = True
            logger.info(f"Connected to server: {self._config.server.url}")

        @sio.event
        async def connect_error(data: Any = None) -> None:
            # Fires both when the server actively refuses the namespace
            # (CONNECT_ERROR packet, e.g. unknown token) and when the
            # transport fails. The agent sorts that out with one status call.
            logger.warning(f"Connection refused by server: {data}")
            self._auth_error(f"socket connect_error: {data}")

        @sio.event
        async def disconnect(reason: str = "") -> None:
            self._connected = False
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None
            if reason == SERVER_DISCONNECT and not self._closing:
                # The library gives up here (no reconnect loop, no
                # connect_error): removed/blocked in the web app, or the
                # server closed sockets on a restart. Find out which.
                logger.warning("Server closed the connection - checking the device status and reconnecting")
                self._schedule_reconnect()
                return
            logger.warning(f"Disconnected from server ({reason or 'unknown reason'})")

        @sio.on("connected")
        async def on_connected(data: dict[str, Any]) -> None:
            logger.info(f"Server confirmed authentication: {data}")
            self._authenticated = True
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            # Let the agent flush unreported job outcomes etc.
            if self._on_ready:
                asyncio.create_task(self._run_on_ready())

        @sio.on("error")
        async def on_error(data: dict[str, Any]) -> None:
            # The gateway answers an unknown/blocked token with
            # {"message": "Authentication required"} and then drops us.
            logger.error(f"Server error: {data}")
            message = str(data.get("message", "")) if isinstance(data, dict) else str(data)
            if "auth" in message.lower():
                self._auth_error(f"server error: {message}")

        @sio.on("deviceStatusChanged")
        async def on_device_status_changed(data: dict[str, Any]) -> None:
            """Admin blocked/unblocked this device in the web app."""
            status = str(data.get("status", "")) if isinstance(data, dict) else ""
            logger.info(f"Server reports device status: {status or data}")
            if status and status != "verified":
                self._auth_error(f"deviceStatusChanged: {status}")

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
        self._closing = False

        await self._sio.connect(
            url,
            auth={"deviceToken": self._device_token},
            transports=["websocket"],
            wait_timeout=10,
        )

    async def disconnect(self) -> None:
        """Disconnect from server and stop any pending reconnect attempts."""
        self._closing = True
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        task = self._reconnect_task
        self._reconnect_task = None
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            # shutdown() also aborts the library's reconnect loop, which a
            # plain disconnect() does not when we are currently offline. The
            # loop creates its abort event on its first tick - give a task
            # that was scheduled just now that tick before asking it to stop.
            await asyncio.sleep(0)
            await self._sio.shutdown()
            if self._sio.eio.state == "connected" and not self._sio.connected:
                # A connect() that was cancelled between the namespace join
                # and the library's own "connected" flag leaves the
                # transport open; shutdown() does not know about it.
                await self._sio.disconnect()
        except Exception as e:
            logger.debug(f"Socket shutdown: {e}")
        self._connected = False

    async def reconnect_with_token(self, device_token: str, device_id: str) -> None:
        """After re-pairing: drop the old socket (and its reconnect loop)
        and connect again with the new token.

        A fresh AsyncClient is used on purpose - python-socketio keeps a
        finished reconnect task around after ``shutdown()``, which would
        stop it from reconnecting after the next drop.
        """
        await self.disconnect()
        self._device_token = device_token
        self._device_id = device_id
        self._sio = self._new_client()
        self._register_handlers()
        await self.connect()

    async def wait(self) -> None:
        """Wait until disconnected."""
        await self._sio.wait()

    def _schedule_reconnect(self) -> None:
        self._reconnect_wanted = True
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect_after_server_disconnect(), name="ws-reconnect")

    async def _reconnect_after_server_disconnect(self) -> None:
        """Our replacement for the library's reconnect loop after the server
        hung up. Every round first asks the agent to check the device status
        (removed -> re-pair, blocked -> wait; both end this loop through
        disconnect()), then connects again. A still-verified device simply
        comes back; an unreachable server keeps the loop going with backoff.
        Only the first check after an authenticated session is urgent, a
        stale token that gets kicked on every attempt is throttled by the
        agent like any other repeated rejection."""
        delay = SERVER_DISCONNECT_RETRY_S
        urgent = self._authenticated
        self._authenticated = False
        while self._reconnect_wanted and not self._closing:
            self._reconnect_wanted = False
            await asyncio.sleep(delay)
            if self._closing:
                return
            self._auth_error("server closed the connection", urgent=urgent)
            urgent = False
            delay = min(delay * 2, self._config.server.reconnect_max_interval)
            try:
                await self.connect()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if self._closing:
                    return
                logger.warning(f"Reconnect failed ({e}); next attempt in {delay:.0f}s")
                self._reconnect_wanted = True

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
