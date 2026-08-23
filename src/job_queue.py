import asyncio
import logging
import time
from typing import Callable, Optional

from .escpos_renderer import render_to_printer
from .job_store import JobStore
from .printer_manager import PrinterManager, PrinterStatus
from .template_engine import TemplateEngine

logger = logging.getLogger(__name__)

RETRY_DELAYS = [2, 10, 30]  # seconds
MAX_QUEUE_SIZE = 100


def _normalize_keys(data: dict) -> dict:
    """Convert camelCase keys to snake_case."""
    import re

    def to_snake(name: str) -> str:
        s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
        return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s1).lower()

    result = {}
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

    def __init__(self, data: dict) -> None:
        self.job_id: str = data.get("jobId") or data.get("job_id", "unknown")
        self.printer_id: str = data.get("printerId") or data.get("printer_id", "")
        self.template_name: str = data.get("templateName") or data.get("template_name", "receipt")
        self.payload: dict = data.get("payload") or data.get("data", {})
        self.copies: int = data.get("copies", 1)
        self.attempts: int = 0
        self.status: str = "queued"
        self.error: Optional[str] = None
        self.created_at: float = time.time()


class JobQueue:
    """Per-printer job queues with retry logic."""

    def __init__(
        self,
        printer_manager: PrinterManager,
        template_engine: TemplateEngine,
        on_job_complete: Optional[Callable] = None,
        on_job_failed: Optional[Callable] = None,
        job_store: Optional[JobStore] = None,
    ) -> None:
        self._printer_manager = printer_manager
        self._template_engine = template_engine
        self._on_job_complete = on_job_complete
        self._on_job_failed = on_job_failed
        self._job_store = job_store
        self._queues: dict[str, asyncio.Queue] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self._stats: dict[str, dict] = {}

    def start_workers(self) -> None:
        """Start a worker task for each configured printer."""
        for printer in self._printer_manager.get_all_printers():
            pid = printer.printer_id
            self._queues[pid] = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)
            self._stats[pid] = {"queued": 0, "completed": 0, "failed": 0}
            self._workers[pid] = asyncio.create_task(
                self._worker(pid),
                name=f"worker-{pid}",
            )
            logger.info(f"Started print worker for printer '{printer.name}' ({pid})")

    async def stop_workers(self) -> None:
        """Stop all worker tasks."""
        for task in self._workers.values():
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers.values(), return_exceptions=True)
        self._workers.clear()
        logger.info("All print workers stopped")

    async def enqueue(self, job_data: dict, persist: bool = True) -> bool:
        """Enqueue a print job. Returns False if queue is full or printer unknown.

        With a job store attached, the job is persisted before this method
        returns (crash safety) and duplicate deliveries — e.g. the server
        replaying jobs after a reconnect — are detected by job_id and acked
        without printing twice.
        """
        job = PrintJob(job_data)

        if persist and self._job_store:
            if not self._job_store.add(job.job_id, job_data):
                logger.info(f"Job {job.job_id} already known — duplicate delivery ignored")
                return True

        queue = self._queues.get(job.printer_id)

        if queue is None:
            logger.warning(f"No queue for printer {job.printer_id}, job {job.job_id} rejected")
            await self._report_failed(job.job_id, "PRINTER_NOT_FOUND", "Printer not configured on this agent")
            return False

        if queue.full():
            logger.warning(f"Queue full for printer {job.printer_id}, job {job.job_id} rejected")
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

    async def _report_complete(self, job_id: str) -> None:
        """Report success to the server; keep the outcome locally if offline."""
        reported = False
        if self._on_job_complete:
            reported = bool(await self._on_job_complete(job_id))
        if self._job_store:
            if reported:
                self._job_store.remove(job_id)
            else:
                self._job_store.mark_outcome(job_id, "completed")

    async def _report_failed(self, job_id: str, error_code: str, error_message: str) -> None:
        """Report failure to the server; keep the outcome locally if offline."""
        reported = False
        if self._on_job_failed:
            reported = bool(await self._on_job_failed(job_id, error_code, error_message))
        if self._job_store:
            if reported:
                self._job_store.remove(job_id)
            else:
                self._job_store.mark_outcome(job_id, "failed", error_code, error_message)

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
                    await self._report_failed(job.job_id, "WORKER_ERROR", str(e))
                except Exception as report_err:
                    logger.error(f"Also failed to report failure for job {job.job_id}: {report_err}")
            finally:
                queue.task_done()

    async def _process_job(self, printer_id: str, job: PrintJob) -> None:
        """Process a single print job with retry logic."""
        printer = self._printer_manager.get_printer(printer_id)
        if printer is None:
            logger.error(f"Printer {printer_id} not found for job {job.job_id}")
            await self._report_failed(job.job_id, "PRINTER_NOT_FOUND", "Printer not found")
            self._stats[printer_id]["failed"] += 1
            return

        max_attempts = len(RETRY_DELAYS) + 1

        while job.attempts < max_attempts:
            job.attempts += 1
            job.status = "printing"

            try:
                # Render template
                rendered = self._template_engine.render(
                    job.template_name,
                    {**job.payload, "paper_width": printer.paper_width},
                )

                # Send to printer
                await printer.execute(
                    lambda p, r=rendered, o={"copies": job.copies}: render_to_printer(p, r, o)
                )

                # Success
                job.status = "completed"
                self._stats[printer_id]["completed"] += 1
                logger.info(f"Job {job.job_id} completed on printer '{printer.name}'")

                await self._report_complete(job.job_id)
                return

            except Exception as e:
                error_msg = str(e)
                error_code = printer.last_error_code.value if printer.last_error_code else "UNKNOWN"
                logger.warning(
                    f"Job {job.job_id} attempt {job.attempts}/{max_attempts} failed: {error_msg}"
                )

                if job.attempts < max_attempts:
                    delay = RETRY_DELAYS[job.attempts - 1]
                    logger.info(f"Retrying job {job.job_id} in {delay}s...")
                    await asyncio.sleep(delay)

                    # Try reconnecting printer
                    if printer.status in (PrinterStatus.ERROR, PrinterStatus.OFFLINE):
                        await printer.reconnect()
                else:
                    # All attempts exhausted
                    job.status = "failed"
                    job.error = error_msg
                    self._stats[printer_id]["failed"] += 1
                    logger.error(f"Job {job.job_id} failed after {max_attempts} attempts: {error_msg}")

                    await self._report_failed(job.job_id, error_code, error_msg)

    def get_queue_stats(self) -> dict:
        """Get statistics for all printer queues."""
        result = {}
        for printer_id, stats in self._stats.items():
            queue = self._queues.get(printer_id)
            result[printer_id] = {
                **stats,
                "pending": queue.qsize() if queue else 0,
            }
        return result
