"""Printer discovery (USB, Windows spooler, mDNS, optional port scan)."""

from .classify import Classification, classify_spooler, classify_usb
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
    "Classification",
    "classify_spooler",
    "classify_usb",
    "DiscoveredPrinter",
    "DiscoveryService",
    "KNOWN_USB_VENDORS",
    "merge_results",
    "network_fingerprint",
    "normalize_mac",
    "usb_fingerprint",
    "windows_fingerprint",
]
