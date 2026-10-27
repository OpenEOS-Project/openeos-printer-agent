from unittest.mock import MagicMock

import pytest

from src import printer_manager
from src.printer_manager import ManagedPrinter, PrinterManager, PrinterStatus, _parse_usb_id


class TestParseUsbId:
    def test_formats(self):
        assert _parse_usb_id("04b8") == 0x04B8
        assert _parse_usb_id("0x04B8") == 0x04B8
        assert _parse_usb_id(0x0202) == 0x0202


class TestBackends:
    async def test_dummy_mode_logs_rendered_text(self, caplog):
        printer = ManagedPrinter(
            {
                "id": "p1",
                "name": "Bon",
                "connectionType": "usb",
                "connectionConfig": {},
                "fingerprint": "usb:04b8:0202:-",
            },
            dummy=True,
        )
        assert await printer.connect() is True
        assert printer.status is PrinterStatus.ONLINE

        with caplog.at_level("INFO"):
            await printer.execute(lambda p: (p.text("Testbon 42\n"), p.cut()))
        assert "dummy printer 'Bon'" in caplog.text
        assert "Testbon 42" in caplog.text
        assert printer.get_heartbeat_dict() == {"fingerprint": "usb:04b8:0202:-", "printerId": "p1", "online": True}

    def test_network_backend(self, monkeypatch):
        created = {}

        def fake_network(host: str, port: int = 9100, **kwargs):
            created.update(host=host, port=port)
            return MagicMock()

        monkeypatch.setattr(printer_manager, "Network", fake_network)
        printer = ManagedPrinter(
            {
                "id": "p2",
                "name": "Kueche",
                "connectionType": "network",
                "connectionConfig": {"ipAddress": "10.0.0.5", "port": "9100"},
            }
        )
        printer._create_escpos()
        assert created == {"host": "10.0.0.5", "port": 9100}

    def test_network_requires_ip(self):
        printer = ManagedPrinter({"id": "p", "name": "x", "connectionType": "network", "connectionConfig": {}})
        with pytest.raises(ValueError, match="ipAddress"):
            printer._create_escpos()

    def test_usb_backend_with_serial_then_fallback(self, monkeypatch):
        calls = []

        def fake_usb(vendor: int, product: int, usb_args=None, **kwargs):
            calls.append((vendor, product, usb_args))
            if usb_args:
                raise RuntimeError("serial not readable")
            return MagicMock()

        monkeypatch.setattr(printer_manager, "Usb", fake_usb)
        printer = ManagedPrinter(
            {
                "id": "p3",
                "name": "Epson",
                "connectionType": "usb",
                "connectionConfig": {"usbVendorId": "04b8", "usbProductId": "0202", "usbSerialNumber": "S1"},
            }
        )
        printer._create_escpos()
        assert calls == [(0x04B8, 0x0202, {"serial_number": "S1"}), (0x04B8, 0x0202, None)]

    def test_windows_backend(self, monkeypatch):
        fake_win32raw = MagicMock()
        monkeypatch.setattr(printer_manager, "Win32Raw", fake_win32raw)
        printer = ManagedPrinter(
            {
                "id": "p4",
                "name": "Bon",
                "connectionType": "windows",
                "connectionConfig": {"windowsPrinterName": "EPSON TM-T20"},
            }
        )
        printer._create_escpos()
        fake_win32raw.assert_called_once_with("EPSON TM-T20")

    def test_windows_backend_without_pywin32(self, monkeypatch):
        monkeypatch.setattr(printer_manager, "Win32Raw", None)
        printer = ManagedPrinter(
            {"id": "p4", "name": "Bon", "connectionType": "windows", "connectionConfig": {"windowsPrinterName": "X"}}
        )
        with pytest.raises(RuntimeError, match="pywin32"):
            printer._create_escpos()

    def test_unknown_connection_type(self):
        printer = ManagedPrinter({"id": "p", "name": "x", "connectionType": "bluetooth", "connectionConfig": {}})
        with pytest.raises(ValueError, match="Unsupported"):
            printer._create_escpos()


class TestManagerReconfigure:
    async def test_fingerprint_change_triggers_reconnect(self):
        pm = PrinterManager(dummy=True)
        await pm.initialize([{"id": "p1", "name": "A", "connectionType": "usb", "fingerprint": "usb:1:2:-"}])
        first = pm.get_printer("p1")
        await pm.reconfigure([{"id": "p1", "name": "A", "connectionType": "usb", "fingerprint": "usb:1:2:S"}])
        assert pm.get_printer("p1") is not first
        await pm.reconfigure([])
        assert pm.get_all_printers() == []
