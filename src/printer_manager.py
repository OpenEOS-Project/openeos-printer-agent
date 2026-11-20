import asyncio
import logging
import socket
from enum import Enum
from typing import Any, Callable, Optional, Union

from escpos.printer import Dummy, Usb

from .escpos_text import escpos_to_text
from .net_printer import NetworkTransport, PrinterNoConfirmation, PrinterStatusError
from .win_spooler import SpoolerTransport

logger = logging.getLogger(__name__)

try:
    import win32print  # type: ignore[import-not-found,unused-ignore]
except Exception:  # pragma: no cover - pywin32 missing outside Windows
    win32print = None

EscposPrinter = Any


class PrinterStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"
    BUSY = "busy"


class PrinterErrorCode(str, Enum):
    PAPER_OUT = "PAPER_OUT"
    COVER_OPEN = "COVER_OPEN"
    PRINTER_OFFLINE = "PRINTER_OFFLINE"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    USB_ERROR = "USB_ERROR"
    UNKNOWN = "UNKNOWN"


def _parse_usb_id(value: Any) -> int:
    """Accept ``"04b8"``, ``"0x04b8"`` or an int (the backend stores what the
    discovery reported: 4-digit hex without prefix)."""
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    return int(text, 16)


def describe_error(e: BaseException) -> str:
    """Never an empty message (escpos raises bare asserts/OSErrors)."""
    text = str(e).strip()
    if isinstance(e, (socket.timeout, TimeoutError)) and not text:
        return "Timeout while talking to the printer"
    if not text:
        return type(e).__name__
    if isinstance(e, OSError) and type(e).__name__ not in text:
        return f"{type(e).__name__}: {text}"
    return text


class ManagedPrinter:
    """Wrapper for a single physical printer with status tracking and locking."""

    def __init__(self, config: dict[str, Any], dummy: bool = False) -> None:
        self.config = config
        self.printer_id: str = config["id"]
        self.name: str = config.get("name", "Unknown")
        self.fingerprint: Optional[str] = config.get("fingerprint")
        self.connection_type: str = config.get("connectionType", "usb")
        self.paper_width: int = config.get("paperWidth", 80)
        # Characters per line when the printer differs from the paper default
        # (42 on 80 mm, 32 on 58 mm), e.g. 48 on 80 mm printers with 576 dots.
        self.columns: Optional[int] = config.get("columns") or None
        self.connection_config: dict[str, Any] = config.get("connectionConfig") or {}
        self.status = PrinterStatus.OFFLINE
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[PrinterErrorCode] = None
        self._dummy = dummy
        self._escpos: Optional[EscposPrinter] = None
        # Network printers: the escpos object only buffers (Dummy); the bytes
        # go out over a fresh, confirmed connection per job (net_printer.py).
        self._transport: Optional[Union[NetworkTransport, SpoolerTransport]] = None
        self._lock = asyncio.Lock()

    @property
    def uses_transport(self) -> bool:
        return self._transport is not None

    async def connect(self) -> bool:
        """Set up the backend and find out whether the printer is reachable.

        Network printers are actually contacted (TCP connect + status query),
        so "online" means online. They stay usable even when the probe fails:
        every job opens its own connection, a printer that comes back later
        just works.
        """
        loop = asyncio.get_running_loop()
        try:
            self._escpos = await loop.run_in_executor(None, self._create_escpos)
        except Exception as e:
            self._set_error(e)
            logger.error(f"Printer '{self.name}' ({self.printer_id}) connection failed: {self.last_error}")
            return False
        if self._transport is not None:
            return await self.probe(log=True)
        self._set_online()
        backend = "dummy" if self._dummy else self.connection_type
        logger.info(f"Printer '{self.name}' ({self.printer_id}) connected via {backend}")
        return True

    async def probe(self, log: bool = False) -> bool:
        """Network printers: open a connection and ask for the status.
        Skipped while a job holds the printer (that job tells us anyway)."""
        transport = self._transport
        if transport is None:
            return self.is_online
        if self._lock.locked():
            return self.is_online
        loop = asyncio.get_running_loop()
        async with self._lock:
            was = self.status
            try:
                state = await loop.run_in_executor(None, transport.probe)
            except Exception as e:
                self._set_error(e)
                if log or was != self.status:
                    logger.warning(f"Printer '{self.name}' ({self.printer_id}) not reachable: {self.last_error}")
                return False
            problem = state.problem if state is not None else None
            if problem:
                self.status = PrinterStatus.ERROR
                self.last_error_code = PrinterErrorCode(problem[0])
                self.last_error = problem[1]
                if log or was != self.status:
                    logger.warning(f"Printer '{self.name}' ({self.printer_id}): {problem[1]}")
                return False
            self._set_online()
            if log or was != self.status:
                how = "status confirmed" if state is not None else "reachable, no status support"
                logger.info(f"Printer '{self.name}' ({self.printer_id}) online via {self.connection_type} ({how})")
            return True

    def _set_online(self) -> None:
        self.status = PrinterStatus.ONLINE
        self.last_error = None
        self.last_error_code = None

    def _set_error(self, e: Exception) -> None:
        self.last_error = describe_error(e)
        self.last_error_code = self._classify_error(e)
        connection_lost = self.last_error_code in (PrinterErrorCode.CONNECTION_FAILED, PrinterErrorCode.PRINTER_OFFLINE)
        self.status = PrinterStatus.OFFLINE if connection_lost else PrinterStatus.ERROR

    def _create_escpos(self) -> EscposPrinter:
        """Create escpos printer instance (runs in executor)."""
        if self._dummy:
            return Dummy()

        conn = self.connection_config

        if self.connection_type == "usb":
            vendor_id = conn.get("usbVendorId")
            product_id = conn.get("usbProductId")
            if not vendor_id or not product_id:
                raise ValueError(f"USB printer '{self.name}' requires usbVendorId and usbProductId")
            vendor = _parse_usb_id(vendor_id)
            product = _parse_usb_id(product_id)
            serial = conn.get("usbSerialNumber")
            if serial:
                # Two identical printers on one box are told apart by serial.
                # Some firmwares refuse to expose it once opened by another
                # driver - fall back to the plain VID/PID match in that case.
                try:
                    return Usb(vendor, product, usb_args={"serial_number": str(serial)})
                except Exception as e:
                    logger.warning(f"USB printer '{self.name}': serial match failed ({e}), retrying without serial")
            return Usb(vendor, product)

        if self.connection_type == "network":
            ip_address = conn.get("ipAddress")
            if not ip_address:
                raise ValueError(f"Network printer '{self.name}' requires ipAddress in connectionConfig")
            port = int(conn.get("port") or 9100)
            self._transport = NetworkTransport(str(ip_address), port=port)
            # Same profile/encoding as escpos.Network; only collects bytes.
            return Dummy()

        if self.connection_type == "windows":
            printer_name = conn.get("windowsPrinterName")
            if not printer_name:
                raise ValueError(f"Windows printer '{self.name}' requires windowsPrinterName in connectionConfig")
            if win32print is None:
                raise RuntimeError("Windows spooler printing requires pywin32 (connection type 'windows')")
            # One spool document per job, watched until Windows printed it
            # (escpos' Win32Raw keeps a single document open forever).
            self._transport = SpoolerTransport(str(printer_name), win32print=win32print)
            return Dummy()

        raise ValueError(f"Unsupported connection type: {self.connection_type}")

    async def execute(self, callback: Callable[[EscposPrinter], None]) -> None:
        """Execute a print operation with lock to prevent concurrent access."""
        async with self._lock:
            if self._escpos is None:
                raise RuntimeError(f"Printer '{self.name}' is not connected")

            # Busy only counts as online when the printer was online before;
            # a dead printer stays offline while we try to reach it.
            if self.status == PrinterStatus.ONLINE:
                self.status = PrinterStatus.BUSY

            loop = asyncio.get_running_loop()
            try:
                if self._transport is not None:
                    await loop.run_in_executor(None, self._render_and_send, callback)
                else:
                    await loop.run_in_executor(None, callback, self._escpos)
                self._set_online()
            except Exception as e:
                self._set_error(e)
                raise
            finally:
                if self._dummy:
                    self._flush_dummy_output()

    def _render_and_send(self, callback: Callable[[EscposPrinter], None]) -> None:
        """Network: render into the buffer, then one confirmed send."""
        buffer = self._escpos
        transport = self._transport
        assert isinstance(buffer, Dummy) and transport is not None
        buffer.clear()
        try:
            callback(buffer)
            data = bytes(buffer.output)
        finally:
            buffer.clear()
        if data:
            transport.send(data)

    def _flush_dummy_output(self) -> None:
        """``--dummy-printer``: log the ESC/POS stream as text and reset the buffer."""
        dummy = self._escpos
        if not isinstance(dummy, Dummy):
            return
        output = bytes(dummy.output)
        dummy.clear()
        if not output:
            return
        text = escpos_to_text(output).rstrip()
        logger.info(f"[dummy printer '{self.name}'] {len(output)} bytes:\n{text}")

    async def disconnect(self) -> None:
        """Disconnect from printer."""
        if self._escpos is not None:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(None, self._escpos.close)
            except Exception as e:
                logger.debug(f"Error closing printer '{self.name}' ({self.printer_id}): {e}")
            self._escpos = None
        self.status = PrinterStatus.OFFLINE

    async def reconnect(self) -> bool:
        """Reconnect to printer."""
        await self.disconnect()
        return await self.connect()

    def _classify_error(self, error: Exception) -> PrinterErrorCode:
        """Classify exception into a known error code."""
        if isinstance(error, PrinterStatusError):
            try:
                return PrinterErrorCode(error.code)
            except ValueError:
                return PrinterErrorCode.UNKNOWN
        if isinstance(error, PrinterNoConfirmation):
            return PrinterErrorCode.PRINTER_OFFLINE
        if isinstance(error, (ConnectionError, TimeoutError, socket.timeout, socket.gaierror)):
            return PrinterErrorCode.CONNECTION_FAILED
        msg = str(error).lower()
        if "paper" in msg or "paper end" in msg:
            return PrinterErrorCode.PAPER_OUT
        if "cover" in msg or "lid" in msg:
            return PrinterErrorCode.COVER_OPEN
        if "offline" in msg:
            return PrinterErrorCode.PRINTER_OFFLINE
        if "usb" in msg or "device not found" in msg:
            return PrinterErrorCode.USB_ERROR
        if "connect" in msg or "timeout" in msg or "refused" in msg or "unreachable" in msg:
            return PrinterErrorCode.CONNECTION_FAILED
        if isinstance(error, OSError) and self._transport is not None:
            return PrinterErrorCode.CONNECTION_FAILED
        return PrinterErrorCode.UNKNOWN

    @property
    def is_online(self) -> bool:
        return self.status in (PrinterStatus.ONLINE, PrinterStatus.BUSY)

    def get_status_dict(self) -> dict[str, Any]:
        return {
            "printerId": self.printer_id,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "status": self.status.value,
            "connectionType": self.connection_type,
            "paperWidth": self.paper_width,
            "lastError": self.last_error,
            "lastErrorCode": self.last_error_code.value if self.last_error_code else None,
        }

    def get_heartbeat_dict(self) -> dict[str, Any]:
        """Entry for ``agentHeartbeat.printers`` (spec section 4). ``printerId``
        is included on top of the spec so manually created printers - which
        have no fingerprint - can still be marked online by the backend."""
        entry: dict[str, Any] = {
            "fingerprint": self.fingerprint,
            "printerId": self.printer_id,
            "online": self.is_online,
        }
        if self.last_error:
            entry["lastError"] = self.last_error
        return entry


class PrinterManager:
    """Manages all adopted printers (``GET /device-api/printers``)."""

    def __init__(self, dummy: bool = False) -> None:
        self._printers: dict[str, ManagedPrinter] = {}
        self._dummy = dummy
        if dummy:
            logger.warning("Dummy printer mode: nothing is sent to hardware, output goes to the log")

    async def initialize(self, printer_configs: list[dict[str, Any]]) -> None:
        """Initialize printers from backend config dicts and connect them."""
        for cfg in printer_configs:
            printer_id = cfg.get("id", "")
            if not printer_id:
                logger.warning(f"Skipping printer config without id: {cfg}")
                continue
            self._printers[printer_id] = ManagedPrinter(cfg, dummy=self._dummy)

        if not self._printers:
            logger.warning("No printers adopted yet - adopt discovered printers in the web app")
            return

        tasks = [printer.connect() for printer in self._printers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        connected = sum(1 for r in results if r is True)
        logger.info(f"Printer initialization: {connected}/{len(self._printers)} connected")

    async def reconfigure(self, printer_configs: list[dict[str, Any]]) -> None:
        """Update printer configuration based on backend data.

        Compares current printers with new config and:
        - Adds new printers
        - Removes printers no longer in config
        - Updates changed printers (disconnect old, connect new)
        """
        new_ids = {cfg["id"] for cfg in printer_configs if cfg.get("id")}
        current_ids = set(self._printers.keys())

        # Remove printers no longer in config
        removed_ids = current_ids - new_ids
        for pid in removed_ids:
            printer = self._printers.pop(pid)
            await printer.disconnect()
            logger.info(f"Removed printer '{printer.name}' ({pid})")

        # Add new or update existing printers
        for cfg in printer_configs:
            pid = cfg.get("id", "")
            if not pid:
                continue

            if pid in self._printers:
                existing = self._printers[pid]
                # Check if config actually changed
                if self._config_changed(existing, cfg):
                    await existing.disconnect()
                    self._printers[pid] = ManagedPrinter(cfg, dummy=self._dummy)
                    await self._printers[pid].connect()
                    logger.info(f"Reconfigured printer '{cfg.get('name')}' ({pid})")
            else:
                # New printer
                self._printers[pid] = ManagedPrinter(cfg, dummy=self._dummy)
                await self._printers[pid].connect()
                logger.info(f"Added printer '{cfg.get('name')}' ({pid})")

    @staticmethod
    def _config_changed(existing: ManagedPrinter, new_cfg: dict[str, Any]) -> bool:
        """Check if relevant config fields have changed."""
        if existing.name != new_cfg.get("name", ""):
            return True
        if existing.connection_type != new_cfg.get("connectionType", ""):
            return True
        if existing.paper_width != new_cfg.get("paperWidth", 80):
            return True
        if existing.columns != (new_cfg.get("columns") or None):
            return True
        if existing.connection_config != (new_cfg.get("connectionConfig") or {}):
            return True
        if existing.fingerprint != new_cfg.get("fingerprint"):
            return True
        return False

    def get_printer(self, printer_id: str) -> Optional[ManagedPrinter]:
        return self._printers.get(printer_id)

    def get_all_printers(self) -> list[ManagedPrinter]:
        return list(self._printers.values())

    def get_all_statuses(self) -> list[dict[str, Any]]:
        return [p.get_status_dict() for p in self._printers.values()]

    def get_heartbeat_entries(self) -> list[dict[str, Any]]:
        return [p.get_heartbeat_dict() for p in self._printers.values()]

    async def health_check_all(self) -> None:
        """Network printers: real probe every round, so the online state the
        web app shows is true. Others: reconnect when offline."""
        checks = []
        for printer in self._printers.values():
            if printer.uses_transport:
                checks.append(printer.probe())
            elif printer.status in (PrinterStatus.OFFLINE, PrinterStatus.ERROR):
                logger.info(f"Attempting reconnect for printer '{printer.name}'")
                checks.append(printer.reconnect())
        if checks:
            await asyncio.gather(*checks, return_exceptions=True)

    async def shutdown(self) -> None:
        """Disconnect all printers."""
        for printer in self._printers.values():
            await printer.disconnect()
        logger.info("All printers disconnected")
