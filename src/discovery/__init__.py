"""Printer discovery (USB, Windows spooler, mDNS, optional port scan)."""

from .service import DiscoveryService, merge_results
from .types import (
    KNOWN_USB_VENDORS,
    DiscoveredPrinter,
    network_fingerprint,
    normalize_mac,
    usb_fingerprint,
    windows_fingerprint,
)

__all__ = [
    "DiscoveredPrinter",
    "DiscoveryService",
    "KNOWN_USB_VENDORS",
    "merge_results",
    "network_fingerprint",
    "normalize_mac",
    "usb_fingerprint",
    "windows_fingerprint",
]
