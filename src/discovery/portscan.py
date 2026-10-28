"""Port-9100 sweep of the local /24 (on request only) and the ESC/POS
model query used to tell receipt printers from everything else.

The sweep is a TCP connect per host with a short timeout and bounded
parallelism; hosts with the port open are reported with their IP only.
``probe_printer`` then asks a network candidate (from the sweep or from
mDNS) for its model with ``GS I`` - the discovery service runs it once per
device and scan, see ``service.py``.
"""

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Optional

from .types import DiscoveredPrinter, network_fingerprint

logger = logging.getLogger(__name__)

RAW_PORT = 9100
MAX_HOSTS = 256
PROBE_TIMEOUT_S = 1.0

# GS I n: n=65 (A) firmware version, n=66 (B) maker name, n=67 (C) model
# name. Replies are "_" + text + NUL.
GS_I_FIRMWARE = b"\x1d\x49\x41"
GS_I_MAKER = b"\x1d\x49\x42"
GS_I_MODEL = b"\x1d\x49\x43"


@dataclass
class ProbeResult:
    """Outcome of one ESC/POS model query."""

    connected: bool
    maker: Optional[str] = None
    model: Optional[str] = None
    firmware: Optional[str] = None

    @property
    def answered(self) -> bool:
        return bool(self.maker or self.model or self.firmware)


def local_ipv4() -> Optional[str]:
    """IP of the interface that carries the default route (no packets are
    sent - connecting a UDP socket only selects the route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = str(s.getsockname()[0])
            return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None


def _private_interface_ipv4() -> Optional[str]:
    """First RFC1918 address of any interface - fallback when the default
    route points at a public IP (hosted servers)."""
    try:
        import psutil

        for iface, addrs in psutil.net_if_addrs().items():
            if iface == "lo":
                continue
            for addr in addrs:
                if addr.family != socket.AF_INET:
                    continue
                try:
                    ip = ipaddress.ip_address(addr.address)
                except ValueError:
                    continue
                if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                    return str(ip)
    except Exception as e:
        logger.debug(f"Interface enumeration failed: {e}")
    return None


def resolve_subnet(configured: Optional[str] = None) -> Optional[ipaddress.IPv4Network]:
    """Subnet to sweep: explicit CIDR from the config, else the /24 of the
    default-route interface (or of the first private interface when the
    default route is public). Never larger than /24 (MAX_HOSTS)."""
    if configured:
        try:
            net = ipaddress.ip_network(configured, strict=False)
        except ValueError:
            logger.warning(f"Invalid discovery.subnet {configured!r}; falling back to local /24")
            net = None
        if isinstance(net, ipaddress.IPv4Network):
            if net.num_addresses > MAX_HOSTS:
                logger.warning(f"discovery.subnet {configured} larger than /24; scanning the first /24 only")
                net = ipaddress.ip_network(f"{net.network_address}/24", strict=False)
            return net if isinstance(net, ipaddress.IPv4Network) else None

    ip = local_ipv4()
    if ip is None or not ipaddress.ip_address(ip).is_private:
        ip = _private_interface_ipv4() or ip
    if ip is None:
        return None
    net = ipaddress.ip_network(f"{ip}/24", strict=False)
    return net if isinstance(net, ipaddress.IPv4Network) else None


def parse_gs_i_reply(data: bytes) -> Optional[str]:
    """Extract the text from a ``GS I`` reply: ``_`` + ASCII + NUL."""
    if not data:
        return None
    if data.startswith(b"_"):
        data = data[1:]
    text = data.split(b"\x00", 1)[0]
    cleaned = "".join(ch for ch in text.decode("ascii", "ignore") if ch.isprintable()).strip()
    return cleaned or None


async def query_model(ip: str, port: int, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Best-effort ESC/POS model query. Any failure yields a silent result.

    Asks for the model first (``GS I 67``); a printer that answers is also
    asked for its maker. One that stays silent gets a last chance with the
    firmware query (``GS I 65``), which some clones implement instead.
    Nothing is ever retried, so a non-ESC/POS device sees at most two short
    writes.
    """
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except Exception:
        return ProbeResult(connected=False)
    result = ProbeResult(connected=True)

    async def ask(query: bytes) -> Optional[str]:
        writer.write(query)
        await writer.drain()
        try:
            reply = await asyncio.wait_for(reader.readuntil(b"\x00"), timeout)
        except Exception:
            return None
        return parse_gs_i_reply(reply)

    try:
        result.model = await ask(GS_I_MODEL)
        if result.model:
            result.maker = await ask(GS_I_MAKER)
        else:
            result.firmware = await ask(GS_I_FIRMWARE)
    except Exception:
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return result


async def probe_printer(printer: DiscoveredPrinter, timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run the model query against a network candidate and fold the answer
    into its details (maker/model/display name). The classification itself
    is the service's job."""
    ip = str(printer.details.get("ipAddress") or "")
    port = printer.details.get("port")
    if not ip:
        return ProbeResult(connected=False)
    result = await query_model(ip, int(port) if isinstance(port, int) else RAW_PORT, timeout)
    if result.answered:
        if result.maker and not printer.details.get("manufacturer"):
            printer.details["manufacturer"] = result.maker
        if result.model and not printer.details.get("model"):
            printer.details["model"] = result.model
        if printer.display_name in ("", ip):
            printer.display_name = " ".join(p for p in (result.maker, result.model) if p) or ip
    return result


async def probe_host(ip: str, port: int, timeout: float) -> bool:
    """TCP connect test. Refused/timeout = no printer."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def scan_subnet(
    subnet: Optional[ipaddress.IPv4Network],
    port: int = RAW_PORT,
    timeout_ms: int = 300,
    concurrency: int = 64,
) -> list[DiscoveredPrinter]:
    """Sweep ``subnet`` for hosts with ``port`` open and describe them."""
    if subnet is None:
        logger.warning("Port scan skipped: no local IPv4 subnet found")
        return []

    timeout = max(timeout_ms, 50) / 1000.0
    semaphore = asyncio.Semaphore(max(1, concurrency))
    hosts = [str(h) for h in subnet.hosts()][:MAX_HOSTS]

    async def probe(ip: str) -> Optional[str]:
        async with semaphore:
            return ip if await probe_host(ip, port, timeout) else None

    results = await asyncio.gather(*(probe(ip) for ip in hosts), return_exceptions=True)
    hits = [r for r in results if isinstance(r, str)]

    found = [
        DiscoveredPrinter(
            fingerprint=network_fingerprint(None, ip),
            connection_type="network",
            display_name=ip,
            discovered_via="portscan",
            details={"ipAddress": ip, "port": port},
        )
        for ip in hits
    ]
    logger.info(f"Port scan {subnet}: {len(found)} host(s) with port {port} open")
    return found
