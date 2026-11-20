"""Controllable raw-TCP receipt printer for the reliability tests.

Runs in its own thread (the transport under test is blocking socket code
that the agent runs in an executor). Modes can be switched while running:

* ``answer_status``: reply to DLE EOT 2 / DLE EOT 4 (False = cheap clone or
  a printer that stopped answering)
* ``offline_cause`` / ``paper_sensor``: the status bytes it replies with
* ``idle_close_s``: close connections that were idle this long (like real
  printers do, and like a connection does across laptop sleep)
* ``accept``: False = refuse new connections (printer off)
"""

import socket
import threading
import time
from typing import Optional

DLE_EOT = b"\x10\x04"
STATUS_OK = 0x12


class FakeTcpPrinter:
    def __init__(self) -> None:
        self.answer_status = True
        self.offline_cause = STATUS_OK
        self.paper_sensor = STATUS_OK
        self.idle_close_s: Optional[float] = None
        self.received: list[bytes] = []  # ticket bytes per connection (queries removed)
        self.connections = 0
        self.open_connections = 0
        self._lock = threading.Lock()
        self._server: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.port = 0

    # -- lifecycle -------------------------------------------------------------

    def start(self, port: int = 0) -> "FakeTcpPrinter":
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(8)
        srv.settimeout(0.05)
        self._server = srv
        self.port = srv.getsockname()[1]
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Printer switched off: listening socket closed -> connection refused."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._server:
            self._server.close()
            self._server = None

    def restart(self) -> None:
        self.start(self.port)

    @property
    def tickets(self) -> list[bytes]:
        with self._lock:
            return [r for r in self.received if r]

    # -- serving ---------------------------------------------------------------

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                self.connections += 1
                self.open_connections += 1
                self.received.append(b"")
                index = len(self.received) - 1
            threading.Thread(target=self._handle, args=(conn, index), daemon=True).start()

    def _handle(self, conn: socket.socket, index: int) -> None:
        conn.settimeout(0.05)
        last = time.monotonic()
        try:
            while not self._stop.is_set():
                if self.idle_close_s is not None and time.monotonic() - last > self.idle_close_s:
                    return
                try:
                    chunk = conn.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    return
                if not chunk:
                    return
                last = time.monotonic()
                data, replies = self._split(chunk)
                with self._lock:
                    self.received[index] += data
                if replies and self.answer_status:
                    conn.sendall(replies)
        finally:
            with self._lock:
                self.open_connections -= 1
            try:
                conn.close()
            except OSError:
                pass

    def _split(self, chunk: bytes) -> tuple[bytes, bytes]:
        """Remove DLE EOT n queries from the stream, build their replies."""
        data = bytearray()
        replies = bytearray()
        i = 0
        while i < len(chunk):
            if chunk[i : i + 2] == DLE_EOT and i + 2 < len(chunk):
                n = chunk[i + 2]
                replies.append(self.offline_cause if n == 2 else self.paper_sensor if n == 4 else STATUS_OK)
                i += 3
                continue
            data.append(chunk[i])
            i += 1
        return bytes(data), bytes(replies)
