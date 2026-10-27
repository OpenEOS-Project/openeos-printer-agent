"""Unit tests for fingerprints and the discovery parsers (no hardware:
pyusb devices, spooler entries, mDNS records and neighbour tables are
simulated)."""

import asyncio
import ipaddress
from typing import Any, Optional

import pytest

from src.config import DiscoveryConfig
from src.discovery import (
    DiscoveredPrinter,
    DiscoveryService,
    mdns,
    merge_results,
    neighbours,
    network_fingerprint,
    normalize_mac,
    portscan,
    spooler,
    types,
    usb,
    usb_fingerprint,
    windows_fingerprint,
)


class TestFingerprints:
    def test_usb_with_serial(self):
        assert usb_fingerprint(0x04B8, 0x0202, "ABC123") == "usb:04b8:0202:ABC123"

    def test_usb_without_serial_uses_dash(self):
        assert usb_fingerprint(0x0519, 0x0003, None) == "usb:0519:0003:-"
        assert usb_fingerprint(0x0519, 0x0003, "   ") == "usb:0519:0003:-"

    def test_network_prefers_mac(self):
        assert network_fingerprint("AA-BB-CC-DD-EE-FF", "192.168.1.5") == "net:aa:bb:cc:dd:ee:ff"

    def test_network_falls_back_to_ip(self):
        assert network_fingerprint(None, "192.168.1.5") == "net:192.168.1.5"
        assert network_fingerprint("00:00:00:00:00:00", "192.168.1.5") == "net:192.168.1.5"

    def test_windows(self):
        assert windows_fingerprint("EPSON TM-T88V Receipt") == "win:EPSON TM-T88V Receipt"

    def test_normalize_mac_rejects_garbage(self):
        assert normalize_mac("not a mac") is None
        assert normalize_mac("") is None
        assert normalize_mac("A1:b2:C3:d4:E5:f6") == "a1:b2:c3:d4:e5:f6"


class FakeInterface:
    def __init__(self, cls: int) -> None:
        self.bInterfaceClass = cls


class FakeUsbDevice:
    """Enough of pyusb's Device for describe_device()."""

    def __init__(
        self,
        vendor: int,
        product: int,
        *,
        device_class: int = 0,
        interface_classes: Optional[list[int]] = None,
        strings: Optional[dict[int, Optional[str]]] = None,
        string_error: bool = False,
    ) -> None:
        self.idVendor = vendor
        self.idProduct = product
        self.bDeviceClass = device_class
        self.iManufacturer = 1
        self.iProduct = 2
        self.iSerialNumber = 3
        self._interfaces = interface_classes or []
        self._strings = strings or {}
        self._string_error = string_error

    def __iter__(self):
        # one configuration whose interfaces have the given classes
        yield [FakeInterface(c) for c in self._interfaces]

    def get_string(self, index: int) -> Optional[str]:
        if self._string_error:
            raise PermissionError("[Errno 13] Access denied (insufficient permissions)")
        return self._strings.get(index)


@pytest.fixture
def patched_usb_strings(monkeypatch):
    class _Util:
        @staticmethod
        def get_string(device: Any, index: int) -> Optional[str]:
            return device.get_string(index)

    monkeypatch.setattr(usb, "usb", type("usbmod", (), {"util": _Util, "core": None}), raising=False)
    monkeypatch.setattr(usb, "_PYUSB_AVAILABLE", True)


class TestUsbDescribe:
    def test_known_vendor_with_strings(self, patched_usb_strings):
        dev = FakeUsbDevice(0x04B8, 0x0202, strings={1: "EPSON", 2: "TM-T88V", 3: "X1A2B3"})
        printer = usb.describe_device(dev)
        assert printer is not None
        assert printer.fingerprint == "usb:04b8:0202:X1A2B3"
        assert printer.connection_type == "usb"
        assert printer.discovered_via == "usb"
        assert printer.display_name == "EPSON TM-T88V"
        payload = printer.to_payload()
        assert payload["details"] == {
            "manufacturer": "EPSON",
            "product": "TM-T88V",
            "serialNumber": "X1A2B3",
            "usbVendorId": "04b8",
            "usbProductId": "0202",
        }

    def test_printer_class_on_interface_unknown_vendor(self, patched_usb_strings):
        dev = FakeUsbDevice(0x1234, 0x0001, interface_classes=[7], strings={2: "Generic POS"})
        printer = usb.describe_device(dev)
        assert printer is not None
        assert printer.fingerprint == "usb:1234:0001:-"
        assert printer.display_name == "USB 1234 Generic POS"

    def test_non_printer_device_is_skipped(self, patched_usb_strings):
        dev = FakeUsbDevice(0x046D, 0xC52B, interface_classes=[3])  # a mouse
        assert usb.describe_device(dev) is None

    def test_permission_error_on_strings_is_tolerated(self, patched_usb_strings):
        # Without the udev rule the string descriptors are unreadable; the
        # device must still be reported with IDs only.
        dev = FakeUsbDevice(0x0519, 0x0003, string_error=True)
        printer = usb.describe_device(dev)
        assert printer is not None
        assert printer.fingerprint == "usb:0519:0003:-"
        assert printer.display_name == "Star Micronics 0003"
        assert "serialNumber" not in printer.to_payload()["details"]


class TestSpoolerDescribe:
    def test_maps_entries_and_skips_virtual_queues(self):
        entries = [
            {
                "pPrinterName": "EPSON TM-T20III Receipt",
                "pPortName": "USB001",
                "pDriverName": "EPSON TM-T20III Receipt5",
            },
            {
                "pPrinterName": "Microsoft Print to PDF",
                "pPortName": "PORTPROMPT:",
                "pDriverName": "Microsoft Print To PDF",
            },
            {"pPrinterName": "Microsoft XPS Document Writer", "pPortName": "PORTPROMPT:", "pDriverName": "XPS"},
            {"pPrinterName": "OneNote (Desktop)", "pPortName": "nul:", "pDriverName": "Send to Microsoft OneNote"},
            {"pPrinterName": "Fax", "pPortName": "SHRFAX:", "pDriverName": "Microsoft Shared Fax Driver"},
            {"pPrinterName": "Bon", "pPortName": "COM3", "pDriverName": "Generic / Text Only"},
        ]
        found = spooler.describe_printers(entries)
        assert [p.display_name for p in found] == ["EPSON TM-T20III Receipt", "Bon"]
        assert found[0].fingerprint == "win:EPSON TM-T20III Receipt"
        assert found[0].connection_type == "windows"
        assert found[0].discovered_via == "spooler"
        # the spooler port name is not a spec detail key - kept locally only
        assert found[0].to_payload()["details"] == {
            "windowsPrinterName": "EPSON TM-T20III Receipt",
            "driverName": "EPSON TM-T20III Receipt5",
        }
        assert found[0].local_details()["portName"] == "USB001"

    def test_scan_without_pywin32_returns_empty(self, monkeypatch):
        monkeypatch.setattr(spooler, "_SPOOLER_AVAILABLE", False)
        assert spooler.scan_spooler() == []
        assert spooler.spooler_available() is False


class TestMdnsDescribe:
    def test_pdl_datastream_service(self):
        printer = mdns.describe_service(
            "_pdl-datastream._tcp.local.",
            "EPSON TM-T88VI._pdl-datastream._tcp.local.",
            ["192.168.1.50", "fe80::1"],
            9100,
            "epson-tm.local.",
            {b"ty": b"EPSON TM-T88VI", b"usb_MFG": b"EPSON", b"txtvers": b"1"},
        )
        assert printer is not None
        assert printer.fingerprint == "net:192.168.1.50"
        assert printer.display_name == "EPSON TM-T88VI"
        assert printer.discovered_via == "mdns"
        details = printer.to_payload()["details"]
        assert details["ipAddress"] == "192.168.1.50"
        assert details["port"] == 9100
        assert details["hostname"] == "epson-tm.local"
        assert details["model"] == "EPSON TM-T88VI"
        assert details["manufacturer"] == "EPSON"
        assert "mdnsName" not in details  # not a spec key; local only
        assert printer.local_details()["mdnsName"] == "EPSON TM-T88VI"
        assert set(details) <= set(types.DETAIL_KEYS)

    def test_ipp_service_assumes_raw_port(self):
        printer = mdns.describe_service(
            "_ipp._tcp.local.", "Star TSP143._ipp._tcp.local.", ["10.0.0.9"], 631, "star.local.", {}
        )
        assert printer is not None
        assert printer.details["port"] == 9100
        assert printer.display_name == "Star TSP143"

    def test_ipv6_only_is_skipped(self):
        assert (
            mdns.describe_service("_printer._tcp.local.", "x._printer._tcp.local.", ["fe80::1"], 515, None, {}) is None
        )


class TestNeighbours:
    def test_parse_ip_neigh(self):
        output = (
            "192.168.1.50 dev eth0 lladdr 00:26:ab:12:34:56 REACHABLE\n"
            "192.168.1.1 dev eth0 lladdr 11:22:33:44:55:66 STALE\n"
            "192.168.1.77 dev eth0  FAILED\n"
            "fe80::1 dev eth0 lladdr 00:11:22:33:44:55 router REACHABLE\n"
        )
        assert neighbours.parse_ip_neigh(output) == {
            "192.168.1.50": "00:26:ab:12:34:56",
            "192.168.1.1": "11:22:33:44:55:66",
        }

    def test_parse_arp_a_windows(self):
        output = (
            "Interface: 192.168.1.10 --- 0x5\n"
            "  Internet Address      Physical Address      Type\n"
            "  192.168.1.1           a4-2b-8c-11-22-33     dynamic\n"
            "  192.168.1.50          00-26-ab-12-34-56     dynamic\n"
            "  192.168.1.255         ff-ff-ff-ff-ff-ff     static\n"
        )
        assert neighbours.parse_arp_a(output) == {
            "192.168.1.1": "a4:2b:8c:11:22:33",
            "192.168.1.50": "00:26:ab:12:34:56",
        }

    def test_parse_arp_a_macos(self):
        output = (
            "? (192.168.1.50) at 0:26:ab:12:34:56 on en0 ifscope [ethernet]\n"
            "? (192.168.1.51) at 00:26:ab:12:34:57 on en0\n"
        )
        # macOS drops leading zeros in octets - they are padded back
        assert neighbours.parse_arp_a(output) == {
            "192.168.1.50": "00:26:ab:12:34:56",
            "192.168.1.51": "00:26:ab:12:34:57",
        }


class TestMerge:
    def test_network_fingerprint_upgraded_to_mac_and_deduplicated(self):
        a = DiscoveredPrinter(
            "net:192.168.1.50", "network", "192.168.1.50", "portscan", {"ipAddress": "192.168.1.50", "port": 9100}
        )
        b = DiscoveredPrinter(
            "net:192.168.1.50", "network", "EPSON TM-T88VI", "mdns", {"ipAddress": "192.168.1.50", "model": "TM-T88VI"}
        )
        merged = merge_results([a, b], {"192.168.1.50": "00:26:ab:12:34:56"})
        assert len(merged) == 1
        only = merged[0]
        assert only.fingerprint == "net:00:26:ab:12:34:56"
        assert only.details["macAddress"] == "00:26:ab:12:34:56"
        assert only.details["model"] == "TM-T88VI"
        assert only.details["port"] == 9100
        assert only.display_name == "EPSON TM-T88VI"
        assert only.discovered_via == "mdns"

    def test_usb_untouched(self):
        u = DiscoveredPrinter("usb:04b8:0202:-", "usb", "Epson", "usb", {})
        assert merge_results([u], {"1.2.3.4": "aa:bb:cc:dd:ee:ff"})[0].fingerprint == "usb:04b8:0202:-"


class TestPortscan:
    def test_parse_gs_i_reply(self):
        assert portscan.parse_gs_i_reply(b"_TM-T88V\x00") == "TM-T88V"
        assert portscan.parse_gs_i_reply(b"EPSON\x00garbage") == "EPSON"
        assert portscan.parse_gs_i_reply(b"") is None
        assert portscan.parse_gs_i_reply(b"\x00") is None

    def test_resolve_subnet_configured(self):
        assert portscan.resolve_subnet("192.168.1.0/24") == ipaddress.ip_network("192.168.1.0/24")
        # larger ranges are capped to the first /24
        assert portscan.resolve_subnet("10.0.0.0/16") == ipaddress.ip_network("10.0.0.0/24")
        # a host address works too
        assert portscan.resolve_subnet("192.168.7.33/24") == ipaddress.ip_network("192.168.7.0/24")

    async def test_scan_finds_fake_printer_and_reads_model(self):
        """End-to-end against the emulator from tools/fake_printer.py."""
        import importlib.util
        from pathlib import Path

        spec = importlib.util.spec_from_file_location("fake_printer", Path("tools/fake_printer.py"))
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        fake = module.FakePrinter("OPENEOS", "FAKE-9100", flush_after=0.2)
        server = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            found = await portscan.scan_subnet(ipaddress.ip_network("127.0.0.1/32"), port=port, timeout_ms=300)
        finally:
            server.close()
            await server.wait_closed()
        assert len(found) == 1
        printer = found[0]
        assert printer.fingerprint == "net:127.0.0.1"
        assert printer.discovered_via == "portscan"
        assert printer.display_name == "OPENEOS FAKE-9100"
        assert printer.details["port"] == port
        assert printer.details["model"] == "FAKE-9100"

    async def test_scan_nothing_open(self):
        found = await portscan.scan_subnet(ipaddress.ip_network("127.0.0.1/32"), port=1, timeout_ms=100)
        assert found == []


class TestDiscoveryService:
    async def test_scan_merges_all_sources_and_reports_full(self):
        reports: list[tuple[list[dict[str, Any]], bool]] = []

        async def reporter(printers: list[dict[str, Any]], full: bool) -> None:
            reports.append((printers, full))

        async def fake_mdns(timeout: float) -> list[DiscoveredPrinter]:
            return [DiscoveredPrinter("net:10.0.0.5", "network", "Star", "mdns", {"ipAddress": "10.0.0.5"})]

        async def fake_subnet(subnet: Any, **kwargs: Any) -> list[DiscoveredPrinter]:
            return [
                DiscoveredPrinter(
                    "net:10.0.0.5", "network", "10.0.0.5", "portscan", {"ipAddress": "10.0.0.5", "port": 9100}
                )
            ]

        service = DiscoveryService(
            DiscoveryConfig(mdns_timeout_s=0.01),
            reporter=reporter,
            usb_scanner=lambda: [DiscoveredPrinter("usb:04b8:0202:-", "usb", "Epson", "usb", {})],
            spooler_scanner=lambda: [],
            mdns_scanner=fake_mdns,
            subnet_scanner=fake_subnet,
            mac_lookup=lambda: {"10.0.0.5": "aa:bb:cc:dd:ee:ff"},
        )
        printers = await service.scan_and_report(subnet_scan=True)
        assert {p.fingerprint for p in printers} == {"usb:04b8:0202:-", "net:aa:bb:cc:dd:ee:ff"}
        assert len(reports) == 1
        payload, full = reports[0]
        assert full is True
        assert {p["fingerprint"] for p in payload} == {"usb:04b8:0202:-", "net:aa:bb:cc:dd:ee:ff"}
        assert service.get_status_dict()["lastScanAt"] is not None

    async def test_failing_scanner_does_not_hide_others(self):
        def broken() -> list[DiscoveredPrinter]:
            raise RuntimeError("no libusb")

        async def fake_mdns(timeout: float) -> list[DiscoveredPrinter]:
            return []

        service = DiscoveryService(
            DiscoveryConfig(mdns_timeout_s=0.01),
            usb_scanner=broken,
            spooler_scanner=lambda: [DiscoveredPrinter("win:Bon", "windows", "Bon", "spooler", {})],
            mdns_scanner=fake_mdns,
            mac_lookup=lambda: {},
        )
        printers = await service.scan()
        assert [p.fingerprint for p in printers] == ["win:Bon"]
        assert service.last_error is not None and "usb" in service.last_error

    async def test_trigger_wakes_loop(self):
        calls: list[bool] = []

        async def fake_mdns(timeout: float) -> list[DiscoveredPrinter]:
            return []

        async def fake_subnet(subnet: Any, **kwargs: Any) -> list[DiscoveredPrinter]:
            calls.append(True)
            return []

        service = DiscoveryService(
            DiscoveryConfig(interval_s=3600, mdns_timeout_s=0.01),
            usb_scanner=lambda: [],
            spooler_scanner=lambda: [],
            mdns_scanner=fake_mdns,
            subnet_scanner=fake_subnet,
            mac_lookup=lambda: {},
        )
        service.start()
        await asyncio.sleep(0.2)
        assert service.last_scan_at is not None
        assert calls == []  # scheduled runs do not port-scan by default
        service.trigger(subnet_scan=True)
        await asyncio.sleep(0.3)
        assert calls == [True]
        await service.stop()
