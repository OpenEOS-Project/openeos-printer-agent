from unittest.mock import AsyncMock, MagicMock

from src.main import PrinterAgent


class TestHandleConfigUpdateTemplateEngineGuard:
    """Covers the #7 fix in PrinterAgent._handle_config_update.

    Startup order (see PrinterAgent.start()) guarantees _template_engine is
    assigned before _job_queue ever is, and it is never reset to None
    afterwards - so in practice this branch can't run with a None template
    engine. The guard exists so a future refactor that breaks that invariant
    fails loudly (a caught, logged RuntimeError) instead of constructing a
    JobQueue with template_engine=None, which would only surface later as a
    crash while rendering a print job.
    """

    async def test_raises_and_is_caught_when_template_engine_missing(self, caplog):
        agent = PrinterAgent(config_path=None)

        agent._ws_client = MagicMock()
        agent._ws_client.fetch_printer_config = AsyncMock(return_value=[])
        agent._printer_manager = MagicMock()
        agent._printer_manager.reconfigure = AsyncMock()
        agent._job_queue = MagicMock()  # truthy -> enters the rebuild branch
        agent._job_queue.stop_workers = AsyncMock()
        agent._template_engine = None  # invariant violated on purpose
        agent._job_store = None
        agent._local_server = None

        # _handle_config_update swallows exceptions from the try block and
        # logs them - it must not propagate and must not silently rebuild
        # the JobQueue with a None template engine.
        await agent._handle_config_update({})

        assert "Template engine not initialized" in caplog.text

    async def test_rebuilds_job_queue_when_template_engine_present(self):
        agent = PrinterAgent(config_path=None)

        agent._ws_client = MagicMock()
        agent._ws_client.fetch_printer_config = AsyncMock(return_value=[])
        agent._ws_client.fetch_templates = AsyncMock(return_value=None)
        agent._ws_client.report_job_complete = AsyncMock()
        agent._ws_client.report_job_failed = AsyncMock()
        agent._printer_manager = MagicMock()
        agent._printer_manager.reconfigure = AsyncMock()
        agent._printer_manager.get_all_printers = MagicMock(return_value=[])
        old_job_queue = MagicMock()
        old_job_queue.stop_workers = AsyncMock()
        agent._job_queue = old_job_queue
        agent._template_engine = MagicMock()
        agent._job_store = None
        agent._local_server = None

        await agent._handle_config_update({})

        old_job_queue.stop_workers.assert_awaited_once()
        assert agent._job_queue is not None
        assert agent._job_queue is not old_job_queue
