"""USB discovery via pyusb.

Reports every device that either carries the USB printer class (7) on the
device or on one of its interfaces, or belongs to a known receipt-printer
vendor. String descriptors (manufacturer/product/serial) need an open
handle, which fails without the udev rule on Linux - those failures are
tolerated and the device is still reported with IDs only.
"""

import logging
from typing import Any, Optional

from .types import KNOWN_USB_VENDORS, USB_PRINTER_CLASS, DiscoveredPrinter, hex4, usb_fingerprint

logger = logging.getLogger(__name__)

try:
    import usb.core
    import usb.util

    _PYUSB_AVAILABLE = True
except Exception:  # pragma: no cover - depends on host packages
    _PYUSB_AVAILABLE = False


def _find_backend() -> Any:
    """On Windows pyusb has no system libusb; the optional ``libusb-package``
    wheel ships one. Elsewhere the default backend search is fine."""
    try:
        import libusb_package

        return libusb_package.get_libusb1_backend()
    except Exception:
        return None


def usb_available() -> bool:
    """Capability flag for the heartbeat: can we enumerate USB at all?"""
    if not _PYUSB_AVAILABLE:
        return False
    try:
        list(usb.core.find(find_all=True, backend=_find_backend()))
        return True
    except Exception as e:
        logger.debug(f"USB enumeration unavailable: {e}")
        return False


def _safe_string(device: Any, index: Optional[int]) -> Optional[str]:
    """Read a string descriptor; None when the index is 0 or the OS refuses
    (permission denied, device busy, broken descriptor)."""
    if not index:
        return None
    try:
        value = usb.util.get_string(device, index)
    except Exception as e:
        logger.debug(f"USB string descriptor {index} unreadable: {e}")
        return None
    if value is None:
        return None
    return str(value).strip() or None


def _is_printer(device: Any) -> bool:
    vendor = int(getattr(device, "idVendor", 0))
    if vendor in KNOWN_USB_VENDORS:
        return True
    if int(getattr(device, "bDeviceClass", 0)) == USB_PRINTER_CLASS:
        return True
    try:
        for config in device:
            for interface in config:
                if int(getattr(interface, "bInterfaceClass", 0)) == USB_PRINTER_CLASS:
                    return True
    except Exception as e:
        # Reading the configuration descriptor can fail without permissions;
        # the vendor check above already caught the common printers.
        logger.debug(f"USB configuration unreadable for {vendor:04x}: {e}")
    return False


def describe_device(device: Any) -> Optional[DiscoveredPrinter]:
    """Turn one pyusb device into a DiscoveredPrinter (None if not a printer)."""
    if not _is_printer(device):
        return None

    vendor_id = int(device.idVendor)
    product_id = int(device.idProduct)
    manufacturer = _safe_string(device, getattr(device, "iManufacturer", 0))
    product = _safe_string(device, getattr(device, "iProduct", 0))
    serial = _safe_string(device, getattr(device, "iSerialNumber", 0))

    vendor_name = manufacturer or KNOWN_USB_VENDORS.get(vendor_id, f"USB {vendor_id:04x}")
    display = f"{vendor_name} {product}".strip() if product else f"{vendor_name} {product_id:04x}"

    return DiscoveredPrinter(
        fingerprint=usb_fingerprint(vendor_id, product_id, serial),
        connection_type="usb",
        display_name=display,
        discovered_via="usb",
        details={
            "manufacturer": manufacturer,
            "product": product,
            "serialNumber": serial,
            "usbVendorId": hex4(vendor_id),
            "usbProductId": hex4(product_id),
        },
    )


def scan_usb() -> list[DiscoveredPrinter]:
    """Enumerate USB printers. Synchronous (pyusb is blocking) - run in an executor."""
    if not _PYUSB_AVAILABLE:
        return []
    found: list[DiscoveredPrinter] = []
    try:
        devices = usb.core.find(find_all=True, backend=_find_backend())
    except Exception as e:
        logger.warning(f"USB enumeration failed: {e}")
        return []
    for device in devices or []:
        try:
            printer = describe_device(device)
        except Exception as e:
            logger.debug(f"Skipping unreadable USB device: {e}")
            continue
        if printer is not None:
            found.append(printer)
    logger.info(f"USB scan: {len(found)} printer(s)")
    return found
