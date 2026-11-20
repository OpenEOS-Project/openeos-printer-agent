"""Raw TCP (port 9100) transport for network receipt printers.

Why not python-escpos' ``Network``: it keeps one socket open forever and
treats "``sendall`` returned" as "printed". A printer that closed the idle
connection (its own timeout, the laptop slept, the printer was switched off)
then swallows the first ticket into the local send buffer - reported as
printed, never printed. A printer that silently vanished (no RST) swallows
every ticket.

What this module does instead, per job:

1. open a fresh connection (short connect timeout, keepalive, and on Linux
   ``TCP_USER_TIMEOUT`` so unacknowledged data fails fast);
2. when the printer is known to answer status queries: ask first and refuse
   to send into an open cover / empty paper roll (nothing half-printed);
3. send the ticket, then the real-time status queries ``DLE EOT 2`` (offline
   cause) and ``DLE EOT 4`` (paper sensor). TCP delivers in order, so an
   answer proves the printer received the whole ticket; the bits tell
   whether it can print it;
4. close the connection - no idle sockets to go stale.

Printers that never answer status queries (some cheap clones) are detected
on first use and fall back to "fresh connection + orderly close", which
still catches dead and silently unreachable printers at connect time.
"""

import logging
import socket
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DLE_EOT_OFFLINE_CAUSE = b"\x10\x04\x02"
DLE_EOT_PAPER_SENSOR = b"\x10\x04\x04"
STATUS_QUERY = DLE_EOT_OFFLINE_CAUSE + DLE_EOT_PAPER_SENSOR

CONNECT_TIMEOUT_S = 5.0
# A ticket with a few images is some ten kilobytes; a printer that does not
# take them within this time is not going to.
IO_TIMEOUT_S = 15.0
STATUS_TIMEOUT_S = 4.0
# Without status support: wait this long for the printer to close its side
# after our FIN (most do right away once the data is in their buffer).
CLOSE_WAIT_S = 2.0


class PrinterStatusError(Exception):
    """The printer answered, but cannot print (cover open, paper out, ...)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PrinterNoConfirmation(Exception):
    """A printer that normally answers status queries did not answer after
    the ticket - we cannot tell whether it arrived."""


@dataclass
class PrinterState:
    cover_open: bool = False
    paper_out: bool = False
    paper_near_end: bool = False
    error: bool = False

    @property
    def problem(self) -> Optional[tuple[str, str]]:
        if self.cover_open:
            return "COVER_OPEN", "Printer cover is open"
        if self.paper_out:
            return "PAPER_OUT", "Printer is out of paper"
        if self.error:
            return "PRINTER_OFFLINE", "Printer reports an error (offline)"
        return None


def _is_status_byte(b: int) -> bool:
    # DLE EOT replies: bit 1 and bit 4 always set, bit 0 and bit 7 clear.
    return b & 0x93 == 0x12


def parse_status(offline_cause: int, paper_sensor: int) -> PrinterState:
    return PrinterState(
        cover_open=bool(offline_cause & 0x04),
        paper_out=bool(offline_cause & 0x20) or bool(paper_sensor & 0x60),
        paper_near_end=bool(paper_sensor & 0x0C),
        error=bool(offline_cause & 0x40),
    )


def _configure(sock: socket.socket, io_timeout: float) -> None:
    sock.settimeout(io_timeout)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except OSError:
        pass
    user_timeout = getattr(socket, "TCP_USER_TIMEOUT", None)
    if user_timeout is not None and sys.platform.startswith("linux"):
        try:
            sock.setsockopt(socket.IPPROTO_TCP, user_timeout, int(io_timeout * 1000))
        except OSError:
            pass


class NetworkTransport:
    """Sends one job per connection and confirms it with a status query."""

    def __init__(
        self,
        host: str,
        port: int = 9100,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        io_timeout: float = IO_TIMEOUT_S,
        status_timeout: float = STATUS_TIMEOUT_S,
        connect: Optional[Callable[..., socket.socket]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self._connect_timeout = connect_timeout
        self._io_timeout = io_timeout
        self._status_timeout = status_timeout
        self._connect = connect or socket.create_connection
        # None = not known yet, True = answers DLE EOT, False = never did
        self.status_supported: Optional[bool] = None
        self.last_state: Optional[PrinterState] = None

    def _open(self) -> socket.socket:
        sock = self._connect((self.host, self.port), timeout=self._connect_timeout)
        _configure(sock, self._io_timeout)
        return sock

    def _query(self, sock: socket.socket) -> Optional[PrinterState]:
        """Send both status queries and read the two answers (or None)."""
        sock.sendall(STATUS_QUERY)
        answers: list[int] = []
        deadline = time.monotonic() + self._status_timeout
        while len(answers) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(16)
            except (socket.timeout, TimeoutError):
                return None
            if not chunk:
                return None  # printer closed the connection
            answers.extend(b for b in chunk if _is_status_byte(b))
        sock.settimeout(self._io_timeout)
        state = parse_status(answers[0], answers[1])
        self.last_state = state
        return state

    def probe(self) -> Optional[PrinterState]:
        """Connect, ask, close. Raises OSError when the printer is not
        reachable; returns None for a reachable printer that does not answer
        status queries."""
        sock = self._open()
        try:
            state = self._query(sock)
        finally:
            _close(sock)
        if state is not None:
            self.status_supported = True
        return state

    def send(self, data: bytes) -> Optional[PrinterState]:
        sock = self._open()
        try:
            if self.status_supported:
                before = self._query(sock)
                if before is not None and before.problem:
                    code, message = before.problem
                    raise PrinterStatusError(code, message)
            sock.sendall(data)
            if self.status_supported is False:
                _drain_close(sock)
                return None
            after = self._query(sock)
            if after is None:
                if self.status_supported:
                    raise PrinterNoConfirmation("Printer did not confirm the ticket (no status reply)")
                logger.info(
                    f"Printer {self.host}:{self.port} does not answer status queries - "
                    "printing without confirmation from now on"
                )
                self.status_supported = False
                return None
            self.status_supported = True
            if after.problem:
                code, message = after.problem
                raise PrinterStatusError(code, message)
            if after.paper_near_end:
                logger.warning(f"Printer {self.host}:{self.port}: paper roll nearly empty")
            return after
        finally:
            _close(sock)


def _drain_close(sock: socket.socket) -> None:
    """Orderly close: FIN, then wait briefly for the printer's FIN/RST."""
    try:
        sock.shutdown(socket.SHUT_WR)
        sock.settimeout(CLOSE_WAIT_S)
        while sock.recv(64):
            pass
    except (OSError, TimeoutError):
        pass


def _close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass
