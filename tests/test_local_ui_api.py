"""/api/ui/* of the local server: the JSON API the desktop window uses."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from src import recent_log
from src.config import AppConfig, LocalServerConfig
from src.job_queue import JobQueue
from src.local_server import UI_API_VERSION, LocalServer, _host_name, _is_loopback
from src.power import KeepAwake
from src.printer_manager import PrinterManager
from src.template_engine import TemplateEngine


def _registrar(status="verified", code=None, org="TSV Musterstadt"):
    reg = MagicMock()
    reg.status = status
    reg.verification_code = code
    reg.organization_name = org
    reg.device_id = "dev-1"
    return reg


def _config(**local):
    return AppConfig(
        server_url="https://api.openeos.de/api",
        local_server=LocalServerConfig(host="127.0.0.1", port=8080, **local),
    )


async def _client(server: LocalServer) -> TestClient:
    client = TestClient(TestServer(server._app))
    await client.start_server()
    return client


@pytest.fixture
async def printer_manager():
    pm = PrinterManager(dummy=True)
    await pm.initialize(
        [
            {
                "id": "p1",
                "name": "Theke",
                "connectionType": "network",
                "connectionConfig": {"ipAddress": "192.0.2.10", "port": 9100},
                "paperWidth": 80,
            }
        ]
    )
    yield pm
    await pm.shutdown()


class TestState:
    async def test_pairing_state_has_code(self):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        server.set_device_registrar(_registrar(status="pending", code="12345678", org=None))
        client = await _client(server)
        try:
            resp = await client.get("/api/ui/state")
            assert resp.status == 200
            data = await resp.json()
        finally:
            await client.close()
        assert data["apiVersion"] == UI_API_VERSION
        assert data["pairing"] == {"status": "pending", "code": "12345678", "organization": None}
        assert data["connection"]["connected"] is False
        assert data["connection"]["ready"] is False
        assert data["connection"]["webAppUrl"] == "https://app.openeos.de"
        assert data["printers"] == []
        assert data["statusPageUrl"] == "http://localhost:8080/"

    async def test_starting_without_registrar(self):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        data = server.build_ui_state()
        assert data["pairing"]["status"] == "starting"
        assert data["pairing"]["code"] is None

    async def test_verified_state_with_printers_queue_power_and_problems(self, printer_manager):
        job_queue = MagicMock()
        job_queue.get_queue_stats.return_value = {
            "p1": {"queued": 3, "completed": 2, "failed": 1, "pending": 0, "lastJob": {"status": "completed"}}
        }
        ws = MagicMock()
        ws.is_connected = True
        keep_awake = KeepAwake(True, backend=None)
        server = LocalServer(config=_config(), system_monitor=MagicMock(), keep_awake=keep_awake)
        server.set_device_registrar(_registrar(code="999"))
        server.set_components(printer_manager=printer_manager, job_queue=job_queue, ws_client=ws)

        recent_log.handler().clear()
        recent_log.install()
        logging.getLogger("src.test").warning("Drucker Theke nicht erreichbar")
        logging.getLogger("src.test").info("not a problem")

        data = server.build_ui_state()
        assert data["pairing"]["code"] is None  # verified: no code shown
        assert data["pairing"]["organization"] == "TSV Musterstadt"
        assert data["connection"]["connected"] is True
        assert data["connection"]["ready"] is True
        [printer] = data["printers"]
        assert printer["name"] == "Theke"
        assert printer["status"] == "online"
        assert printer["queue"] == {"pending": 0, "completed": 2, "failed": 1}
        assert printer["lastJob"] == {"status": "completed"}
        assert data["queue"] == {"pending": 0, "completed": 2, "failed": 1}
        assert [p["message"] for p in data["problems"]] == ["Drucker Theke nicht erreichbar"]
        assert data["power"] == {"keepAwake": True, "active": False, "backend": None, "error": None}
        assert data["paths"]["logFile"].endswith("agent.log")


class TestTestPrint:
    async def test_enqueues_local_job(self, printer_manager):
        job_queue = MagicMock()
        job_queue.enqueue = AsyncMock(return_value=True)
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        server.set_components(printer_manager=printer_manager, job_queue=job_queue, ws_client=MagicMock())
        client = await _client(server)
        try:
            resp = await client.post("/api/ui/printers/p1/test-print", headers={"X-OpenEOS-Agent-UI": "1"})
            assert resp.status == 202
            body = await resp.json()
        finally:
            await client.close()
        job, kwargs = job_queue.enqueue.call_args.args[0], job_queue.enqueue.call_args.kwargs
        assert job["jobId"] == body["jobId"]
        assert job["printerId"] == "p1"
        assert job["templateName"] == "_admin_test"
        assert job["_local"] is True
        assert job["payload"]["printerName"] == "Theke"
        assert kwargs == {"persist": False}

    async def test_unknown_printer_and_not_ready(self, printer_manager):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        client = await _client(server)
        try:
            headers = {"X-OpenEOS-Agent-UI": "1"}
            assert (await client.post("/api/ui/printers/p1/test-print", headers=headers)).status == 409
            server.set_components(printer_manager=printer_manager, job_queue=MagicMock(), ws_client=MagicMock())
            assert (await client.post("/api/ui/printers/nope/test-print", headers=headers)).status == 404
        finally:
            await client.close()

    async def test_write_needs_header(self, printer_manager):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        server.set_components(printer_manager=printer_manager, job_queue=MagicMock(), ws_client=MagicMock())
        client = await _client(server)
        try:
            resp = await client.post("/api/ui/printers/p1/test-print")
            assert resp.status == 403
            assert (await resp.json())["error"] == "missing_header"
        finally:
            await client.close()

    async def test_local_job_is_printed_but_not_reported(self, printer_manager, tmp_path):
        on_complete = AsyncMock(return_value=True)
        store = MagicMock()
        jq = JobQueue(
            printer_manager=printer_manager,
            template_engine=TemplateEngine(cache_dir=tmp_path / "tpl"),
            on_job_complete=on_complete,
            job_store=store,
        )
        jq.start_workers()
        try:
            ok = await jq.enqueue(
                {"jobId": "local-1", "printerId": "p1", "templateName": "_admin_test", "_local": True, "payload": {}}
            )
            assert ok
            await jq._queues["p1"].join()
        finally:
            await jq.stop_workers()
        stats = jq.get_queue_stats()["p1"]
        assert stats["completed"] == 1
        assert stats["lastJob"]["jobId"] == "local-1"
        assert stats["lastJob"]["local"] is True
        on_complete.assert_not_called()
        store.add.assert_not_called()
        store.mark_outcome.assert_not_called()


class TestGuard:
    async def test_rejects_foreign_host_header(self):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        client = await _client(server)
        try:
            resp = await client.get("/api/ui/state", headers={"Host": "evil.example:8080"})
            assert resp.status == 403
            assert (await resp.json())["error"] == "bad_host"
            resp = await client.get("/api/ui/state", headers={"Host": "localhost:8080"})
            assert resp.status == 200
        finally:
            await client.close()

    async def test_rejects_remote_peer(self):
        server = LocalServer(config=_config(), system_monitor=MagicMock())

        async def handler(request):
            from aiohttp import web

            return web.json_response({"ok": True})

        guarded = server._local_only(handler)
        transport = MagicMock()
        transport.get_extra_info.return_value = ("192.168.1.20", 50000)
        request = make_mocked_request("GET", "/api/ui/state", headers={"Host": "localhost"}, transport=transport)
        resp = await guarded(request)
        assert resp.status == 403

    async def test_remote_allowed_when_configured(self):
        server = LocalServer(config=_config(allow_remote_ui=True), system_monitor=MagicMock())

        async def handler(request):
            from aiohttp import web

            return web.json_response({"ok": True})

        transport = MagicMock()
        transport.get_extra_info.return_value = ("192.168.1.20", 50000)
        request = make_mocked_request("GET", "/api/ui/state", headers={"Host": "pi.local"}, transport=transport)
        resp = await server._local_only(handler)(request)
        assert resp.status == 200

    def test_status_page_and_old_api_unchanged(self):
        server = LocalServer(config=_config(), system_monitor=MagicMock())
        paths = {r.resource.canonical for r in server._app.router.routes() if r.resource is not None}
        assert {"/", "/api/status", "/api/printers", "/api/jobs", "/health"} <= paths

    @pytest.mark.parametrize(
        "remote,expected",
        [
            ("127.0.0.1", True),
            ("127.0.0.5", True),
            ("::1", True),
            ("::ffff:127.0.0.1", True),
            ("192.168.1.2", False),
            ("::ffff:192.168.1.2", False),
            (None, False),
            ("garbage", False),
        ],
    )
    def test_is_loopback(self, remote, expected):
        assert _is_loopback(remote) is expected

    @pytest.mark.parametrize(
        "host,expected",
        [("localhost:8080", "localhost"), ("127.0.0.1", "127.0.0.1"), ("[::1]:8080", "[::1]"), ("x.y", "x.y")],
    )
    def test_host_name(self, host, expected):
        assert _host_name(host) == expected


class TestRecentLog:
    def test_ring_buffer_keeps_newest_warnings(self):
        h = recent_log.RecentProblemsHandler(capacity=3)
        log = logging.getLogger("ring-test")
        log.propagate = False
        log.addHandler(h)
        try:
            for i in range(5):
                log.warning(f"w{i}")
            log.info("ignored")
        finally:
            log.removeHandler(h)
        assert [e["message"] for e in h.entries()] == ["w4", "w3", "w2"]

    def test_max_age(self, monkeypatch):
        recent_log.handler().clear()
        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "old", None, None)
        record.created = 0
        recent_log.handler().emit(record)
        assert recent_log.recent_problems() == []
