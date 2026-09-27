import argparse
import asyncio
import logging
import signal
from pathlib import Path

from .config import load_config
from .device_registrar import DeviceRegistrar
from .job_queue import JobQueue
from .job_store import JobStore
from .local_server import LocalServer
from .printer_manager import PrinterManager
from .system_monitor import SystemMonitor
from .template_engine import TemplateEngine
from .utils import setup_logging
from .version import __version__
from .websocket_client import WebSocketClient

logger = logging.getLogger(__name__)


class PrinterAgent:
    """Main orchestrator for the OpenEOS Printer Agent."""

    def __init__(self, config_path: str | None = None) -> None:
        self._config = load_config(config_path)
        self._shutdown_event = asyncio.Event()

        # Components (initialized in start())
        self._system_monitor: SystemMonitor | None = None
        self._device_registrar: DeviceRegistrar | None = None
        self._printer_manager: PrinterManager | None = None
        self._template_engine: TemplateEngine | None = None
        self._job_queue: JobQueue | None = None
        self._job_store: JobStore | None = None
        self._ws_client: WebSocketClient | None = None
        self._local_server: LocalServer | None = None
        self._health_check_task: asyncio.Task | None = None

        # Jobs the backend replays immediately on (re)connect can arrive before
        # the job queue + workers exist (connect happens several startup steps
        # before the queue is built). Buffer them here and drain once the queue
        # is live so replayed jobs are never silently dropped.
        self._early_jobs: list[dict] = []
        self._accepting_jobs = False

    async def start(self) -> None:
        """Start the printer agent with all components."""
        config = self._config

        # 1. Logging
        setup_logging(config.logging)
        logger.info(f"OpenEOS Printer Agent v{__version__} starting...")
        logger.info(f"Agent: {config.agent.name} ({config.agent.id})")

        # 2. Sentry (optional)
        if config.sentry.enabled and config.sentry.dsn:
            try:
                import sentry_sdk

                sentry_sdk.init(
                    dsn=config.sentry.dsn,
                    environment=config.sentry.environment,
                    traces_sample_rate=config.sentry.traces_sample_rate,
                    release=f"openeos-printer-agent@{__version__}",
                )
                logger.info("Sentry initialized")
            except Exception as e:
                logger.warning(f"Sentry init failed: {e}")

        # 3. System Monitor
        self._system_monitor = SystemMonitor()

        # 4. Device Registration - ensure we have a device_token
        self._device_registrar = DeviceRegistrar(config)
        device_token = await self._device_registrar.ensure_registered()

        # 5. Local HTTP Server (starts early to show setup/waiting page)
        if config.local_server.enabled:
            self._local_server = LocalServer(
                config=config,
                system_monitor=self._system_monitor,
                device_registrar=self._device_registrar,
            )
            await self._local_server.start()

        # 6. Wait for device verification (blocks until org is assigned)
        if self._device_registrar.status != "verified":
            logger.info("Device not yet verified, waiting for verification...")
            await self._device_registrar.wait_for_verification()

        logger.info(f"Device verified! Organization: {self._device_registrar.organization_name}")

        # 7. WebSocket Client (needs device_token, created before PrinterManager for config fetch)
        self._ws_client = WebSocketClient(
            config=config,
            device_token=device_token,
            system_monitor=self._system_monitor,
            on_print_job=self._handle_print_job,
            on_template_update=self._handle_template_update,
            on_config_update=self._handle_config_update,
            on_cash_drawer=self._handle_cash_drawer,
            on_ready=self._handle_server_ready,
        )

        # 8. Connect WebSocket
        try:
            await self._ws_client.connect()
        except Exception as e:
            logger.error(f"Initial WebSocket connection failed: {e}")
            logger.info("Will retry automatically...")

        # 9. Fetch printer configuration from backend (REST API)
        printer_configs = await self._ws_client.fetch_printer_config()

        # 10. Printer Manager (initialized with backend config)
        self._printer_manager = PrinterManager()
        await self._printer_manager.initialize(printer_configs)

        # Wire printer manager into WebSocket client for heartbeats
        self._ws_client.set_printer_manager(self._printer_manager)

        # 11. Template Engine + fetch server templates
        self._template_engine = TemplateEngine()
        templates = await self._ws_client.fetch_templates()
        if templates:
            self._template_engine.update_server_templates(templates)
        logger.info(f"Templates available: {self._template_engine.get_available_templates()}")

        # 12. Job Store (crash-safe queue persistence) + Job Queue
        job_store_path = config.job_store_file or str(Path(config.device_token_file).parent / "print-jobs.db")
        try:
            self._job_store = JobStore(job_store_path)
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

        # 13. Update local server with full components
        if self._local_server:
            self._local_server.set_components(
                printer_manager=self._printer_manager,
                job_queue=self._job_queue,
                ws_client=self._ws_client,
            )

        # 14. Periodic health checks
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

    async def _handle_print_job(self, data: dict) -> bool:
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

    async def _handle_template_update(self, data: dict) -> None:
        """Callback when templates are updated via WebSocket."""
        if self._template_engine and isinstance(data, dict):
            templates = data.get("templates", {})
            if templates:
                self._template_engine.update_server_templates(templates)

    async def _handle_cash_drawer(self, data: dict) -> None:
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

    async def _handle_config_update(self, data: dict) -> None:
        """Callback when printerConfigUpdate is received via WebSocket.

        Re-fetches printer config from backend and reconfigures PrinterManager.
        """
        logger.info("Handling printer config update...")
        if not self._ws_client or not self._printer_manager:
            return

        try:
            printer_configs = await self._ws_client.fetch_printer_config()
            await self._printer_manager.reconfigure(printer_configs)

            # Restart job queue workers for new printer set. In-memory queues
            # are discarded, but pending jobs survive in the job store and are
            # re-enqueued below.
            if self._job_queue:
                # _template_engine wird in start() gesetzt, bevor _job_queue
                # je einen Wert bekommt (Schritt 11 vor Schritt 12), und
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
                    )

            # Also re-fetch templates in case org changed
            templates = await self._ws_client.fetch_templates()
            if templates and self._template_engine:
                self._template_engine.update_server_templates(templates)

            logger.info("Printer config update applied successfully")
        except Exception as e:
            logger.error(f"Failed to apply printer config update: {e}")

    async def shutdown(self) -> None:
        """Graceful shutdown in reverse order."""
        logger.info("Shutting down Printer Agent...")

        # 1. Stop health checks
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass

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

        logger.info("Printer Agent stopped")

    async def run(self) -> None:
        """Start and run until shutdown signal."""
        await self.start()

        # Wait for shutdown
        loop = asyncio.get_event_loop()

        def _signal_handler():
            self._shutdown_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        await self._shutdown_event.wait()
        await self.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenEOS Printer Agent")
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"openeos-printer-agent {__version__}",
    )
    args = parser.parse_args()

    agent = PrinterAgent(config_path=args.config)

    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
