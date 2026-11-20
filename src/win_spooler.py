"""Windows print spooler transport ("Generic / Text Only" and vendor drivers).

python-escpos' ``Win32Raw`` starts ONE spool document when it is opened and
ends it only in ``close()``. The agent kept the printer open for good, so
every ticket was appended to a document that never finished: reported as
printed, while Windows printed nothing until the agent stopped.

Per job this transport instead

1. opens the printer, writes the ticket as its own RAW document
   (StartDoc/StartPage/Write/EndPage/EndDoc) and closes the handle;
2. watches that job in the queue until Windows has printed it (the job
   is gone or marked printed/complete). Error, offline, paper out or
   "needs attention" fail the job; so does a job still waiting after
   ``timeout_s``. A failed job is removed from the queue first, so the
   retry cannot print it a second time later.

``probe()`` reads the printer's queue status (offline, error, paper out,
paused) without printing anything.
"""

import time
from typing import Any, Callable, Optional

from .net_printer import PrinterNoConfirmation, PrinterState, PrinterStatusError

# JOB_STATUS_* (winspool.h)
JOB_STATUS_PAUSED = 0x0001
JOB_STATUS_ERROR = 0x0002
JOB_STATUS_DELETING = 0x0004
JOB_STATUS_OFFLINE = 0x0020
JOB_STATUS_PAPEROUT = 0x0040
JOB_STATUS_PRINTED = 0x0080
JOB_STATUS_DELETED = 0x0100
JOB_STATUS_BLOCKED_DEVQ = 0x0200
JOB_STATUS_USER_INTERVENTION = 0x0400
JOB_STATUS_COMPLETE = 0x1000
JOB_CONTROL_DELETE = 5
JOB_FAILED = JOB_STATUS_OFFLINE | JOB_STATUS_ERROR | JOB_STATUS_USER_INTERVENTION | JOB_STATUS_BLOCKED_DEVQ

# PRINTER_STATUS_* / PRINTER_ATTRIBUTE_WORK_OFFLINE
PRINTER_STATUS_PAUSED = 0x00000001
PRINTER_STATUS_ERROR = 0x00000002
PRINTER_STATUS_PAPER_JAM = 0x00000008
PRINTER_STATUS_PAPER_OUT = 0x00000010
PRINTER_STATUS_OFFLINE = 0x00000080
PRINTER_STATUS_DOOR_OPEN = 0x00400000
PRINTER_STATUS_NOT_AVAILABLE = 0x00001000
PRINTER_ATTRIBUTE_WORK_OFFLINE = 0x00000400

JOB_TIMEOUT_S = 30.0
ERROR_INVALID_PARAMETER = 87  # GetJob for a job that is no longer queued


def _winerror(e: BaseException) -> Optional[int]:
    code = getattr(e, "winerror", None)
    if code is None and e.args and isinstance(e.args[0], int):
        code = e.args[0]
    return code


class SpoolerTransport:
    def __init__(
        self,
        printer_name: str,
        win32print: Any = None,
        timeout_s: float = JOB_TIMEOUT_S,
        poll_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if win32print is None:
            import win32print as _win32print  # type: ignore[import-not-found,unused-ignore]

            win32print = _win32print
        self._wp = win32print
        self.name = printer_name
        self._timeout = timeout_s
        self._poll = poll_s
        self._sleep = sleep
        # same attribute as NetworkTransport; the spooler always reports
        self.status_supported: Optional[bool] = True

    def probe(self) -> Optional[PrinterState]:
        handle = self._wp.OpenPrinter(self.name)
        try:
            info = self._wp.GetPrinter(handle, 2)
        finally:
            self._wp.ClosePrinter(handle)
        status = int(info.get("Status", 0))
        attributes = int(info.get("Attributes", 0))
        return PrinterState(
            cover_open=bool(status & PRINTER_STATUS_DOOR_OPEN),
            paper_out=bool(status & (PRINTER_STATUS_PAPER_OUT | PRINTER_STATUS_PAPER_JAM)),
            error=bool(
                status & (PRINTER_STATUS_ERROR | PRINTER_STATUS_OFFLINE | PRINTER_STATUS_NOT_AVAILABLE)
                or status & PRINTER_STATUS_PAUSED
                or attributes & PRINTER_ATTRIBUTE_WORK_OFFLINE
            ),
        )

    def send(self, data: bytes) -> Optional[PrinterState]:
        handle = self._wp.OpenPrinter(self.name)
        try:
            job_id = self._wp.StartDocPrinter(handle, 1, ("OpenEOS", None, "RAW"))
            try:
                self._wp.StartPagePrinter(handle)
                self._wp.WritePrinter(handle, data)
                self._wp.EndPagePrinter(handle)
            finally:
                self._wp.EndDocPrinter(handle)
            self._wait_printed(handle, job_id)
        finally:
            self._wp.ClosePrinter(handle)
        return None

    def _wait_printed(self, handle: Any, job_id: int) -> None:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                info = self._wp.GetJob(handle, job_id, 1)
            except Exception as e:
                if _winerror(e) == ERROR_INVALID_PARAMETER:
                    return  # job id unknown: printed and removed from the queue
                # Spooler briefly unavailable (restart, RPC hiccup): this says
                # nothing about the job - keep watching until the deadline.
                if time.monotonic() >= deadline:
                    raise PrinterNoConfirmation(f"Could not check the print job on '{self.name}': {e}") from e
                self._sleep(self._poll)
                continue
            status = int(info.get("Status", 0))
            if status & (JOB_STATUS_PRINTED | JOB_STATUS_COMPLETE):
                return
            if status & JOB_STATUS_PAPEROUT:
                self._cancel(handle, job_id)
                raise PrinterStatusError("PAPER_OUT", f"Windows printer '{self.name}' is out of paper")
            if status & JOB_FAILED:
                self._cancel(handle, job_id)
                message = f"Windows reports printer '{self.name}' offline or in error (job status {status:#x})"
                raise PrinterStatusError("PRINTER_OFFLINE", message)
            if status & (JOB_STATUS_DELETING | JOB_STATUS_DELETED):
                raise PrinterNoConfirmation(f"Print job on '{self.name}' was deleted before it printed")
            if time.monotonic() >= deadline:
                self._cancel(handle, job_id)
                paused = " (printer queue paused?)" if status & JOB_STATUS_PAUSED else ""
                raise PrinterNoConfirmation(
                    f"Windows did not print the job on '{self.name}' within {self._timeout:.0f}s{paused}"
                )
            self._sleep(self._poll)

    def _cancel(self, handle: Any, job_id: int) -> None:
        try:
            self._wp.SetJob(handle, job_id, 0, None, JOB_CONTROL_DELETE)
        except Exception:
            pass
