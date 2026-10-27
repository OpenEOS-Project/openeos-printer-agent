"""IP -> MAC lookup from the OS neighbour/ARP table, best effort.

Used to give network printers a stable fingerprint (``net:<mac>``) that
survives DHCP lease changes. Linux: ``ip neigh``; Windows/macOS: ``arp -a``.
Anything that fails (tool missing, no permission, odd output) yields an
empty mapping and the fingerprint falls back to the IP.
"""

import logging
import re
import subprocess
import sys
from typing import Optional

from .types import normalize_mac

logger = logging.getLogger(__name__)

_IP_NEIGH_RE = re.compile(r"^(?P<ip>\d+\.\d+\.\d+\.\d+)\s.*\blladdr\s+(?P<mac>[0-9a-fA-F:]{17})\b")
# Windows: "192.168.1.50   00-26-ab-12-34-56   dynamic"
# macOS:   "? (192.168.1.50) at 0:26:ab:12:34:56 on en0 ..." (octets unpadded)
_ARP_A_RE = re.compile(
    r"(?P<ip>\d+\.\d+\.\d+\.\d+)\)?\s+(?:at\s+)?(?P<mac>[0-9a-fA-F]{1,2}([-:][0-9a-fA-F]{1,2}){5})\b"
)


def parse_ip_neigh(output: str) -> dict[str, str]:
    """Parse ``ip -4 neigh`` output (Linux)."""
    table: dict[str, str] = {}
    for line in output.splitlines():
        match = _IP_NEIGH_RE.match(line.strip())
        if not match:
            continue
        mac = normalize_mac(match.group("mac"))
        if mac:
            table[match.group("ip")] = mac
    return table


def parse_arp_a(output: str) -> dict[str, str]:
    """Parse ``arp -a`` output (Windows uses dashes, macOS uses colons)."""
    table: dict[str, str] = {}
    for line in output.splitlines():
        match = _ARP_A_RE.search(line)
        if not match:
            continue
        mac = normalize_mac(match.group("mac"))
        if mac:
            table[match.group("ip")] = mac
    return table


def _run(cmd: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.debug(f"{cmd[0]} unavailable: {e}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def lookup_macs() -> dict[str, str]:
    """Current neighbour table as {ip: mac}. Synchronous - run in an executor."""
    if sys.platform.startswith("linux"):
        output = _run(["ip", "-4", "neigh"])
        if output:
            return parse_ip_neigh(output)
    output = _run(["arp", "-a"])
    if output:
        return parse_arp_a(output)
    return {}
