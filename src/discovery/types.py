"""Shared data types for printer discovery.

A ``DiscoveredPrinter`` is what the scanners produce and what the agent
reports to ``POST /device-api/printers/discovered``; ``to_payload()`` yields
exactly the ``DiscoveredPrinterInput`` shape from the rework spec (section 4).
"""

from dataclasses import dataclass, field
from typing import Any, Literal, Optional

ConnectionType = Literal["usb", "network", "windows"]
DiscoveredVia = Literal["usb", "mdns", "portscan", "spooler", "manual"]

# USB vendor IDs of common ESC/POS printer makers (spec section 7). Devices
# from these vendors are reported even when they do not advertise the USB
# printer class - several cheap models enumerate as vendor-specific.
KNOWN_USB_VENDORS: dict[int, str] = {
    0x04B8: "Epson",
    0x0519: "Star Micronics",
    0x1504: "Bixolon",
    0x0DD4: "Custom",
    0x0416: "Winbond",
    0x28E9: "GD Microelectronics",
    0x0483: "STMicroelectronics",
    0x6868: "Generic (6868)",
    0x1FC9: "NXP",
    0x1D90: "Citizen",
    0x1343: "Citizen Systems",
    0x0619: "Seiko Instruments",
    0x0D3A: "Posiflex",
    0x076C: "Partner Tech",
}

USB_PRINTER_CLASS = 7

# The ``details`` keys of DiscoveredPrinterInput (spec section 4). The API
# validates with forbidNonWhitelisted, so anything else collected along the
# way (mDNS instance name, spooler port name) stays local - visible on the
# status page, never sent.
DETAIL_KEYS = (
    "manufacturer",
    "product",
    "model",
    "serialNumber",
    "usbVendorId",
    "usbProductId",
    "ipAddress",
    "port",
    "hostname",
    "macAddress",
    "windowsPrinterName",
    "driverName",
    # classification (see classify.py)
    "escpos",
    "kind",
    "kindReason",
)


def usb_fingerprint(vendor_id: int, product_id: int, serial: Optional[str]) -> str:
    """``usb:<vendorId>:<productId>:<serial|'-'>`` with 4-digit lowercase hex IDs."""
    serial_part = serial.strip() if serial and serial.strip() else "-"
    return f"usb:{vendor_id:04x}:{product_id:04x}:{serial_part}"


def network_fingerprint(mac: Optional[str], ip: str) -> str:
    """``net:<mac|ip>`` - MAC preferred, IP as fallback."""
    normalized = normalize_mac(mac)
    return f"net:{normalized}" if normalized else f"net:{ip}"


def windows_fingerprint(printer_name: str) -> str:
    """``win:<printerName>``."""
    return f"win:{printer_name}"


def normalize_mac(mac: Optional[str]) -> Optional[str]:
    """Lowercase, colon-separated MAC or None for empty/incomplete values
    (``ip neigh`` lists FAILED entries without an address; ``arp -a`` on
    Windows uses dashes)."""
    if not mac:
        return None
    parts = mac.strip().lower().replace("-", ":").split(":")
    if len(parts) != 6 or any(not p or len(p) > 2 or any(c not in "0123456789abcdef" for c in p) for p in parts):
        return None
    cleaned = ":".join(p.zfill(2) for p in parts)  # macOS prints "0:26:ab:..."
    if cleaned in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    return cleaned


def hex4(value: int) -> str:
    """USB IDs as the backend stores them in connectionConfig (``04b8``)."""
    return f"{value:04x}"


@dataclass
class DiscoveredPrinter:
    fingerprint: str
    connection_type: ConnectionType
    display_name: str
    discovered_via: DiscoveredVia
    details: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """``DiscoveredPrinterInput`` for the API; only the spec's detail
        keys, empty values dropped, ``port`` only when numeric."""
        details: dict[str, Any] = {}
        for key in DETAIL_KEYS:
            value = self.details.get(key)
            if value in (None, ""):
                continue
            if key == "port" and not isinstance(value, int):
                continue
            details[key] = value
        return {
            "fingerprint": self.fingerprint,
            "connectionType": self.connection_type,
            "displayName": self.display_name,
            "details": details,
            "discoveredVia": self.discovered_via,
        }

    def local_details(self) -> dict[str, Any]:
        """Everything known, for the status page."""
        return {k: v for k, v in self.details.items() if v not in (None, "")}

    def merge(self, other: "DiscoveredPrinter") -> None:
        """Fold details of a second sighting (e.g. mDNS + port scan of the
        same host) into this record without losing anything already known."""
        for key, value in other.details.items():
            if value not in (None, "") and not self.details.get(key):
                self.details[key] = value
        if (not self.display_name or self.display_name == self.details.get("ipAddress")) and other.display_name:
            self.display_name = other.display_name
