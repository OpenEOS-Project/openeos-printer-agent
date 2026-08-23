import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.config import PrinterConfig
from src.printer_manager import PrinterManager, ManagedPrinter, PrinterStatus
from src.template_engine import TemplateEngine
from src.job_queue import JobQueue, PrintJob, _normalize_keys


class TestNormalizeKeys:
    def test_camel_to_snake(self):
        data = {"jobId": "1", "printerId": "2", "templateName": "receipt"}
        result = _normalize_keys(data)
        assert result["job_id"] == "1"
        assert result["printer_id"] == "2"
        assert result["template_name"] == "receipt"

    def test_nested_dict(self):
        data = {"outerKey": {"innerKey": "value"}}
        result = _normalize_keys(data)
        assert result["outer_key"]["inner_key"] == "value"

    def test_list_of_dicts(self):
        data = {"itemList": [{"itemName": "A"}, {"itemName": "B"}]}
        result = _normalize_keys(data)
        assert result["item_list"][0]["item_name"] == "A"


class TestPrintJob:
    def test_from_camel_case(self):
        data = {
            "jobId": "job-1",
            "printerId": "printer-1",
            "templateName": "receipt",
            "payload": {"key": "value"},
            "copies": 2,
        }
        job = PrintJob(data)
        assert job.job_id == "job-1"
        assert job.printer_id == "printer-1"
        assert job.template_name == "receipt"
        assert job.copies == 2
        assert job.status == "queued"

    def test_defaults(self):
        job = PrintJob({})
        assert job.job_id == "unknown"
        assert job.copies == 1
        assert job.template_name == "receipt"


class TestJobQueue:
    @pytest.fixture
    def printer_manager(self, sample_printer_config):
        pm = PrinterManager([sample_printer_config])
        # Mock the printer as connected
        printer = pm.get_printer("printer-001")
        printer.status = PrinterStatus.ONLINE
        printer._escpos = MagicMock()
        return pm

    @pytest.fixture
    def template_engine(self):
        return TemplateEngine()

    @pytest.fixture
    def job_queue(self, printer_manager, template_engine):
        on_complete = AsyncMock()
        on_failed = AsyncMock()
        jq = JobQueue(
            printer_manager=printer_manager,
            template_engine=template_engine,
            on_job_complete=on_complete,
            on_job_failed=on_failed,
        )
        return jq

    @pytest.mark.asyncio
    async def test_enqueue_valid_job(self, job_queue, sample_print_job):
        job_queue.start_workers()
        try:
            result = await job_queue.enqueue(sample_print_job)
            assert result is True
        finally:
            await job_queue.stop_workers()

    @pytest.mark.asyncio
    async def test_enqueue_unknown_printer(self, job_queue):
        job_queue.start_workers()
        try:
            result = await job_queue.enqueue({
                "jobId": "job-x",
                "printerId": "unknown-printer",
                "templateName": "receipt",
                "payload": {},
            })
            assert result is False
        finally:
            await job_queue.stop_workers()

    @pytest.mark.asyncio
    async def test_queue_stats(self, job_queue):
        job_queue.start_workers()
        try:
            stats = job_queue.get_queue_stats()
            assert "printer-001" in stats
            assert stats["printer-001"]["completed"] == 0
            assert stats["printer-001"]["failed"] == 0
        finally:
            await job_queue.stop_workers()

    @pytest.mark.asyncio
    async def test_enqueue_reports_failure_for_missing_printer(self, printer_manager, template_engine):
        on_failed = AsyncMock()
        jq = JobQueue(
            printer_manager=printer_manager,
            template_engine=template_engine,
            on_job_failed=on_failed,
        )
        jq.start_workers()
        try:
            await jq.enqueue({
                "jobId": "job-x",
                "printerId": "nonexistent",
                "payload": {},
            })
            on_failed.assert_awaited_once()
        finally:
            await jq.stop_workers()

    @pytest.mark.asyncio
    async def test_worker_reports_failure_for_unexpected_exception(
        self, job_queue, sample_print_job
    ):
        # A bug anywhere _process_job doesn't already handle (not just the
        # printer-not-found/queue-full cases it classifies itself) must still
        # surface as a reported failure, not vanish silently with the job
        # never acknowledged either way.
        on_failed = AsyncMock()
        job_queue._on_job_failed = on_failed
        job_queue.start_workers()
        try:
            with patch.object(
                job_queue, "_process_job", side_effect=RuntimeError("boom")
            ):
                await job_queue.enqueue(sample_print_job)
                await asyncio.wait_for(
                    job_queue._queues["printer-001"].join(), timeout=2
                )
            on_failed.assert_awaited_once()
            args = on_failed.call_args.args
            assert args[1] == "WORKER_ERROR"
        finally:
            await job_queue.stop_workers()
