import asyncio
import logging
from enum import Enum
from typing import Optional

from escpos.printer import Network, Usb

logger = logging.getLogger(__name__)


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


class ManagedPrinter:
    """Wrapper for a single physical printer with status tracking and locking."""

    def __init__(self, config: dict) -> None:
        self.config = config
        self.printer_id: str = config["id"]
        self.name: str = config.get("name", "Unknown")
        self.connection_type: str = config.get("connectionType", "usb")
        self.paper_width: int = config.get("paperWidth", 80)
        self.connection_config: dict = config.get("connectionConfig", {})
        self.status = PrinterStatus.OFFLINE
        self.last_error: Optional[str] = None
        self.last_error_code: Optional[PrinterErrorCode] = None
        self._escpos: Optional[Usb | Network] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> bool:
        """Connect to the physical printer."""
        loop = asyncio.get_event_loop()
        try:
            self._escpos = await loop.run_in_executor(None, self._create_escpos)
            self.status = PrinterStatus.ONLINE
            self.last_error = None
            self.last_error_code = None
            logger.info(f"Printer '{self.name}' ({self.printer_id}) connected via {self.connection_type}")
            return True
        except Exception as e:
            self.status = PrinterStatus.ERROR
            self.last_error = str(e)
            self.last_error_code = self._classify_error(e)
            logger.error(f"Printer '{self.name}' ({self.printer_id}) connection failed: {e}")
            return False

    def _create_escpos(self) -> Usb | Network:
        """Create escpos printer instance (runs in executor)."""
        conn = self.connection_config

        if self.connection_type == "usb":
            vendor_id = conn.get("usbVendorId")
            product_id = conn.get("usbProductId")
            if not vendor_id or not product_id:
                raise ValueError(f"USB printer '{self.name}' requires usbVendorId and usbProductId")
            vendor = int(vendor_id, 16) if isinstance(vendor_id, str) else vendor_id
            product = int(product_id, 16) if isinstance(product_id, str) else product_id
            return Usb(vendor, product)

        elif self.connection_type == "network":
            ip_address = conn.get("ipAddress")
            if not ip_address:
                raise ValueError(f"Network printer '{self.name}' requires ipAddress in connectionConfig")
            port = conn.get("port", 9100)
            return Network(ip_address, port=port)

        else:
            raise ValueError(f"Unsupported connection type: {self.connection_type}")

    async def execute(self, callback) -> None:
        """Execute a print operation with lock to prevent concurrent access."""
        async with self._lock:
            if self._escpos is None:
                raise RuntimeError(f"Printer '{self.name}' is not connected")

            self.status = PrinterStatus.BUSY

            loop = asyncio.get_event_loop()
            try:
                await loop.run_in_executor(None, callback, self._escpos)
                self.status = PrinterStatus.ONLINE
                self.last_error = None
                self.last_error_code = None
            except Exception as e:
                self.status = PrinterStatus.ERROR
                self.last_error = str(e)
                self.last_error_code = self._classify_error(e)
                raise

    async def disconnect(self) -> None:
        """Disconnect from printer."""
        if self._escpos is not None:
            loop = asyncio.get_event_loop()
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
        msg = str(error).lower()
        if "paper" in msg or "paper end" in msg:
            return PrinterErrorCode.PAPER_OUT
        if "cover" in msg or "lid" in msg:
            return PrinterErrorCode.COVER_OPEN
        if "offline" in msg:
            return PrinterErrorCode.PRINTER_OFFLINE
        if "usb" in msg or "device not found" in msg:
            return PrinterErrorCode.USB_ERROR
        if "connect" in msg or "timeout" in msg or "refused" in msg:
            return PrinterErrorCode.CONNECTION_FAILED
        return PrinterErrorCode.UNKNOWN

    def get_status_dict(self) -> dict:
        return {
            "printerId": self.printer_id,
            "name": self.name,
            "status": self.status.value,
            "connectionType": self.connection_type,
            "paperWidth": self.paper_width,
            "lastError": self.last_error,
            "lastErrorCode": self.last_error_code.value if self.last_error_code else None,
        }


class PrinterManager:
    """Manages all configured printers."""

    def __init__(self) -> None:
        self._printers: dict[str, ManagedPrinter] = {}

    async def initialize(self, printer_configs: list[dict]) -> None:
        """Initialize printers from backend config dicts and connect them."""
        for cfg in printer_configs:
            printer_id = cfg.get("id", "")
            if not printer_id:
                logger.warning(f"Skipping printer config without id: {cfg}")
                continue
            self._printers[printer_id] = ManagedPrinter(cfg)

        if not self._printers:
            logger.warning("No printers configured")
            return

        tasks = [printer.connect() for printer in self._printers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        connected = sum(1 for r in results if r is True)
        logger.info(f"Printer initialization: {connected}/{len(self._printers)} connected")

    async def reconfigure(self, printer_configs: list[dict]) -> None:
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
                    self._printers[pid] = ManagedPrinter(cfg)
                    await self._printers[pid].connect()
                    logger.info(f"Reconfigured printer '{cfg.get('name')}' ({pid})")
            else:
                # New printer
                self._printers[pid] = ManagedPrinter(cfg)
                await self._printers[pid].connect()
                logger.info(f"Added printer '{cfg.get('name')}' ({pid})")

    @staticmethod
    def _config_changed(existing: ManagedPrinter, new_cfg: dict) -> bool:
        """Check if relevant config fields have changed."""
        if existing.name != new_cfg.get("name", ""):
            return True
        if existing.connection_type != new_cfg.get("connectionType", ""):
            return True
        if existing.paper_width != new_cfg.get("paperWidth", 80):
            return True
        if existing.connection_config != new_cfg.get("connectionConfig", {}):
            return True
        return False

    def get_printer(self, printer_id: str) -> Optional[ManagedPrinter]:
        return self._printers.get(printer_id)

    def get_all_printers(self) -> list[ManagedPrinter]:
        return list(self._printers.values())

    def get_all_statuses(self) -> list[dict]:
        return [p.get_status_dict() for p in self._printers.values()]

    async def health_check_all(self) -> None:
        """Check all printers and attempt reconnect for offline ones."""
        for printer in self._printers.values():
            if printer.status in (PrinterStatus.OFFLINE, PrinterStatus.ERROR):
                logger.info(f"Attempting reconnect for printer '{printer.name}'")
                await printer.reconnect()

    async def shutdown(self) -> None:
        """Disconnect all printers."""
        for printer in self._printers.values():
            await printer.disconnect()
        logger.info("All printers disconnected")
