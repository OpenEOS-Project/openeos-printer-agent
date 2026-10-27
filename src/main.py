import asyncio
import logging
import signal
import sys
from typing import Any, Optional

from .api_client import DeviceApiClient
from .config import AppConfig, load_config
from .device_registrar import DeviceRegistrar
from .discovery import DiscoveryService
from .job_queue import JobQueue
from .job_store import JobStore
from .local_server import LocalServer
from .printer_manager import PrinterManager
from .system_monitor import SystemMonitor
from .template_engine import TemplateEngine
from .utils import setup_logging
from .version import get_version
from .websocket_client import WebSocketClient

logger = logging.getLogger(__name__)


class PrinterAgent:
    """Main orchestrator for the OpenEOS Printer Agent."""

    def __init__(self, config_path: Optional[str] = None, config: Optional[AppConfig] = None) -> None:
        self._config = config if config is not None else load_config(config_path)
        self._shutdown_event = asyncio.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # Components (initialized in start())
        self._system_monitor: Optional[SystemMonitor] = None
        self._device_registrar: Optional[DeviceRegistrar] = None
        self._api_client: Optional[DeviceApiClient] = None
        self._printer_manager: Optional[PrinterManager] = None
        self._template_engine: Optional[TemplateEngine] = None
        self._job_queue: Optional[JobQueue] = None
        self._job_store: Optional[JobStore] = None
        self._ws_client: Optional[WebSocketClient] = None
        self._local_server: Optional[LocalServer] = None
        self._discovery: Optional[DiscoveryService] = None
        self._health_check_task: Optional[asyncio.Task[None]] = None

        # Jobs the backend replays immediately on (re)connect can arrive before
        # the job queue + workers exist (connect happens several startup steps
        # before the queue is built). Buffer them here and drain once the queue
        # is live so replayed jobs are never silently dropped.
        self._early_jobs: list[dict[str, Any]] = []
        self._accepting_jobs = False

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def discovery(self) -> Optional[DiscoveryService]:
        return self._discovery

    @property
    def printer_manager(self) -> Optional[PrinterManager]:
        return self._printer_manager

    async def start(self) -> None:
        """Start the printer agent with all components."""
        config = self._config

        # 1. Logging
        setup_logging(config)
        logger.info(f"OpenEOS Printer Agent v{get_version()} starting...")
        logger.info(f"Server: {config.server.url}")
        logger.info(f"Data directory: {config.data_path}")

        # 2. System Monitor
        self._system_monitor = SystemMonitor()

        # 3. Local HTTP Server (starts early to show the pairing code)
        if config.local_server.enabled:
            self._local_server = LocalServer(
                config=config,
                system_monitor=self._system_monitor,
            )
            try:
                await self._local_server.start()
            except OSError as e:
                # Port taken (second instance, another app): the agent still
                # works, only the status page is missing.
                logger.warning(f"Local status server not started: {e}")
                self._local_server = None

        # 4. Device Registration - ensure we have a device_token
        self._device_registrar = DeviceRegistrar(config)
        if self._local_server:
            self._local_server.set_device_registrar(self._device_registrar)
        device_token = await self._device_registrar.ensure_registered()

        # 5. Wait for device verification (blocks until org is assigned)
        if self._device_registrar.status != "verified":
            logger.info("Device not yet verified, waiting for verification...")
            await self._device_registrar.wait_for_verification()

        logger.info(f"Device verified! Organization: {self._device_registrar.organization_name}")
        device_id = self._device_registrar.device_id or "unknown"

        # 6. REST client for the device API
        self._api_client = DeviceApiClient(config.server.url, device_token)

        # 7. Discovery service (started once the socket is up, see below)
        self._discovery = DiscoveryService(config.discovery, reporter=self._api_client.report_discovered)

        # 8. WebSocket Client
        self._ws_client = WebSocketClient(
            config=config,
            device_token=device_token,
            device_id=device_id,
            system_monitor=self._system_monitor,
            on_print_job=self._handle_print_job,
            on_template_update=self._handle_template_update,
            on_config_update=self._handle_config_update,
            on_cash_drawer=self._handle_cash_drawer,
            on_discover=self._handle_discover,
            on_ready=self._handle_server_ready,
            capabilities=self._discovery.capabilities,
        )

        # 9. Connect WebSocket
        try:
            await self._ws_client.connect()
        except Exception as e:
            logger.error(f"Initial WebSocket connection failed: {e}")
            logger.info("Will retry automatically...")

        # 10. Fetch the adopted printers from the backend (REST API)
        printer_configs = await self._api_client.get_printers()

        # 11. Printer Manager (initialized with backend config)
        self._printer_manager = PrinterManager(dummy=config.dummy_printer)
        await self._printer_manager.initialize(printer_configs)

        # Wire printer manager into WebSocket client for heartbeats
        self._ws_client.set_printer_manager(self._printer_manager)

        # 12. Template Engine + fetch server templates
        self._template_engine = TemplateEngine(cache_dir=config.template_cache_path)
        templates = await self._api_client.get_templates()
        if templates:
            self._template_engine.update_server_templates(templates)
        logger.info(f"Templates available: {self._template_engine.get_available_templates()}")

        # 13. Job Store (crash-safe queue persistence) + Job Queue
        try:
            self._job_store = JobStore(str(config.job_store_path))
        except Exception as e:
            logger.warning(f"Job store unavailable ({e}) — print jobs are not crash-safe")
            self._job_store = None

        self._job_queue = JobQueue(
            printer_manager=self._printer_manager,
            template_engine=self._template_engine,
            on_job_complete=self._ws_client.report_job_complete,
            on_job_failed=self._ws_client.report_job_failed,
            job_store=self._job_store,
        )
        self._job_queue.start_workers()
        # Re-enqueue jobs that survived a crash/restart
        await self._job_queue.load_persisted()
        # Queue + workers are live — accept jobs and flush anything the backend
        # replayed while we were still starting up.
        await self._drain_early_jobs()
        # The socket's "connected" event fired before the job store existed
        # (step 9 vs. 13), so outcomes stored by a previous run have not been
        # delivered yet - do it now instead of waiting for the next reconnect.
        await self._handle_server_ready()

        # 14. Update local server with full components
        if self._local_server:
            self._local_server.set_components(
                printer_manager=self._printer_manager,
                job_queue=self._job_queue,
                ws_client=self._ws_client,
                discovery=self._discovery,
            )

        # 15. Discovery: first scan now, then every 5 minutes / on request
        self._discovery.start()

        # 16. Periodic health checks
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        logger.info("Printer Agent fully started")

    async def _health_check_loop(self) -> None:
        """Periodic printer health checks (every 60s)."""
        while True:
            try:
                await asyncio.sleep(60)
                if self._printer_manager:
                    await self._printer_manager.health_check_all()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")

    async def _handle_print_job(self, data: dict[str, Any]) -> bool:
        """Callback when a print job is received via WebSocket.

        The return value becomes the socket.io ack: True only after the job
        has been accepted (and persisted, if a job store is available).
        """
        if self._job_queue and self._accepting_jobs:
            return await self._job_queue.enqueue(data)
        # Arrived before the queue/workers are ready (backend replays queued
        # jobs immediately on connect). Buffer and drain once the queue is up,
        # acking as accepted since the job will be processed.
        logger.info(f"Buffering print job {data.get('jobId', 'unknown')} until queue is ready")
        self._early_jobs.append(data)
        return True

    async def _drain_early_jobs(self) -> None:
        """Mark the queue ready and enqueue any jobs buffered during startup."""
        self._accepting_jobs = True
        if not self._early_jobs:
            return
        buffered = self._early_jobs
        self._early_jobs = []
        logger.info(f"Draining {len(buffered)} buffered print job(s) into the queue")
        for job in buffered:
            if self._job_queue:
                await self._job_queue.enqueue(job)

    async def _handle_server_ready(self) -> None:
        """After (re)connecting: deliver job outcomes that could not be
        reported while offline, so the server never re-sends printed jobs."""
        if not self._job_store or not self._ws_client:
            return
        for row in self._job_store.get_unreported():
            job_id = row["job_id"]
            if row["status"] == "completed":
                reported = await self._ws_client.report_job_complete(job_id)
            else:
                reported = await self._ws_client.report_job_failed(
                    job_id,
                    row.get("error_code") or "UNKNOWN",
                    row.get("error_message") or "Unknown error",
                )
            if reported:
                self._job_store.remove(job_id)
                logger.info(f"Delivered stored outcome for job {job_id}")

    async def _handle_template_update(self, data: dict[str, Any]) -> None:
        """Callback when templates are updated via WebSocket."""
        if self._template_engine and isinstance(data, dict):
            templates = data.get("templates", {})
            if templates:
                self._template_engine.update_server_templates(templates)

    async def _handle_cash_drawer(self, data: dict[str, Any]) -> None:
        """Callback when an openCashDrawer event is received via WebSocket."""
        if not self._printer_manager:
            return

        printer_id = data.get("printerId")
        if not printer_id:
            logger.warning("openCashDrawer event missing printerId")
            return

        printer = self._printer_manager.get_printer(printer_id)
        if not printer:
            logger.warning(f"Printer {printer_id} not found for cash drawer open")
            return

        pin = self._config.cash_drawer_pin
        try:
            await printer.execute(lambda p: p.cashdraw(pin))
            logger.info(f"Cash drawer opened on printer {printer_id} (kick pin {pin})")
        except Exception as e:
            logger.error(f"Failed to open cash drawer on printer {printer_id}: {e}")

    async def _handle_discover(self, subnet_scan: bool) -> None:
        """``printerDiscover`` from the server: queue an immediate scan."""
        if self._discovery:
            self._discovery.trigger(subnet_scan=subnet_scan)

    async def _handle_config_update(self, data: dict[str, Any]) -> None:
        """Callback when printerConfigUpdate is received via WebSocket.

        Re-fetches the adopted printers from the backend and reconfigures
        the PrinterManager.
        """
        logger.info("Handling printer config update...")
        if not self._api_client or not self._ws_client or not self._printer_manager:
            return

        try:
            printer_configs = await self._api_client.get_printers()
            await self._printer_manager.reconfigure(printer_configs)

            # Restart job queue workers for new printer set. In-memory queues
            # are discarded, but pending jobs survive in the job store and are
            # re-enqueued below.
            if self._job_queue:
                # _template_engine wird in start() gesetzt, bevor _job_queue
                # je einen Wert bekommt (Schritt 12 vor Schritt 13), und
                # danach nie wieder auf None zurückgesetzt. Ist _job_queue
                # hier gesetzt, MUSS _template_engine es also auch sein - die
                # explizite Prüfung macht das für mypy sichtbar und wandelt
                # eine sonst stille Annahme in einen lauten Fehler, falls die
                # Startreihenfolge sich je ändert, statt erst beim Rendern
                # eines Druckjobs mit einem fehlenden Template zu crashen.
                template_engine = self._template_engine
                if template_engine is None:
                    raise RuntimeError("Template engine not initialized; cannot rebuild job queue")

                self._accepting_jobs = False
                await self._job_queue.stop_workers()
                self._job_queue = JobQueue(
                    printer_manager=self._printer_manager,
                    template_engine=template_engine,
                    on_job_complete=self._ws_client.report_job_complete,
                    on_job_failed=self._ws_client.report_job_failed,
                    job_store=self._job_store,
                )
                self._job_queue.start_workers()
                await self._job_queue.load_persisted()
                await self._drain_early_jobs()

                # Update local server reference
                if self._local_server:
                    self._local_server.set_components(
                        printer_manager=self._printer_manager,
                        job_queue=self._job_queue,
                        ws_client=self._ws_client,
                        discovery=self._discovery,
                    )

            # Also re-fetch templates in case org changed
            templates = await self._api_client.get_templates()
            if templates and self._template_engine:
                self._template_engine.update_server_templates(templates)

            logger.info("Printer config update applied successfully")
        except Exception as e:
            logger.error(f"Failed to apply printer config update: {e}")

    async def shutdown(self) -> None:
        """Graceful shutdown in reverse order."""
        logger.info("Shutting down Printer Agent...")

        # 1. Stop health checks + discovery
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        if self._discovery:
            await self._discovery.stop()

        # 2. Disconnect WebSocket
        if self._ws_client:
            await self._ws_client.disconnect()

        # 3. Stop job queue workers
        if self._job_queue:
            await self._job_queue.stop_workers()

        # 4. Stop HTTP server
        if self._local_server:
            await self._local_server.stop()

        # 5. Disconnect printers
        if self._printer_manager:
            await self._printer_manager.shutdown()

        # 6. Close job store (pending jobs stay on disk for the next start)
        if self._job_store:
            self._job_store.close()

        # 7. Close the REST session
        if self._api_client:
            await self._api_client.close()

        logger.info("Printer Agent stopped")

    # -- shutdown requests ---------------------------------------------------

    def request_shutdown(self) -> None:
        """Ask the running agent to stop (call from the event loop thread)."""
        self._shutdown_event.set()

    def request_shutdown_threadsafe(self) -> None:
        """Ask the running agent to stop from another thread (Windows
        service control handler, signal handlers on Windows)."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._shutdown_event.set)

    def _install_signal_handlers(self) -> None:
        """SIGTERM/SIGINT -> graceful shutdown, on every platform.

        ``loop.add_signal_handler`` is Unix-only (NotImplementedError on
        Windows' ProactorEventLoop). There we fall back to ``signal.signal``
        and hop back onto the loop thread via call_soon_threadsafe.
        """
        loop = asyncio.get_running_loop()
        signals = [signal.SIGTERM, signal.SIGINT]
        if sys.platform.startswith("win") and hasattr(signal, "SIGBREAK"):
            signals.append(signal.SIGBREAK)

        try:
            for sig in signals:
                loop.add_signal_handler(sig, self.request_shutdown)
            return
        except (NotImplementedError, RuntimeError, AttributeError):
            pass

        def _handler(signum: int, frame: Any) -> None:
            self.request_shutdown_threadsafe()

        for sig in signals:
            try:
                signal.signal(sig, _handler)
            except (ValueError, OSError):
                # Not the main thread (Windows service worker): the service
                # control handler stops us instead.
                pass

    async def run(self) -> None:
        """Start and run until shutdown signal."""
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()

        start_task = asyncio.create_task(self.start())
        stop_task = asyncio.create_task(self._shutdown_event.wait())
        # Shutdown must also work while start() still waits for pairing or
        # for an unreachable server - hence the race instead of a plain await.
        done, _ = await asyncio.wait({start_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
        if start_task in done:
            exc = start_task.exception()
            if exc is not None:
                logger.error(f"Startup failed: {exc}")
                await self.shutdown()
                raise exc
            await self._shutdown_event.wait()
        else:
            start_task.cancel()
            try:
                await start_task
            except (asyncio.CancelledError, Exception):
                pass
        stop_task.cancel()
        await self.shutdown()


def main() -> None:
    """Entry point for ``python -m src.main``; delegates to the CLI."""
    from .cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
