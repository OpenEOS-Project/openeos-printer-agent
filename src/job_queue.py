import asyncio
import logging
import time
from typing import Any, Callable, Optional

from .escpos_renderer import render_to_printer
from .job_store import JobStore
from .printer_manager import PrinterManager, PrinterStatus
from .template_engine import TemplateEngine

logger = logging.getLogger(__name__)

# Printer errors (offline, paper out, cover open, no confirmation) are
# retried with growing pauses for RETRY_WINDOW_S before the job is reported
# as failed - a printer that is switched back on, refilled or closed within
# that time prints the ticket after all. The pause before the next attempt
# ends early when the printer is seen online again (``wake_retries``).
RETRY_DELAYS = [2, 5, 10, 20, 30]  # seconds; the last one repeats
RETRY_WINDOW_S = 10 * 60
MAX_QUEUE_SIZE = 100


def _normalize_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Convert camelCase keys to snake_case."""
    import re

    def to_snake(name: str) -> str:
        s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()

    result: dict[str, Any] = {}
    for key, value in data.items():
        snake_key = to_snake(key)
        if isinstance(value, dict):
            result[snake_key] = _normalize_keys(value)
        elif isinstance(value, list):
            result[snake_key] = [_normalize_keys(v) if isinstance(v, dict) else v for v in value]
        else:
            result[snake_key] = value
    return result


class PrintJob:
    """Internal representation of a print job."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.job_id: str = data.get("jobId") or data.get("job_id", "unknown")
        self.printer_id: str = data.get("printerId") or data.get("printer_id", "")
        self.template_name: str = data.get("templateName") or data.get("template_name", "receipt")
        self.payload: dict[str, Any] = data.get("payload") or data.get("data", {})
        self.copies: int = data.get("copies", 1)
        self.attempts: int = 0
        self.status: str = "queued"
        self.error: Optional[str] = None
        self.created_at: float = time.time()
        # Local jobs (test print from the desktop window) never came from the
        # server: no job store entry, no completion/failure report.
        self.local: bool = bool(data.get("_local", False))


class JobQueue:
    """Per-printer job queues with retry logic."""

    def __init__(
        self,
        printer_manager: PrinterManager,
        template_engine: TemplateEngine,
        on_job_complete: Optional[Callable[..., Any]] = None,
        on_job_failed: Optional[Callable[..., Any]] = None,
        job_store: Optional[JobStore] = None,
    ) -> None:
        self._printer_manager = printer_manager
        self._template_engine = template_engine
        self._on_job_complete = on_job_complete
        self._on_job_failed = on_job_failed
        self._job_store = job_store
        self._queues: dict[str, asyncio.Queue[PrintJob]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._stats: dict[str, dict[str, int]] = {}
        self._wakeups: dict[str, asyncio.Event] = {}
        self._report_tasks: set[asyncio.Task[None]] = set()
        # Outcome of the most recent job per printer (desktop window: "last
        # print 14:02 ok" / the error of a failed test print).
        self._last_job: dict[str, dict[str, Any]] = {}

    def start_workers(self) -> None:
        """Start a worker task for each configured printer."""
        for printer in self._printer_manager.get_all_printers():
            pid = printer.printer_id
            self._queues[pid] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
            self._stats[pid] = {"queued": 0, "completed": 0, "failed": 0}
            self._wakeups[pid] = asyncio.Event()
            self._workers[pid] = asyncio.create_task(
                self._worker(pid),
                name=f"worker-{pid}",
            )
            logger.info(f"Started print worker for printer '{printer.name}' ({pid})")

    async def stop_workers(self) -> None:
        """Stop all worker tasks."""
        for task in list(self._workers.values()) + list(self._report_tasks):
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        logger.info("All print workers stopped")

    async def enqueue(self, job_data: dict[str, Any], persist: bool = True) -> bool:
        """Enqueue a print job. Returns False if queue is full or printer unknown.

        With a job store attached, the job is persisted before this method
        returns (crash safety) and duplicate deliveries — e.g. the server
        replaying jobs after a reconnect — are detected by job_id and acked
        without printing twice.
        """
        job = PrintJob(job_data)
        if job.local:
            persist = False

        if persist and self._job_store:
            if not self._job_store.add(job.job_id, job_data):
                if not self._handle_duplicate(job.job_id, job_data):
                    return True
                # failed before: fall through and print it again

        queue = self._queues.get(job.printer_id)

        if queue is None:
            logger.warning(f"No queue for printer {job.printer_id}, job {job.job_id} rejected")
            if not job.local:
                await self._report_failed(job.job_id, "PRINTER_NOT_FOUND", "Printer not configured on this agent")
            return False

        if queue.full():
            logger.warning(f"Queue full for printer {job.printer_id}, job {job.job_id} rejected")
            if not job.local:
                await self._report_failed(job.job_id, "QUEUE_FULL", "Print queue is full")
            return False

        await queue.put(job)
        self._stats[job.printer_id]["queued"] += 1
        logger.info(f"Job {job.job_id} queued for printer {job.printer_id}")
        return True

    async def load_persisted(self) -> None:
        """Re-enqueue jobs that survived a crash/restart in the job store."""
        if not self._job_store:
            return
        jobs = self._job_store.get_pending()
        for job_data in jobs:
            await self.enqueue(job_data, persist=False)
        if jobs:
            logger.info(f"Restored {len(jobs)} persisted print job(s)")

    def _handle_duplicate(self, job_id: str, job_data: dict[str, Any]) -> bool:
        """The server delivered a job we already have. Returns True when it
        has to be printed (again).

        * still pending: it is in the queue - nothing to do;
        * completed: never print twice - send the completion once more, the
          server evidently did not get it;
        * failed: print again. The server re-sends a failed job only on a
          manual retry in the web app, which reuses the job id.
        """
        assert self._job_store is not None
        row = self._job_store.get(job_id)
        if row is None or row["status"] == "pending":
            logger.info(f"Job {job_id} already queued - duplicate delivery ignored")
            return False
        if row["status"] == "failed":
            logger.info(f"Job {job_id} failed before and was sent again (retry) - printing it again")
            self._job_store.requeue(job_id, job_data)
            return True
        logger.info(f"Job {job_id} was already printed - not printing again, re-sending the completion")
        task = asyncio.create_task(
            self._send_complete(job_id, int(row.get("attempts") or 0), row.get("finished_at")),
            name=f"re-report-{job_id}",
        )
        self._report_tasks.add(task)
        task.add_done_callback(self._report_tasks.discard)
        return False

    def wake_retries(self, printer_id: Optional[str] = None) -> None:
        """End the pause of jobs waiting for a retry (printer back online,
        computer woke up)."""
        for pid, event in self._wakeups.items():
            if printer_id is None or pid == printer_id:
                event.set()

    async def _report_complete(self, job_id: str, attempts: int = 1) -> None:
        """Persist the outcome, then report it. The row stays (for duplicate
        detection) and is marked reported only after the server's ack."""
        if self._job_store:
            self._job_store.mark_outcome(job_id, "completed", attempts=attempts)
        await self._send_complete(job_id, attempts, time.time())

    async def _send_complete(self, job_id: str, attempts: int, printed_at: Optional[float]) -> None:
        reported = False
        if self._on_job_complete:
            reported = bool(await self._on_job_complete(job_id, attempts=attempts, printed_at=printed_at))
        if reported and self._job_store:
            self._job_store.mark_reported(job_id)

    async def _report_failed(self, job_id: str, error_code: str, error_message: str, attempts: int = 0) -> None:
        if self._job_store:
            self._job_store.mark_outcome(job_id, "failed", error_code, error_message, attempts=attempts)
        reported = False
        if self._on_job_failed:
            reported = bool(await self._on_job_failed(job_id, error_code, error_message, attempts=attempts))
        if reported and self._job_store:
            self._job_store.mark_reported(job_id)

    async def _report_job_complete(self, job: PrintJob) -> None:
        if not job.local:
            await self._report_complete(job.job_id, attempts=job.attempts)

    async def _report_job_failed(self, job: PrintJob, error_code: str, error_message: str) -> None:
        if not job.local:
            await self._report_failed(job.job_id, error_code, error_message, attempts=job.attempts)

    async def _worker(self, printer_id: str) -> None:
        """Worker loop: process jobs from the queue for a specific printer."""
        queue = self._queues[printer_id]

        while True:
            try:
                job: PrintJob = await queue.get()
            except asyncio.CancelledError:
                break

            try:
                await self._process_job(printer_id, job)
            except asyncio.CancelledError:
                queue.task_done()
                raise
            except Exception as e:
                # _process_job already reports the failures it knows how to
                # classify (printer not found, render error, etc.) — this is
                # the catch-all for anything that slips past that, e.g. a bug
                # in _report_complete/_report_failed itself. Without this,
                # the job was silently dropped: no retry, no failure report,
                # nobody notified a print job vanished.
                logger.error(f"Worker error for printer {printer_id}, job {job.job_id}: {e}")
                try:
                    await self._report_job_failed(job, "WORKER_ERROR", str(e))
                except Exception as report_err:
                    logger.error(f"Also failed to report failure for job {job.job_id}: {report_err}")
            finally:
                queue.task_done()

    async def _process_job(self, printer_id: str, job: PrintJob) -> None:
        """Render once, then print with retries while the printer has trouble."""
        printer = self._printer_manager.get_printer(printer_id)
        if printer is None:
            logger.error(f"Printer {printer_id} not found for job {job.job_id}")
            await self._report_job_failed(job, "PRINTER_NOT_FOUND", "Printer not found")
            self._stats[printer_id]["failed"] += 1
            return

        try:
            rendered = self._template_engine.render(
                job.template_name,
                {**job.payload, "paper_width": printer.paper_width, "columns": printer.columns},
            )
        except Exception as e:
            # Retrying cannot fix a broken template or payload.
            job.status = "failed"
            job.error = str(e) or type(e).__name__
            self._stats[printer_id]["failed"] += 1
            self._remember(printer_id, job)
            logger.error(f"Job {job.job_id} could not be rendered: {job.error}")
            await self._report_job_failed(job, "TEMPLATE_ERROR", job.error)
            return

        # A named function instead of a lambda here, because mypy cannot infer
        # parameter types for a lambda that carries extra defaulted arguments
        # (the r=/o= trick binds the current values instead of late binding).
        def _do_print(p: Any, r: str = rendered, o: dict[str, Any] = {"copies": job.copies}) -> None:
            render_to_printer(p, r, o)

        started = time.monotonic()
        wakeup = self._wakeups.setdefault(printer_id, asyncio.Event())
        while True:
            job.attempts += 1
            job.status = "printing"
            try:
                await printer.execute(_do_print)
            except Exception as e:
                error_msg = printer.last_error or str(e) or type(e).__name__
                error_code = printer.last_error_code.value if printer.last_error_code else "UNKNOWN"
                elapsed = time.monotonic() - started
                if elapsed >= RETRY_WINDOW_S:
                    job.status = "failed"
                    job.error = error_msg
                    self._stats[printer_id]["failed"] += 1
                    self._remember(printer_id, job)
                    logger.error(
                        f"Job {job.job_id} failed after {job.attempts} attempts in {elapsed:.0f}s: {error_msg}"
                    )
                    await self._report_job_failed(job, error_code, error_msg)
                    return
                delay = RETRY_DELAYS[min(job.attempts - 1, len(RETRY_DELAYS) - 1)]
                logger.warning(
                    f"Job {job.job_id} attempt {job.attempts} failed ({error_code}: {error_msg}); retrying in {delay}s"
                )
                wakeup.clear()
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=delay)
                    logger.info(f"Retrying job {job.job_id} now (printer back / computer woke up)")
                except asyncio.TimeoutError:
                    pass
                # USB/spooler handles go bad with the device; network printers
                # open a fresh connection per job anyway.
                if not printer.uses_transport and printer.status in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                    await printer.reconnect()
                continue

            job.status = "completed"
            self._stats[printer_id]["completed"] += 1
            self._remember(printer_id, job)
            logger.info(f"Job {job.job_id} completed on printer '{printer.name}'")
            await self._report_job_complete(job)
            return

    def _remember(self, printer_id: str, job: PrintJob) -> None:
        self._last_job[printer_id] = {
            "jobId": job.job_id,
            "status": job.status,
            "finishedAt": time.time(),
            "error": job.error,
            "local": job.local,
        }

    def get_queue_stats(self) -> dict[str, Any]:
        """Get statistics for all printer queues."""
        result: dict[str, Any] = {}
        for printer_id, stats in self._stats.items():
            queue = self._queues.get(printer_id)
            result[printer_id] = {
                **stats,
                "pending": queue.qsize() if queue else 0,
                "lastJob": self._last_job.get(printer_id),
            }
        return result
