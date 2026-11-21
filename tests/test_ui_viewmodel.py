"""Desktop window: state mapping, texts, client - no display needed."""

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestServer

from src import service_control as sc
from src.config import AppConfig, LocalServerConfig
from src.local_server import LocalServer
from src.ui import viewmodel as vm
from src.ui.agent_client import AgentClient, AgentError, AgentUnreachable, discover_port
from src.ui.i18n import DE, EN, Translator, detect_language

DE_T = Translator("de")
EN_T = Translator("en")
NOW = 1_800_000_000.0


def _state(**overrides):
    data = {
        "apiVersion": 1,
        "agent": {"version": "0.3.0", "hostname": "KASSE-PC", "platform": "win32"},
        "pairing": {"status": "verified", "code": None, "organization": "TSV Musterstadt"},
        "connection": {
            "connected": True,
            "ready": True,
            "serverUrl": "https://api.openeos.de/api",
            "webAppUrl": "https://app.openeos.de",
        },
        "printers": [
            {
                "printerId": "p1",
                "name": "Theke",
                "status": "online",
                "connectionType": "network",
                "lastError": None,
                "queue": {"pending": 0, "completed": 3, "failed": 0},
                "lastJob": {"status": "completed", "finishedAt": NOW - 60},
            }
        ],
        "queue": {"pending": 0, "completed": 3, "failed": 0},
        "discovery": {"found": 2},
        "problems": [],
        "power": {"keepAwake": True, "active": True, "backend": "windows", "error": None},
        "paths": {"logDir": r"C:\ProgramData\OpenEOS Printer Agent\logs"},
        "statusPageUrl": "http://localhost:8080/",
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}  # type: ignore[dict-item]
        else:
            data[key] = value
    return data


def _view(data, service=sc.RUNNING, tr=DE_T, **kw):
    kw.setdefault("service_kind", "windows")
    return vm.build_view_state(data, service, tr, ui_version="0.3.0", now=NOW, **kw)


class TestOverallState:
    def test_connected(self):
        v = _view(_state())
        assert v.state == vm.CONNECTED
        assert v.tray == vm.TRAY_OK
        assert v.headline == "Verbunden"
        assert "TSV Musterstadt" in v.detail
        assert v.server_host == "api.openeos.de"
        assert v.web_app_url == "https://app.openeos.de"
        assert v.pairing_code is None
        assert v.tooltip == "OpenEOS Drucker-Agent – Verbunden"

    def test_needs_pairing_shows_grouped_code(self):
        v = _view(_state(pairing={"status": "pending", "code": "12345678", "organization": None}))
        assert v.state == vm.NEEDS_PAIRING
        assert v.tray == vm.TRAY_PAIRING
        assert v.pairing_code == "1234 5678"

    @pytest.mark.parametrize(
        "pairing,conn,expected",
        [
            ({"status": "blocked"}, {}, vm.BLOCKED),
            ({"status": "waiting_for_org"}, {}, vm.WAITING_FOR_ORG),
            ({"status": "starting"}, {}, vm.STARTING),
            ({"status": "unknown"}, {}, vm.STARTING),
            ({"status": "verified"}, {"ready": False, "connected": False}, vm.STARTING),
            ({"status": "verified"}, {"connected": False}, vm.DISCONNECTED),
        ],
    )
    def test_agent_states(self, pairing, conn, expected):
        v = _view(_state(pairing=pairing, connection=conn))
        assert v.state == expected

    def test_printer_problem(self):
        data = _state()
        data["printers"].append(
            {"printerId": "p2", "name": "Küche", "status": "offline", "connectionType": "usb", "lastError": "USB weg"}
        )
        v = _view(data)
        assert v.state == vm.PRINTER_PROBLEM
        assert v.tray == vm.TRAY_WARN
        assert "1 von 2" in v.detail
        assert v.printers[1].detail == "USB weg"
        assert v.printers[1].kind == "error"
        assert v.printers[1].connection == "USB"

    def test_no_printers(self):
        v = _view(_state(printers=[]))
        assert v.state == vm.NO_PRINTERS
        assert "2 gefunden" in v.printers_empty_text

    @pytest.mark.parametrize(
        "service,expected,can_start",
        [
            (sc.STOPPED, vm.SERVICE_STOPPED, True),
            (sc.STARTING, vm.STARTING, False),
            (sc.NOT_INSTALLED, vm.NOT_INSTALLED, False),
            (sc.RUNNING, vm.UNREACHABLE, False),
            (sc.UNKNOWN, vm.AGENT_DOWN, False),
        ],
    )
    def test_agent_unreachable(self, service, expected, can_start):
        v = _view(None, service=service)
        assert v.state == expected
        assert v.tray == vm.TRAY_ERROR or expected == vm.STARTING
        assert v.agent_reachable is False
        assert v.can_start_service is can_start
        assert v.footer == "Fenster 0.3.0"

    def test_unreachable_mentions_port(self):
        v = _view(None, service=sc.RUNNING, port=8123)
        assert "8123" in v.detail

    def test_outdated_agent(self):
        assert _view(None, outdated=True).state == vm.OUTDATED

    def test_no_service_manager(self):
        v = _view(None, service=sc.UNKNOWN, service_kind=None)
        assert v.state == vm.AGENT_DOWN
        assert v.service_text == ""
        assert not (v.can_start_service or v.can_stop_service or v.can_restart_service)


class TestDetails:
    def test_printer_rows_and_queue(self):
        v = _view(_state())
        [row] = v.printers
        assert row.name == "Theke"
        assert row.status_text == "Online"
        assert row.connection == "Netzwerk"
        assert row.queue_text == "0 offen · 3 gedruckt · 0 fehlgeschlagen"
        assert row.detail.startswith("Zuletzt gedruckt ")
        assert row.can_test_print
        assert "3 gedruckt" in v.queue_text

    def test_failed_last_job(self):
        data = _state()
        data["printers"][0]["lastJob"] = {"status": "failed", "finishedAt": NOW, "error": "Papier leer"}
        assert "Papier leer" in _view(data).printers[0].detail

    def test_problems_newest_first(self):
        data = _state(
            problems=[
                {"time": NOW - 10, "level": "ERROR", "message": "Drucker weg"},
                {"time": NOW - 30 * 3600, "level": "WARNING", "message": "alt"},
            ]
        )
        v = _view(data)
        assert [p.message for p in v.problems] == ["Drucker weg", "alt"]
        assert v.problems[0].level == "ERROR"
        assert "." in v.problems[1].time_text  # older entries carry the date

    @pytest.mark.parametrize(
        "power,key",
        [
            ({"keepAwake": True, "active": True}, "power.active"),
            ({"keepAwake": True, "active": False}, "power.waiting"),
            ({"keepAwake": False, "active": False}, "power.off"),
        ],
    )
    def test_power_texts(self, power, key):
        assert _view(_state(power=power)).power_text == DE[key]

    def test_power_error_and_platform_hint(self):
        v = _view(_state(power={"error": "systemd-inhibit: request refused"}, agent={"platform": "linux"}))
        assert "systemd-inhibit" in v.power_text
        assert v.power_hint == DE["power.hint_linux"]
        assert _view(_state()).power_hint == DE["power.hint"]

    def test_service_actions(self):
        v = _view(_state(), service=sc.RUNNING)
        assert v.can_restart_service and v.can_stop_service and not v.can_start_service
        assert v.service_text == "läuft"

    def test_english(self):
        v = _view(_state(), tr=EN_T)
        assert v.headline == "Connected"
        assert v.printers[0].queue_text == "0 pending · 3 printed · 0 failed"


class TestNotifications:
    @pytest.mark.parametrize(
        "prev,cur,key",
        [
            (None, vm.CONNECTED, None),
            (vm.CONNECTED, vm.CONNECTED, None),
            (vm.CONNECTED, vm.DISCONNECTED, "notify.disconnected"),
            (vm.CONNECTED, vm.PRINTER_PROBLEM, "notify.printer_problem"),
            (vm.DISCONNECTED, vm.CONNECTED, "notify.reconnected"),
            (vm.CONNECTED, vm.NEEDS_PAIRING, "notify.needs_pairing"),
            (vm.STARTING, vm.DISCONNECTED, None),
        ],
    )
    def test_transitions(self, prev, cur, key):
        assert vm.notification_for(prev, cur) == key


class TestI18n:
    def test_tables_have_the_same_keys(self):
        assert set(DE) == set(EN)

    def test_german_uses_du(self):
        text = " ".join(DE.values())
        for formal in (" Sie ", " Ihre ", " Ihnen "):
            assert formal not in text

    @pytest.mark.parametrize(
        "env,lang",
        [
            ({"OPENEOS_UI_LANG": "en", "LANG": "de_DE.UTF-8"}, "en"),
            ({"LANG": "de_DE.UTF-8"}, "de"),
            ({"LANG": "fr_FR.UTF-8"}, "en"),
            ({"LANGUAGE": "de:en", "LANG": "C"}, "de"),
        ],
    )
    def test_detect(self, env, lang):
        assert detect_language(env) == lang

    def test_fallbacks(self):
        assert Translator("fr").lang == "en"
        assert DE_T("no.such.key") == "no.such.key"
        assert DE_T.get("conn.bluetooth", "bluetooth") == "bluetooth"


class TestClient:
    async def test_state_and_test_print_against_real_server(self):
        server = LocalServer(
            config=AppConfig(local_server=LocalServerConfig(host="127.0.0.1", port=8080)), system_monitor=MagicMock()
        )
        reg = MagicMock(status="pending", verification_code="87654321", organization_name=None, device_id="d")
        server.set_device_registrar(reg)
        ts = TestServer(server._app, host="127.0.0.1")
        await ts.start_server()
        try:
            client = AgentClient(port=ts.port)
            import asyncio

            data = await asyncio.to_thread(client.state)
            assert data["pairing"]["code"] == "87654321"
            with pytest.raises(AgentError) as exc:
                await asyncio.to_thread(client.test_print, "nope")
            assert exc.value.status == 409
        finally:
            await ts.close()

    def test_unreachable(self):
        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with pytest.raises(AgentUnreachable):
            AgentClient(port=port, timeout=0.5).state()

    def test_discover_port(self, tmp_path):
        missing = tmp_path / "missing.yaml"
        cfg = tmp_path / "config.yaml"
        cfg.write_text("server_url: x\nlocal_server:\n  port: 18090\n", encoding="utf-8")
        assert discover_port([missing, cfg]) == 18090
        plain = tmp_path / "plain.yaml"
        plain.write_text("server_url: x\n", encoding="utf-8")
        assert discover_port([plain, cfg]) == 8080  # first existing file wins, like the agent
        assert discover_port([missing]) == 8080
