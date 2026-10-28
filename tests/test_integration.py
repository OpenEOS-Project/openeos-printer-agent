"""End-to-end against the in-process fake backend (tests/fake_api.py):

pairing -> verification -> socket connect -> discovery report (full=true)
-> adoption + printerConfigUpdate -> adopted printer fetched -> job printed
to the Dummy backend -> printerJobComplete -> printerDiscover with subnet
scan -> second report. Scanners are stubbed; the port scan hits the real
fake printer emulator on loopback.
"""

import asyncio
import socket
from typing import Any

import aiohttp
import pytest

from src.config import AppConfig, DiscoveryConfig, LocalServerConfig, LoggingConfig
from src.discovery import DiscoveredPrinter, mdns, neighbours, spooler, usb
from src.main import PrinterAgent
from tests.fake_api import FakeApi

USB_FINGERPRINT = "usb:04b8:0202:X1A2B3"


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
def stub_scanners(monkeypatch):
    monkeypatch.setattr(
        usb,
        "scan_usb",
        lambda: [
            DiscoveredPrinter(
                USB_FINGERPRINT,
                "usb",
                "EPSON TM-T88V",
                "usb",
                {
                    "manufacturer": "EPSON",
                    "product": "TM-T88V",
                    "serialNumber": "X1A2B3",
                    "usbVendorId": "04b8",
                    "usbProductId": "0202",
                },
            )
        ],
    )
    monkeypatch.setattr(spooler, "scan_spooler", lambda: [])

    async def no_mdns(timeout: float) -> list[DiscoveredPrinter]:
        return []

    monkeypatch.setattr(mdns, "scan_mdns", no_mdns)
    monkeypatch.setattr(neighbours, "lookup_macs", lambda: {})


async def test_full_flow(api: FakeApi, stub_scanners, tmp_path, caplog, monkeypatch):
    status_port = _free_port()
    config = AppConfig(
        server_url=api.url,
        data_dir=str(tmp_path / "data"),
        logging=LoggingConfig(level="INFO", dir=str(tmp_path / "logs")),
        local_server=LocalServerConfig(enabled=True, host="127.0.0.1", port=status_port),
        discovery=DiscoveryConfig(interval_s=3600, mdns_timeout_s=0.01, subnet="127.0.0.1/32"),
        dummy_printer=True,
    )
    config.server.heartbeat_interval_ms = 500
    config.server.reconnect_interval_ms = 200

    # Port scan target: the emulator from tools/fake_printer.py on a free port
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("fake_printer", Path("tools/fake_printer.py"))
    assert spec and spec.loader
    fp_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fp_module)
    fake_printer = fp_module.FakePrinter("OPENEOS", "FAKE-9100", flush_after=0.2)
    printer_server = await asyncio.start_server(fake_printer.handle, "127.0.0.1", 0)
    printer_port = printer_server.sockets[0].getsockname()[1]
    from src.discovery import portscan

    monkeypatch.setattr(portscan, "RAW_PORT", printer_port)
    original_scan = portscan.scan_subnet

    async def scan_on_fake_port(subnet: Any, port: int = printer_port, **kwargs: Any) -> list[DiscoveredPrinter]:
        return await original_scan(subnet, port=printer_port, **kwargs)

    monkeypatch.setattr(portscan, "scan_subnet", scan_on_fake_port)

    agent = PrinterAgent(config=config)
    run_task = asyncio.create_task(agent.run())

    try:
        # 1. Pairing: the agent registers and shows the code (log + status page)
        await api.wait_for(lambda: api.devices)
        device = api.devices[api.token]
        assert device["userAgent"].startswith("openeos-printer-agent/0.2.2")
        code = device["verificationCode"]
        assert f"{code[:3]} {code[3:]}" in caplog.text  # banner prints "123 456"
        assert "PAIRING REQUIRED" in caplog.text
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{status_port}/") as resp:
                page = await resp.text()
            assert device["verificationCode"] in page
            async with session.get(f"http://127.0.0.1:{status_port}/api/status") as resp:
                assert (await resp.json())["mode"] == "setup"
        assert (tmp_path / "data" / "device.json").exists()

        # 2. Admin enters the code -> verified -> socket connects, discovery runs
        api.verify()
        await api.wait_for(lambda: api.discovered_reports)
        report = api.discovered_reports[0]
        assert report["full"] is True
        assert [p["fingerprint"] for p in report["printers"]] == [USB_FINGERPRINT]
        assert report["printers"][0] == {
            "fingerprint": USB_FINGERPRINT,
            "connectionType": "usb",
            "displayName": "EPSON TM-T88V",
            "details": {
                "manufacturer": "EPSON",
                "product": "TM-T88V",
                "serialNumber": "X1A2B3",
                "usbVendorId": "04b8",
                "usbProductId": "0202",
            },
            "discoveredVia": "usb",
        }
        assert api.sync_calls == 0  # the removed sync endpoint is never called

        # 3. Heartbeat payload per spec
        await api.wait_for(lambda: api.heartbeats)
        hb = api.heartbeats[0]
        assert set(hb) == {"version", "os", "arch", "hostname", "pythonVersion", "capabilities", "printers"}
        assert hb["version"] == "0.2.2"
        assert set(hb["capabilities"]) == {"usb", "mdns", "portscan", "spooler"}

        # 4. Adopt in the "web app" -> printerConfigUpdate -> agent fetches it
        api.adopt(USB_FINGERPRINT, "printer-1", name="Kassenbon")
        await api.emit_config_update()
        await api.wait_for(lambda: agent.printer_manager and agent.printer_manager.get_printer("printer-1"))
        managed = agent.printer_manager.get_printer("printer-1")
        assert managed is not None and managed.fingerprint == USB_FINGERPRINT
        await api.wait_for(lambda: managed.is_online)

        # 5. Test print as a normal job with the _admin_test template
        ack = await api.send_job(
            {
                "jobId": "job-1",
                "printerId": "printer-1",
                "templateName": "_admin_test",
                "copies": 1,
                "payload": {"printerName": "Kassenbon", "jobId": "job-1", "body": "Hallo Testbon"},
            }
        )
        assert ack == {"received": True}
        await api.wait_for(lambda: api.job_completes)
        assert api.job_completes[0]["jobId"] == "job-1"
        assert "Hallo Testbon" in caplog.text  # dummy printer logged the rendered ticket
        assert "OPENEOS TEST-DRUCK" in caplog.text

        # heartbeat now lists the adopted printer as online
        await api.wait_for(
            lambda: any(
                p.get("fingerprint") == USB_FINGERPRINT and p.get("online")
                for h in api.heartbeats
                for p in h["printers"]
            )
        )

        # 6. "Neu suchen" with network scan -> port scan finds the emulator
        before = len(api.discovered_reports)
        assert await api.emit_discover(subnet_scan=True) == {"received": True}
        await api.wait_for(lambda: len(api.discovered_reports) > before)
        latest = api.discovered_reports[-1]
        fps = {p["fingerprint"]: p for p in latest["printers"]}
        assert USB_FINGERPRINT in fps
        assert "net:127.0.0.1" in fps
        assert fps["net:127.0.0.1"]["displayName"] == "OPENEOS FAKE-9100"
        assert fps["net:127.0.0.1"]["discoveredVia"] == "portscan"
        assert fps["net:127.0.0.1"]["details"]["port"] == printer_port

        # status page lists both
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{status_port}/api/discovery") as resp:
                found = await resp.json()
        assert {p["fingerprint"] for p in found["printers"]} == {USB_FINGERPRINT, "net:127.0.0.1"}
    finally:
        agent.request_shutdown()
        await asyncio.wait_for(run_task, 15)
        printer_server.close()
        await printer_server.wait_closed()


async def test_restart_reuses_token_and_replays_pending_job(api: FakeApi, stub_scanners, tmp_path):
    """Token persisted on first start; a job accepted but not finished before
    a crash is printed after the restart (job store)."""
    config = AppConfig(
        server_url=api.url,
        data_dir=str(tmp_path / "data"),
        logging=LoggingConfig(dir=str(tmp_path / "logs")),
        local_server=LocalServerConfig(enabled=False),
        discovery=DiscoveryConfig(interval_s=3600, mdns_timeout_s=0.01),
        dummy_printer=True,
    )
    config.server.heartbeat_interval_ms = 500

    agent = PrinterAgent(config=config)
    task = asyncio.create_task(agent.run())
    await api.wait_for(lambda: api.devices)
    api.verify()
    await api.wait_for(lambda: api.discovered_reports)
    api.adopt(USB_FINGERPRINT, "printer-1")
    await api.emit_config_update()
    await api.wait_for(lambda: agent.printer_manager and agent.printer_manager.get_printer("printer-1"))

    # Simulate "printed but the completion never reached the server":
    # stop reporting, send a job, then kill the agent.
    agent._job_queue._on_job_complete = _never_delivered  # type: ignore[union-attr]
    ack = await api.send_job({"jobId": "job-x", "printerId": "printer-1", "templateName": "_admin_test", "payload": {}})
    assert ack == {"received": True}
    await asyncio.sleep(0.5)
    agent.request_shutdown()
    await asyncio.wait_for(task, 15)
    assert api.job_completes == []

    # Second start: same token (no new /devices/init), outcome delivered on connect.
    registered = len(api.devices)
    agent2 = PrinterAgent(config=config)
    task2 = asyncio.create_task(agent2.run())
    try:
        await api.wait_for(lambda: api.job_completes)
        assert api.job_completes[0]["jobId"] == "job-x"
        assert len(api.devices) == registered
    finally:
        agent2.request_shutdown()
        await asyncio.wait_for(task2, 15)


async def _never_delivered(job_id: str) -> bool:
    return False
