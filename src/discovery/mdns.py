"""mDNS / DNS-SD discovery of network printers via zeroconf.

Browses the service types receipt printers commonly announce
(``_pdl-datastream._tcp`` = raw port 9100, ``_printer._tcp`` = LPD,
``_ipp._tcp``) for a few seconds and collects IP, hostname, port and the
model strings from the TXT record.
"""

import asyncio
import logging
from typing import Any, Optional

from .types import DiscoveredPrinter, network_fingerprint

logger = logging.getLogger(__name__)

SERVICE_TYPES = ("_pdl-datastream._tcp.local.", "_printer._tcp.local.", "_ipp._tcp.local.")

try:
    from zeroconf import ServiceStateChange, Zeroconf
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

    _ZEROCONF_AVAILABLE = True
except Exception:  # pragma: no cover - depends on host packages
    _ZEROCONF_AVAILABLE = False


def mdns_available() -> bool:
    return _ZEROCONF_AVAILABLE


def _txt(properties: dict[Any, Any], key: str) -> Optional[str]:
    """TXT values arrive as bytes keyed by bytes; tolerate str too."""
    for k, v in properties.items():
        name = k.decode("utf-8", "ignore") if isinstance(k, bytes) else str(k)
        if name.lower() != key.lower():
            continue
        if v is None:
            return None
        value = v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
        return value.strip() or None
    return None


def describe_service(
    service_type: str,
    name: str,
    addresses: list[str],
    port: Optional[int],
    server: Optional[str],
    properties: dict[Any, Any],
) -> Optional[DiscoveredPrinter]:
    """Build a DiscoveredPrinter from resolved service data (pure, testable).

    The fingerprint is IP-based at this stage; the discovery service swaps in
    the MAC from the neighbour table afterwards when it knows one.
    """
    ipv4 = [a for a in addresses if ":" not in a]
    if not ipv4:
        return None
    ip = ipv4[0]
    hostname = (server or "").rstrip(".") or None
    model = _txt(properties, "ty") or _txt(properties, "product") or _txt(properties, "usb_MDL")
    manufacturer = _txt(properties, "usb_MFG")
    if model:
        model = model.strip("()")
    instance = name
    for suffix in SERVICE_TYPES:
        if instance.endswith("." + suffix):
            instance = instance[: -(len(suffix) + 1)]
    display = model or instance or hostname or ip

    # Raw 9100 is what we print to. LPD/IPP announcements tell us the printer
    # exists; assume the raw port unless the service itself is pdl-datastream.
    raw_port = port if service_type.startswith("_pdl-datastream") and port else 9100

    return DiscoveredPrinter(
        fingerprint=network_fingerprint(None, ip),
        connection_type="network",
        display_name=display,
        discovered_via="mdns",
        details={
            "ipAddress": ip,
            "port": raw_port,
            "hostname": hostname,
            "model": model,
            "manufacturer": manufacturer,
            "mdnsName": instance,
            "mdnsService": service_type.rstrip("."),
        },
    )


async def scan_mdns(timeout_s: float = 3.0) -> list[DiscoveredPrinter]:
    """Browse for printer services for ``timeout_s`` seconds."""
    if not _ZEROCONF_AVAILABLE:
        return []

    found: dict[str, DiscoveredPrinter] = {}
    pending: set[asyncio.Task[None]] = set()
    aiozc: Optional[AsyncZeroconf] = None

    async def resolve(zc: Zeroconf, service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        try:
            if not await info.async_request(zc, 1500):
                return
        except Exception as e:
            logger.debug(f"mDNS resolve failed for {name}: {e}")
            return
        addresses = [str(a) for a in info.parsed_addresses()]
        printer = describe_service(service_type, name, addresses, info.port, info.server, info.properties or {})
        if printer is None:
            return
        existing = found.get(printer.fingerprint)
        if existing:
            existing.merge(printer)
        else:
            found[printer.fingerprint] = printer

    def on_change(zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Added or state_change is ServiceStateChange.Updated:
            task = asyncio.ensure_future(resolve(zeroconf, service_type, name))
            pending.add(task)
            task.add_done_callback(pending.discard)

    try:
        aiozc = AsyncZeroconf()
        browser = AsyncServiceBrowser(aiozc.zeroconf, list(SERVICE_TYPES), handlers=[on_change])
        await asyncio.sleep(timeout_s)
        if pending:
            await asyncio.wait(pending, timeout=2.0)
        await browser.async_cancel()
    except Exception as e:
        # No multicast route (container, VPN-only host) or a firewall that
        # drops 5353 - log and move on, the other scanners still run.
        logger.warning(f"mDNS scan failed: {e}")
    finally:
        if aiozc is not None:
            try:
                await aiozc.async_close()
            except Exception:
                pass

    logger.info(f"mDNS scan: {len(found)} printer(s)")
    return list(found.values())
