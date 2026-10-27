import platform
import socket

from src.printer_manager import PrinterManager
from src.system_monitor import SystemMonitor, agent_identity, normalize_arch
from src.websocket_client import WebSocketClient


class TestAgentHeartbeat:
    async def test_payload_matches_spec(self, sample_config):
        pm = PrinterManager(dummy=True)
        await pm.initialize(
            [
                {"id": "p1", "name": "Bon", "connectionType": "usb", "fingerprint": "usb:04b8:0202:-"},
                {"id": "p2", "name": "Manuell", "connectionType": "network", "fingerprint": None},
            ]
        )
        client = WebSocketClient(
            config=sample_config,
            device_token="tok",
            device_id="dev-1",
            system_monitor=SystemMonitor(),
            capabilities=lambda: {"usb": True, "mdns": True, "portscan": True, "spooler": False},
        )
        client.set_printer_manager(pm)

        payload = client.build_agent_heartbeat()

        assert payload["version"] == "0.2.1"
        assert payload["os"] in ("linux", "windows", "darwin")
        assert payload["hostname"] == socket.gethostname()
        assert payload["pythonVersion"] == platform.python_version()
        assert payload["arch"] == normalize_arch(platform.machine())
        assert payload["capabilities"] == {"usb": True, "mdns": True, "portscan": True, "spooler": False}
        assert payload["printers"] == [
            {"fingerprint": "usb:04b8:0202:-", "printerId": "p1", "online": True},
            {"fingerprint": None, "printerId": "p2", "online": True},
        ]
        await pm.shutdown()

    def test_normalize_arch(self):
        assert normalize_arch("AMD64") == "x86_64"
        assert normalize_arch("x86_64") == "x86_64"
        assert normalize_arch("aarch64") == "arm64"
        assert normalize_arch("armv7l") == "armv7"

    def test_identity_keys(self):
        assert set(agent_identity()) == {"os", "arch", "hostname", "pythonVersion"}
