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

    async def test_raises_and_is_caught_when_template_engine_missing(self, caplog, sample_config):
        agent = PrinterAgent(config=sample_config)

        agent._api_client = MagicMock()
        agent._api_client.get_printers = AsyncMock(return_value=[])
        agent._ws_client = MagicMock()
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

    async def test_rebuilds_job_queue_when_template_engine_present(self, sample_config):
        agent = PrinterAgent(config=sample_config)

        agent._api_client = MagicMock()
        agent._api_client.get_printers = AsyncMock(return_value=[])
        agent._api_client.get_templates = AsyncMock(return_value={})
        agent._ws_client = MagicMock()
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


class TestDiscoverCallback:
    async def test_printer_discover_triggers_scan(self, sample_config):
        agent = PrinterAgent(config=sample_config)
        agent._discovery = MagicMock()
        await agent._handle_discover(True)
        agent._discovery.trigger.assert_called_once_with(subnet_scan=True)


class TestShutdownRequests:
    async def test_threadsafe_request_sets_event(self, sample_config):
        import asyncio

        agent = PrinterAgent(config=sample_config)
        agent._loop = asyncio.get_running_loop()
        agent.request_shutdown_threadsafe()
        await asyncio.wait_for(agent._shutdown_event.wait(), 1)

    async def test_run_stops_while_start_is_still_waiting(self, sample_config, monkeypatch):
        """Shutdown must work before start() finished (e.g. still pairing)."""
        import asyncio

        agent = PrinterAgent(config=sample_config)

        async def never_finishes() -> None:
            await asyncio.sleep(3600)

        monkeypatch.setattr(agent, "start", never_finishes)
        task = asyncio.create_task(agent.run())
        await asyncio.sleep(0.05)
        agent.request_shutdown()
        await asyncio.wait_for(task, 5)
