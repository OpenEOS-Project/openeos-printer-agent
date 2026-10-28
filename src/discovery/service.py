"""Discovery orchestration: run the scanners, merge their results, attach
MACs, classify the candidates and hand the list to the reporter
(``POST /device-api/printers/discovered``).

Runs once at start, then every ``discovery.interval_s`` (5 minutes), and on
the ``printerDiscover`` socket event. The subnet port scan is part of a run
only when explicitly requested (``subnetScan: true``) or enabled in config.

Network candidates are asked for their model with ESC/POS ``GS I`` exactly
once per scan; the verdict is cached per fingerprint for the agent's
lifetime so the 5-minute runs stay quiet. An explicit "search again" from
the web app (``printerDiscover``) clears the cache and asks everything anew.
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from ..config import DiscoveryConfig
from . import mdns, neighbours, portscan, spooler, usb
from .classify import Classification, classify_network_probe, classify_network_without_probe
from .types import DiscoveredPrinter, network_fingerprint

logger = logging.getLogger(__name__)

Reporter = Callable[[list[dict[str, Any]], bool], Awaitable[Any]]
Prober = Callable[[DiscoveredPrinter, float], Awaitable[portscan.ProbeResult]]


class DiscoveryService:
    def __init__(
        self,
        config: DiscoveryConfig,
        reporter: Optional[Reporter] = None,
        *,
        usb_scanner: Optional[Callable[[], list[DiscoveredPrinter]]] = None,
        spooler_scanner: Optional[Callable[[], list[DiscoveredPrinter]]] = None,
        mdns_scanner: Optional[Callable[[float], Awaitable[list[DiscoveredPrinter]]]] = None,
        subnet_scanner: Optional[Callable[..., Awaitable[list[DiscoveredPrinter]]]] = None,
        mac_lookup: Optional[Callable[[], dict[str, str]]] = None,
        prober: Optional[Prober] = None,
    ) -> None:
        self._config = config
        self._reporter = reporter
        # Resolved at scan time (not as default arguments) so tests can swap
        # the module-level scanners without touching this class.
        self._usb_scanner = usb_scanner
        self._spooler_scanner = spooler_scanner
        self._mdns_scanner = mdns_scanner
        self._subnet_scanner = subnet_scanner
        self._mac_lookup = mac_lookup
        self._prober = prober

        self._lock = asyncio.Lock()
        self._task: Optional[asyncio.Task[None]] = None
        self._wakeup = asyncio.Event()
        self._requested_subnet_scan = False
        self._requested_reprobe = False
        # fingerprint -> verdict of the network probe (see module docstring)
        self._classifications: dict[str, Classification] = {}
        self._last_result: list[DiscoveredPrinter] = []
        self._last_scan_at: Optional[float] = None
        self._last_error: Optional[str] = None

    # -- capabilities --------------------------------------------------------

    def capabilities(self) -> dict[str, bool]:
        """Heartbeat ``capabilities`` block (spec section 2)."""
        return {
            "usb": usb.usb_available(),
            "mdns": mdns.mdns_available(),
            "portscan": True,
            "spooler": spooler.spooler_available(),
        }

    # -- status for the local page -----------------------------------------

    @property
    def last_result(self) -> list[DiscoveredPrinter]:
        return list(self._last_result)

    @property
    def last_scan_at(self) -> Optional[float]:
        return self._last_scan_at

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def get_status_dict(self) -> dict[str, Any]:
        return {
            "lastScanAt": self._last_scan_at,
            "lastError": self._last_error,
            "capabilities": self.capabilities(),
            "printers": [{**p.to_payload(), "details": p.local_details()} for p in self._last_result],
        }

    # -- scanning ------------------------------------------------------------

    async def scan(self, subnet_scan: bool = False, reprobe: bool = False) -> list[DiscoveredPrinter]:
        """Run all scanners once and return the merged list (no reporting).

        ``reprobe`` drops the cached network verdicts first, so every
        candidate is asked again.
        """
        loop = asyncio.get_running_loop()
        async with self._lock:
            if reprobe:
                self._classifications.clear()
            started = time.time()
            results: list[DiscoveredPrinter] = []

            usb_task = loop.run_in_executor(None, self._usb_scanner or usb.scan_usb)
            spooler_task = loop.run_in_executor(None, self._spooler_scanner or spooler.scan_spooler)
            mdns_task = asyncio.ensure_future((self._mdns_scanner or mdns.scan_mdns)(self._config.mdns_timeout_s))
            subnet_task: Optional[asyncio.Future[list[DiscoveredPrinter]]] = None
            if subnet_scan:
                subnet_task = asyncio.ensure_future(
                    (self._subnet_scanner or portscan.scan_subnet)(
                        portscan.resolve_subnet(self._config.subnet),
                        timeout_ms=self._config.portscan_timeout_ms,
                        concurrency=self._config.portscan_concurrency,
                    )
                )

            for name, awaitable in (
                ("usb", usb_task),
                ("spooler", spooler_task),
                ("mdns", mdns_task),
                ("portscan", subnet_task),
            ):
                if awaitable is None:
                    continue
                try:
                    results.extend(await awaitable)
                except Exception as e:
                    # One broken scanner must not hide the others' results.
                    logger.warning(f"Discovery scanner {name} failed: {e}")
                    self._last_error = f"{name}: {e}"

            try:
                macs = await loop.run_in_executor(None, self._mac_lookup or neighbours.lookup_macs)
            except Exception as e:
                logger.debug(f"Neighbour lookup failed: {e}")
                macs = {}

            merged = merge_results(results, macs)
            await self._classify_network(merged)
            self._last_result = merged
            self._last_scan_at = time.time()
            logger.info(f"Discovery finished in {time.time() - started:.1f}s: {len(merged)} printer(s)")
            return merged

    async def _classify_network(self, printers: list[DiscoveredPrinter]) -> None:
        """Attach ``escpos``/``kind``/``kindReason`` to every network
        candidate: cached verdict, string-based verdict, or one ESC/POS
        model query per device (bounded parallelism)."""
        semaphore = asyncio.Semaphore(max(1, self._config.probe_concurrency))
        prober = self._prober or portscan.probe_printer

        async def classify(printer: DiscoveredPrinter) -> None:
            cached = self._classifications.get(printer.fingerprint)
            if cached is None:
                cached = classify_network_without_probe(printer)
            if cached is None:
                async with semaphore:
                    try:
                        result = await prober(printer, self._config.probe_timeout_s)
                    except Exception as e:
                        logger.debug(f"ESC/POS probe of {printer.details.get('ipAddress')} failed: {e}")
                        result = portscan.ProbeResult(connected=False)
                cached = classify_network_probe(printer, result.connected, result.maker, result.model, result.firmware)
                ip = printer.details.get("ipAddress")
                logger.info(f"Probed {ip}: escpos={cached.escpos} kind={cached.kind} ({cached.reason})")
            self._classifications[printer.fingerprint] = cached
            cached.apply(printer)

        await asyncio.gather(*(classify(p) for p in printers if p.connection_type == "network"))

    async def scan_and_report(self, subnet_scan: bool = False, reprobe: bool = False) -> list[DiscoveredPrinter]:
        """Full scan followed by a ``full: true`` report to the backend."""
        printers = await self.scan(subnet_scan=subnet_scan, reprobe=reprobe)
        if self._reporter is not None:
            try:
                await self._reporter([p.to_payload() for p in printers], True)
                self._last_error = None
            except Exception as e:
                logger.warning(f"Reporting discovered printers failed: {e}")
                self._last_error = f"report: {e}"
        return printers

    # -- scheduling ----------------------------------------------------------

    def start(self) -> None:
        """Start the periodic loop (first scan right away)."""
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="discovery")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def trigger(self, subnet_scan: bool = False, reprobe: bool = True) -> None:
        """Request an immediate scan (``printerDiscover`` socket event).

        An explicit request from the web app re-asks every network device
        (``reprobe``); the scheduled runs reuse the cached verdicts.
        """
        self._requested_subnet_scan = self._requested_subnet_scan or subnet_scan
        self._requested_reprobe = self._requested_reprobe or reprobe
        self._wakeup.set()

    async def _loop(self) -> None:
        while True:
            subnet = self._requested_subnet_scan or self._config.subnet_scan_on_schedule
            reprobe = self._requested_reprobe
            self._requested_subnet_scan = False
            self._requested_reprobe = False
            self._wakeup.clear()
            try:
                await self.scan_and_report(subnet_scan=subnet, reprobe=reprobe)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Discovery run failed: {e}")
                self._last_error = str(e)
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=max(5, self._config.interval_s))
            except asyncio.TimeoutError:
                pass


def merge_results(results: list[DiscoveredPrinter], macs: dict[str, str]) -> list[DiscoveredPrinter]:
    """Deduplicate by fingerprint and upgrade network fingerprints to MACs.

    mDNS and the port scan both key network printers by IP at first; once
    the neighbour table knows the MAC both sightings collapse into one
    record with ``net:<mac>`` and the IP kept in details.
    """
    merged: dict[str, DiscoveredPrinter] = {}
    for printer in results:
        if printer.connection_type == "network":
            ip = printer.details.get("ipAddress")
            mac = macs.get(ip) if ip else None
            if mac:
                printer.details["macAddress"] = mac
            printer.fingerprint = network_fingerprint(mac, ip or printer.fingerprint.removeprefix("net:"))
        existing = merged.get(printer.fingerprint)
        if existing is None:
            merged[printer.fingerprint] = printer
        else:
            existing.merge(printer)
            # mDNS knows the model, the port scan only the IP: keep the
            # richer origin when both saw the same device.
            if existing.discovered_via == "portscan" and printer.discovered_via == "mdns":
                existing.discovered_via = "mdns"
    return list(merged.values())
