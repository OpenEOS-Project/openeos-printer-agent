"""Windows print-spooler discovery via ``win32print.EnumPrinters``.

Any locally installed printer queue becomes a ``windows`` candidate; the
agent later prints to it raw through ``escpos.printer.Win32Raw``. Virtual
queues Windows ships by default (PDF, XPS, OneNote, Fax) are skipped - raw
ESC/POS sent there only produces garbage documents.
"""

import logging
from typing import Any

from .types import DiscoveredPrinter, windows_fingerprint

logger = logging.getLogger(__name__)

try:
    import win32print

    _SPOOLER_AVAILABLE = True
except Exception:  # pragma: no cover - only on Windows
    win32print = None
    _SPOOLER_AVAILABLE = False

VIRTUAL_PRINTER_MARKERS = (
    "microsoft print to pdf",
    "microsoft xps document writer",
    "onenote",
    "fax",
    "adobe pdf",
)


def spooler_available() -> bool:
    return _SPOOLER_AVAILABLE


def _is_virtual(name: str, driver: str, port: str) -> bool:
    haystack = f"{name} {driver} {port}".lower()
    return any(marker in haystack for marker in VIRTUAL_PRINTER_MARKERS)


def describe_printers(entries: list[dict[str, Any]]) -> list[DiscoveredPrinter]:
    """Map ``EnumPrinters(level=2)`` dicts to DiscoveredPrinters (pure, testable)."""
    found: list[DiscoveredPrinter] = []
    for entry in entries:
        name = str(entry.get("pPrinterName") or "").strip()
        if not name:
            continue
        driver = str(entry.get("pDriverName") or "")
        port = str(entry.get("pPortName") or "")
        if _is_virtual(name, driver, port):
            continue
        found.append(
            DiscoveredPrinter(
                fingerprint=windows_fingerprint(name),
                connection_type="windows",
                display_name=name,
                discovered_via="spooler",
                details={
                    "windowsPrinterName": name,
                    "driverName": driver or None,
                    "portName": port or None,
                    "model": str(entry.get("pComment") or "") or None,
                },
            )
        )
    return found


def scan_spooler() -> list[DiscoveredPrinter]:
    """Enumerate local spooler queues. Synchronous - run in an executor."""
    if not _SPOOLER_AVAILABLE:
        return []
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        entries = win32print.EnumPrinters(flags, None, 2)
    except Exception as e:
        logger.warning(f"Spooler enumeration failed: {e}")
        return []
    found = describe_printers([dict(e) for e in entries])
    logger.info(f"Spooler scan: {len(found)} printer(s)")
    return found
