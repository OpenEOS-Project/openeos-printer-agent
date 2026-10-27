"""Token rejected by the server at runtime or at start.

Removed in the web app -> the agent discards device.json and shows a new
pairing code without a restart; blocked -> it waits and reconnects once
unblocked; a stale token at start -> pairs again instead of crashing.
Everything runs against tests/fake_api.py on loopback.
"""

import asyncio
import json
import socket

import aiohttp
import pytest

from src import device_registrar, main
from src.config import AppConfig, DiscoveryConfig, LocalServerConfig, LoggingConfig
from src.device_registrar import (
    STATUS_BLOCKED,
    STATUS_INVALID,
    STATUS_PENDING,
    STATUS_UNREACHABLE,
    STATUS_VERIFIED,
    DeviceRegistrar,
)
from src.main import PrinterAgent
from src.system_monitor import SystemMonitor
from src.websocket_client import WebSocketClient
from tests.fake_api import FakeApi
from tests.test_integration import stub_scanners  # noqa: F401 - fixture


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def api():
    fake = FakeApi()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def fast_polls(monkeypatch):
    monkeypatch.setattr(device_registrar, "BLOCKED_POLL_S", 0.2)
    monkeypatch.setattr(device_registrar, "REGISTER_RETRY_S", 0.2)
    monkeypatch.setattr(main, "AUTH_CHECK_MIN_INTERVAL_S", 0.0)


def _config(api: FakeApi, tmp_path, status_port: int | None = None) -> AppConfig:
    config = AppConfig(
        server_url=api.url,
        data_dir=str(tmp_path / "data"),
        logging=LoggingConfig(level="INFO", dir=str(tmp_path / "logs")),
        local_server=LocalServerConfig(enabled=status_port is not None, host="127.0.0.1", port=status_port or 8080),
        discovery=DiscoveryConfig(interval_s=3600, mdns_timeout_s=0.01),
        dummy_printer=True,
    )
    config.server.heartbeat_interval_ms = 300
    config.server.reconnect_interval_ms = 200
    config.server.reconnect_max_interval_ms = 400
    return config


async def _start_paired(api: FakeApi, config: AppConfig) -> tuple[PrinterAgent, asyncio.Task[None]]:
    agent = PrinterAgent(config=config)
    task = asyncio.create_task(agent.run())
    await api.wait_for(lambda: api.devices)
    api.verify()
    await api.wait_for(lambda: api.heartbeats)
    return agent, task


async def _stop(agent: PrinterAgent, task: asyncio.Task[None]) -> None:
    agent.request_shutdown()
    await asyncio.wait_for(task, 15)


async def test_removed_device_repairs_without_restart(api: FakeApi, stub_scanners, fast_polls, tmp_path, caplog):  # noqa: F811
    status_port = _free_port()
    config = _config(api, tmp_path, status_port)
    agent, task = await _start_paired(api, config)
    try:
        await api.wait_for(lambda: api.discovered_reports)
        old_token = api.remove()
        token_file = tmp_path / "data" / "device.json"
        assert json.loads(token_file.read_text())["device_token"] == old_token

        # Next REST call (a discovery report) fails with 401 -> status check
        # -> device gone -> new registration, new code. No restart.
        await api.emit_discover(subnet_scan=False, token=old_token)
        await api.wait_for(lambda: api.devices and api.token != old_token)
        new_device = api.devices[api.token]
        await api.wait_for(lambda: token_file.exists() and api.token in token_file.read_text())
        assert "removed in the OpenEOS web app" in caplog.text
        assert f"{new_device['verificationCode'][:3]} {new_device['verificationCode'][3:]}" in caplog.text
        assert len(api.devices) == 1

        # Status page is back in setup mode with the new code
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{status_port}/") as resp:
                assert new_device["verificationCode"] in await resp.text()
            async with session.get(f"http://127.0.0.1:{status_port}/api/status") as resp:
                body = await resp.json()
        assert body["mode"] == "setup"
        assert body["verificationCode"] == new_device["verificationCode"]

        # Admin enters the new code -> socket reconnects with the new token,
        # discovery reports again, jobs can flow.
        reports_before = len(api.discovered_reports)
        heartbeats_before = len(api.heartbeats)
        api.verify(organization_name="Neuer Verein")
        await api.wait_for(lambda: api.token in api.sids and len(api.heartbeats) > heartbeats_before)
        await api.wait_for(lambda: len(api.discovered_reports) > reports_before)
        assert api.discovered_reports[-1]["full"] is True
        assert await api.emit_discover(subnet_scan=False) == {"received": True}
        assert "Connected again as device" in caplog.text
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{status_port}/api/status") as resp:
                body = await resp.json()
        assert body.get("mode") != "setup"
        assert body["agent"]["organization"] == "Neuer Verein"
        assert body["connection"]["connected"] is True
    finally:
        await _stop(agent, task)


async def test_blocked_device_waits_and_reconnects(api: FakeApi, stub_scanners, fast_polls, tmp_path, caplog):  # noqa: F811
    status_port = _free_port()
    config = _config(api, tmp_path, status_port)
    agent, task = await _start_paired(api, config)
    try:
        token = api.token
        old_sid = api.sids[token]
        await api.block()

        # deviceStatusChanged -> status check says blocked -> wait, no re-pair
        await api.wait_for(lambda: agent._device_registrar and agent._device_registrar.status == "blocked")
        await api.wait_for(lambda: "blocked in the OpenEOS web app" in caplog.text)
        await asyncio.sleep(0.6)  # a few BLOCKED_POLL_S rounds
        assert list(api.devices) == [token]  # no new registration
        assert json.loads((tmp_path / "data" / "device.json").read_text())["device_token"] == token
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{status_port}/") as resp:
                assert "Gesperrt" in await resp.text()
            async with session.get(f"http://127.0.0.1:{status_port}/api/status") as resp:
                body = await resp.json()
        assert body == {"mode": "setup", "status": "blocked", "verificationCode": None, "deviceId": body["deviceId"]}

        heartbeats_before = len(api.heartbeats)
        await api.unblock()
        await api.wait_for(lambda: api.sids.get(token) not in (None, old_sid))
        await api.wait_for(lambda: len(api.heartbeats) > heartbeats_before)
        assert agent._device_registrar is not None and agent._device_registrar.status == "verified"
        assert list(api.devices) == [token]
    finally:
        await _stop(agent, task)


async def test_stale_token_at_start_pairs_again(api: FakeApi, stub_scanners, fast_polls, tmp_path, caplog):  # noqa: F811
    """device.json from a device that was removed while the agent was off:
    the old code raised "Invalid device token" and crash-looped the service."""
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / "device.json").write_text(json.dumps({"device_token": "tok-stale", "device_id": "dev-old"}))
    config = _config(api, tmp_path)
    agent = PrinterAgent(config=config)
    task = asyncio.create_task(agent.run())
    try:
        await api.wait_for(lambda: api.devices)
        assert "does not know this device any more" in caplog.text
        assert json.loads((data / "device.json").read_text())["device_token"] == api.token
        api.verify()
        await api.wait_for(lambda: api.heartbeats)
        assert len(api.devices) == 1
    finally:
        await _stop(agent, task)


class TestRegistrarStatus:
    async def test_check_status_classifies_answers(self, api: FakeApi, tmp_path):
        config = _config(api, tmp_path)
        registrar = DeviceRegistrar(config)
        await registrar.ensure_registered()
        assert await registrar.check_status() == STATUS_PENDING
        assert registrar.status == "pending"

        api.verify(organization_name="Verein")
        assert await registrar.check_status() == STATUS_VERIFIED
        assert registrar.organization_name == "Verein"
        assert registrar.verification_code is None
        saved = json.loads(registrar.token_file.read_text())
        assert saved["device_token"] == api.token and saved["verification_code"] is None

        await api.block()
        assert await registrar.check_status() == STATUS_BLOCKED
        assert registrar.status == "blocked"

        api.remove()
        assert await registrar.check_status() == STATUS_INVALID

        await api.stop()
        assert await registrar.check_status() == STATUS_UNREACHABLE

    async def test_discard_token_removes_file(self, api: FakeApi, tmp_path):
        registrar = DeviceRegistrar(_config(api, tmp_path))
        await registrar.ensure_registered()
        assert registrar.token_file.exists()
        registrar.discard_token()
        assert not registrar.token_file.exists()
        assert registrar.device_token is None and registrar.verification_code is None
        assert registrar.status == "unknown"
        # a second discard is harmless
        registrar.discard_token()

    async def test_config_token_is_never_discarded(self, api: FakeApi, fast_polls, tmp_path, caplog):
        config = _config(api, tmp_path)
        config.server.device_token = "tok-provisioned"
        registrar = DeviceRegistrar(config)
        await registrar.ensure_registered()
        assert registrar.token_from_config
        task = asyncio.create_task(registrar.wait_for_verification(poll_interval=0.05))
        await api.wait_for(lambda: "rejected by the server" in caplog.text)
        await asyncio.sleep(0.3)
        assert api.devices == {}  # no self-registration with a provisioned token
        assert registrar.device_token == "tok-provisioned"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestSocketRejection:
    async def test_connect_error_reports_auth_error(self, tmp_path):
        """Gateway variant that refuses the namespace outright."""
        api = FakeApi(reject_mode="refuse")
        await api.start()
        reasons: list[str] = []
        try:
            client = WebSocketClient(
                config=_config(api, tmp_path),
                device_token="tok-unknown",
                device_id="dev-x",
                system_monitor=SystemMonitor(),
                on_auth_error=reasons.append,
            )
            with pytest.raises(Exception):
                await client.connect()
            await client.disconnect()
            assert reasons and reasons[0].startswith("socket connect_error")
            assert "Authentication required" in reasons[0]
        finally:
            await api.stop()

    async def test_error_event_reports_auth_error(self, api: FakeApi, tmp_path):
        """Real gateway: accept, emit error, disconnect."""
        reasons: list[str] = []
        client = WebSocketClient(
            config=_config(api, tmp_path),
            device_token="tok-unknown",
            device_id="dev-x",
            system_monitor=SystemMonitor(),
            on_auth_error=reasons.append,
        )
        await client.connect()
        await api.wait_for(lambda: reasons)
        assert reasons[0] == "server error: Authentication required"
        await client.disconnect()
        assert client.is_connected is False
