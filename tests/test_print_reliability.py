"""Printing must not lose or duplicate tickets: network transport with
confirmation, honest online state, retries, acknowledged reports, dedupe
of re-delivered jobs, wake-up handling. Scenarios from the sleep/network
reproduction against staging."""

import asyncio
import socket
import sqlite3
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src import job_queue as jq_module
from src import main as main_module
from src.escpos_renderer import render_to_printer
from src.job_queue import JobQueue
from src.job_store import JobStore
from src.main import PrinterAgent
from src.net_printer import (
    NetworkTransport,
    PrinterNoConfirmation,
    PrinterStatusError,
    parse_status,
)
from src.printer_manager import ManagedPrinter, PrinterErrorCode, PrinterManager, PrinterStatus
from src.template_engine import TemplateEngine
from src.websocket_client import WebSocketClient
from tests.fake_tcp_printer import FakeTcpPrinter


@pytest.fixture
def fake_printer():
    printer = FakeTcpPrinter().start()
    yield printer
    printer.stop()


def _transport(port: int, **kw) -> NetworkTransport:
    kw.setdefault("status_timeout", 0.5)
    kw.setdefault("connect_timeout", 1.0)
    return NetworkTransport("127.0.0.1", port, **kw)


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _network_printer(port: int) -> ManagedPrinter:
    printer = ManagedPrinter(
        {
            "id": "net-1",
            "name": "Theke",
            "connectionType": "network",
            "connectionConfig": {"ipAddress": "127.0.0.1", "port": port},
        }
    )
    printer._escpos = printer._create_escpos()
    assert printer._transport is not None
    printer._transport._status_timeout = 0.5
    printer._transport._connect_timeout = 1.0
    return printer


# -- transport ----------------------------------------------------------------------


class TestTransport:
    def test_fresh_connection_per_job_and_confirmed(self, fake_printer):
        t = _transport(fake_printer.port)
        assert t.send(b"TICKET-1\x1dV\x00") is not None
        assert t.send(b"TICKET-2") is not None
        assert fake_printer.connections == 2
        assert t.status_supported is True
        assert _wait(lambda: fake_printer.open_connections == 0)  # nothing left open
        assert fake_printer.tickets == [b"TICKET-1\x1dV\x00", b"TICKET-2"]

    def test_printer_closing_idle_connections_loses_nothing(self, fake_printer):
        # (a) the printer drops idle connections - with one long-lived socket
        # the next ticket vanished into the send buffer.
        fake_printer.idle_close_s = 0.1
        t = _transport(fake_printer.port)
        t.send(b"A")
        time.sleep(0.3)
        t.send(b"B")
        assert fake_printer.tickets == [b"A", b"B"]

    def test_printer_without_status_support_falls_back(self, fake_printer):
        fake_printer.answer_status = False
        t = _transport(fake_printer.port)
        assert t.send(b"A") is None
        assert t.status_supported is False
        started = time.monotonic()
        assert t.send(b"B") is None
        assert time.monotonic() - started < 0.4  # no status wait any more
        assert _wait(lambda: fake_printer.tickets == [b"A", b"B"])

    def test_missing_confirmation_from_status_printer_is_an_error(self, fake_printer):
        # (c) printer reachable at TCP level but not taking data any more
        t = _transport(fake_printer.port)
        t.send(b"A")
        fake_printer.answer_status = False
        with pytest.raises(PrinterNoConfirmation):
            t.send(b"B")

    def test_cover_open_is_detected_before_sending(self, fake_printer):
        t = _transport(fake_printer.port)
        t.probe()
        fake_printer.offline_cause = 0x12 | 0x04
        with pytest.raises(PrinterStatusError) as exc:
            t.send(b"NOT-PRINTED")
        assert exc.value.code == "COVER_OPEN"
        assert fake_printer.tickets == []

    def test_paper_out_after_sending(self, fake_printer):
        fake_printer.paper_sensor = 0x12 | 0x60
        t = _transport(fake_printer.port)
        with pytest.raises(PrinterStatusError) as exc:
            t.send(b"X")
        assert exc.value.code == "PAPER_OUT"

    def test_refused_when_off(self, fake_printer):
        port = fake_printer.port
        fake_printer.stop()
        # Linux: refused right away; Windows retries the SYN and may time out
        with pytest.raises(OSError):
            _transport(port).send(b"X")

    def test_probe_raises_when_unreachable(self):
        def timeout_connect(addr, timeout):
            raise socket.timeout("timed out")

        with pytest.raises(socket.timeout):
            NetworkTransport("192.0.2.1", 9100, connect=timeout_connect).probe()

    @pytest.mark.parametrize(
        "offline,paper,problem",
        [
            (0x12, 0x12, None),
            (0x16, 0x12, "COVER_OPEN"),
            (0x32, 0x12, "PAPER_OUT"),
            (0x12, 0x72, "PAPER_OUT"),
            (0x52, 0x12, "PRINTER_OFFLINE"),
            (0x12, 0x1E, None),  # near end only
        ],
    )
    def test_parse_status(self, offline, paper, problem):
        state = parse_status(offline, paper)
        assert (state.problem[0] if state.problem else None) == problem


# -- managed printer ------------------------------------------------------------------


class TestManagedNetworkPrinter:
    async def test_online_state_comes_from_a_real_probe(self, fake_printer):
        printer = _network_printer(fake_printer.port)
        printer._escpos = None
        assert await printer.connect() is True
        assert printer.status == PrinterStatus.ONLINE
        fake_printer.stop()
        assert await printer.probe() is False
        assert printer.status == PrinterStatus.OFFLINE
        assert printer.last_error_code == PrinterErrorCode.CONNECTION_FAILED
        assert printer.last_error  # never empty
        fake_printer.restart()
        assert await printer.probe() is True
        assert printer.is_online

    async def test_unreachable_at_start_still_prints_later(self, fake_printer):
        port = fake_printer.port
        fake_printer.stop()
        printer = _network_printer(port)
        printer._escpos = None
        assert await printer.connect() is False
        assert printer.status == PrinterStatus.OFFLINE
        fake_printer.restart()
        await printer.execute(lambda p: p.text("hallo\n"))
        assert printer.is_online
        assert b"hallo" in b"".join(fake_printer.tickets)

    async def test_execute_reports_status_problem(self, fake_printer):
        printer = _network_printer(fake_printer.port)
        fake_printer.paper_sensor = 0x72
        with pytest.raises(PrinterStatusError):
            await printer.execute(lambda p: p.text("x\n"))
        assert printer.status == PrinterStatus.ERROR
        assert printer.last_error_code == PrinterErrorCode.PAPER_OUT

    async def test_health_check_probes_network_printers(self, fake_printer):
        pm = PrinterManager()
        pm._printers["net-1"] = _network_printer(fake_printer.port)
        await pm.health_check_all()
        assert pm.get_printer("net-1").is_online
        assert fake_printer.connections == 1


# -- renderer -------------------------------------------------------------------------


class TestRendererErrors:
    def test_io_error_in_tag_propagates(self):
        printer = MagicMock()
        printer.cut.side_effect = BrokenPipeError(32, "Broken pipe")
        with pytest.raises(BrokenPipeError):
            render_to_printer(printer, "Hallo\n<<CUT>>")

    def test_markup_error_is_still_swallowed(self):
        printer = MagicMock()
        printer.cashdraw.side_effect = ValueError("bad pin")
        render_to_printer(printer, "Hallo\n<<OPEN_DRAWER>>")  # no exception


# -- queue: retries, store, dedupe ----------------------------------------------------------


@pytest.fixture
def fast_retries(monkeypatch):
    monkeypatch.setattr(jq_module, "RETRY_DELAYS", [0.05])
    monkeypatch.setattr(jq_module, "RETRY_WINDOW_S", 1.0)


def _queue(printer: ManagedPrinter, store=None, complete=None, failed=None, tmp_path=None) -> JobQueue:
    pm = PrinterManager()
    pm._printers[printer.printer_id] = printer
    return JobQueue(
        printer_manager=pm,
        template_engine=TemplateEngine(cache_dir=tmp_path / "tpl") if tmp_path else TemplateEngine(),
        on_job_complete=complete or AsyncMock(return_value=True),
        on_job_failed=failed or AsyncMock(return_value=True),
        job_store=store,
    )


def _job(job_id="job-1", printer_id="net-1"):
    return {"jobId": job_id, "printerId": printer_id, "templateName": "_admin_test", "payload": {}}


class TestQueueReliability:
    async def test_printer_off_then_on_prints_without_failure(self, fake_printer, fast_retries, tmp_path):
        # (b) powered off: the job waits and prints once the printer is back
        port = fake_printer.port
        fake_printer.stop()
        printer = _network_printer(port)
        failed = AsyncMock(return_value=True)
        complete = AsyncMock(return_value=True)
        q = _queue(printer, complete=complete, failed=failed, tmp_path=tmp_path)
        q.start_workers()
        try:
            await q.enqueue(_job())
            await asyncio.sleep(0.2)
            assert printer.status == PrinterStatus.OFFLINE
            fake_printer.restart()
            q.wake_retries("net-1")
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=3)
        finally:
            await q.stop_workers()
        complete.assert_awaited_once_with("job-1")
        failed.assert_not_awaited()
        assert any(b"TEST-DRUCK" in t for t in fake_printer.tickets)

    async def test_gives_up_after_retry_window(self, fake_printer, fast_retries, tmp_path):
        port = fake_printer.port
        fake_printer.stop()
        failed = AsyncMock(return_value=True)
        q = _queue(_network_printer(port), failed=failed, tmp_path=tmp_path)
        q.start_workers()
        try:
            await q.enqueue(_job())
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=5)
        finally:
            await q.stop_workers()
        job_id, code, message = failed.call_args.args
        assert code == "CONNECTION_FAILED"
        assert message

    async def test_silent_printer_is_never_reported_completed(self, fake_printer, fast_retries, tmp_path):
        # (c) the printer stops taking data without closing: no "completed"
        printer = _network_printer(fake_printer.port)
        await printer.probe()
        fake_printer.answer_status = False
        complete = AsyncMock(return_value=True)
        failed = AsyncMock(return_value=True)
        q = _queue(printer, complete=complete, failed=failed, tmp_path=tmp_path)
        q.start_workers()
        try:
            await q.enqueue(_job())
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=8)
        finally:
            await q.stop_workers()
        complete.assert_not_awaited()
        assert failed.call_args.args[1] == "PRINTER_OFFLINE"
        assert not printer.is_online

    async def test_template_error_fails_without_retry(self, fake_printer, tmp_path):
        failed = AsyncMock(return_value=True)
        q = _queue(_network_printer(fake_printer.port), failed=failed, tmp_path=tmp_path)
        q.start_workers()
        try:
            await q.enqueue({"jobId": "j", "printerId": "net-1", "templateName": "does-not-exist", "payload": {}})
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=3)
        finally:
            await q.stop_workers()
        assert failed.call_args.args[1] == "TEMPLATE_ERROR"
        assert fake_printer.connections == 0

    async def test_redelivered_job_is_not_printed_twice(self, fake_printer, tmp_path):
        # (d) printed while the server connection was dead: the report got
        # no ack, the server replays the job after reconnect.
        store = JobStore(str(tmp_path / "jobs.db"))
        complete = AsyncMock(side_effect=[False, True])
        q = _queue(_network_printer(fake_printer.port), store=store, complete=complete, tmp_path=tmp_path)
        q.start_workers()
        try:
            await q.enqueue(_job())
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=3)
            assert [r["job_id"] for r in store.get_unreported()] == ["job-1"]

            assert await q.enqueue(_job()) is True  # replay
            await asyncio.sleep(0.1)
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=3)
        finally:
            await q.stop_workers()
        assert len(fake_printer.tickets) == 1
        assert complete.await_count == 2  # outcome re-sent instead
        assert store.get_unreported() == []
        assert store.get("job-1")["reported"] == 1
        store.close()

    async def test_crash_after_print_before_report_does_not_reprint(self, fake_printer, tmp_path):
        store = JobStore(str(tmp_path / "jobs.db"))
        q = _queue(
            _network_printer(fake_printer.port),
            store=store,
            complete=AsyncMock(return_value=False),
            tmp_path=tmp_path,
        )
        q.start_workers()
        try:
            await q.enqueue(_job())
            await asyncio.wait_for(q._queues["net-1"].join(), timeout=3)
        finally:
            await q.stop_workers()
        assert store.get_pending() == []  # restart would not print it again
        assert store.get_unreported()[0]["status"] == "completed"
        store.close()


class TestJobStore:
    def test_lifecycle_and_prune(self, tmp_path):
        store = JobStore(str(tmp_path / "j.db"))
        assert store.add("a", {"jobId": "a"})
        assert not store.add("a", {"jobId": "a"})
        store.mark_outcome("a", "completed")
        assert store.get_unreported() == [
            {"job_id": "a", "status": "completed", "error_code": None, "error_message": None}
        ]
        store.mark_reported("a")
        assert store.get_unreported() == []
        assert not store.add("a", {"jobId": "a"})  # still known: no duplicate print
        assert store.prune(now=time.time() + 3600) == 0
        assert store.prune(now=time.time() + 25 * 3600) == 1
        assert store.add("a", {"jobId": "a"})
        store.close()

    def test_unreported_outcomes_kept_longer(self, tmp_path):
        store = JobStore(str(tmp_path / "j.db"))
        store.add("a", {})
        store.mark_outcome("a", "failed", "PAPER_OUT", "x")
        assert store.prune(now=time.time() + 25 * 3600) == 0
        assert store.prune(now=time.time() + 8 * 24 * 3600) == 1
        store.close()

    def test_migrates_old_schema(self, tmp_path):
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, data TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',"
            " error_code TEXT, error_message TEXT, created_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO jobs VALUES ('old', '{}', 'completed', NULL, NULL, 1.0)")
        conn.commit()
        conn.close()
        store = JobStore(str(path))
        assert store.get_unreported()[0]["job_id"] == "old"
        store.close()


# -- reporting with ack -------------------------------------------------------------------------


class TestReportAck:
    @pytest.fixture
    def ws(self, sample_config):
        client = WebSocketClient(config=sample_config, device_token="t", device_id="d", system_monitor=MagicMock())
        client._connected = True
        client._sio = MagicMock()
        return client

    @pytest.mark.parametrize(
        "ack,expected",
        [
            ({"success": True}, True),
            ({"error": "Job not found"}, True),
            ({"error": "Failed to update job status"}, False),
            (None, True),
        ],
    )
    async def test_ack_values(self, ws, ack, expected):
        ws._sio.call = AsyncMock(return_value=ack)
        assert await ws.report_job_complete("j") is expected
        event, payload = ws._sio.call.call_args.args
        assert event == "printerJobComplete"
        assert payload == {"jobId": "j", "agentId": "d"}
        assert ws._sio.call.call_args.kwargs["timeout"] > 0

    async def test_no_ack_keeps_outcome(self, ws):
        ws._sio.call = AsyncMock(side_effect=TimeoutError())
        assert await ws.report_job_failed("j", "PAPER_OUT", "x") is False

    async def test_not_connected(self, ws):
        ws._connected = False
        ws._sio.call = AsyncMock()
        assert await ws.report_job_complete("j") is False
        ws._sio.call.assert_not_called()


# -- wake-up / reconnect -------------------------------------------------------------------------


class TestWakeUp:
    async def test_resume_rebuilds_socket_and_wakes_retries(self, sample_config):
        agent = PrinterAgent(config=sample_config)
        agent._ws_client = MagicMock()
        agent._ws_client.force_reconnect = AsyncMock()
        agent._job_queue = MagicMock()
        agent._printer_manager = MagicMock()
        agent._printer_manager.health_check_all = AsyncMock()
        agent._printer_manager.get_all_printers = MagicMock(return_value=[])
        agent.notify_resume("test")
        agent.notify_resume("test again")  # debounced
        await asyncio.sleep(0.05)
        agent._ws_client.force_reconnect.assert_awaited_once()
        agent._job_queue.wake_retries.assert_called_once_with()
        agent._printer_manager.health_check_all.assert_awaited()

    async def test_clock_jump_is_detected(self, sample_config, monkeypatch):
        agent = PrinterAgent(config=sample_config)
        seen = []
        agent.notify_resume = seen.append  # type: ignore[method-assign]
        monkeypatch.setattr(main_module, "WAKE_CHECK_INTERVAL_S", 0.01)
        clock = iter([1000.0, 1000.01, 1300.0, 1300.01, 1300.02] + [1300.03 + i for i in range(100)])
        monkeypatch.setattr(main_module.time, "time", lambda: next(clock))
        task = asyncio.create_task(agent._wake_watch_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        assert seen and "clock jumped" in seen[0]

    async def test_force_reconnect_retries_until_connected(self, sample_config):
        sample_config.server.reconnect_interval_ms = 1
        client = WebSocketClient(config=sample_config, device_token="t", device_id="d", system_monitor=MagicMock())
        client.reconnect_with_token = AsyncMock(side_effect=[OSError("net down"), OSError("still down"), None])
        await asyncio.wait_for(client.force_reconnect(), timeout=2)
        assert client.reconnect_with_token.await_count == 3
