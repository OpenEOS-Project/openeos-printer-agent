#!/usr/bin/env python3
"""Network receipt-printer emulator for tests without hardware.

Listens on TCP 9100 (the raw/JetDirect port), accepts whatever the agent
sends, answers ESC/POS ``GS I`` model queries so discovery can name it, and
logs every received job as readable text. Standalone: only the standard
library, so it runs on any machine with Python 3.8+.

    python tools/fake_printer.py                       # 0.0.0.0:9100
    python tools/fake_printer.py --port 9101 --log-file /tmp/fake-printer.log
    python tools/fake_printer.py --model "TM-T88V" --maker "EPSON"

A job is logged when the stream contains a cut (``GS V``) or when the
connection goes idle for --flush-after seconds / closes. Exit with Ctrl+C.
"""

import argparse
import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Optional

ESC = 0x1B
GS = 0x1D
FS = 0x1C
DLE = 0x10
CUT_MARKER = "--- CUT ---"

_ESC_LEN = {
    ord("@"): 0, ord("!"): 1, ord("a"): 1, ord("E"): 1, ord("-"): 1, ord("d"): 1,
    ord("J"): 1, ord("p"): 3, ord("t"): 1, ord("M"): 1, ord("{"): 1, ord("G"): 1,
    ord("R"): 1, ord("2"): 0, ord("3"): 1, ord("c"): 2, ord("i"): 0, ord("m"): 0,
    ord("="): 1, ord("S"): 0, ord("T"): 0, ord("L"): 0, ord("W"): 8, ord("$"): 2,
    ord("\\"): 2, ord("e"): 1, ord("f"): 2, ord("s"): 1, ord("r"): 1, ord("u"): 1, ord("v"): 0,
}  # fmt: skip
_GS_LEN = {
    ord("!"): 1, ord("B"): 1, ord("a"): 1, ord("L"): 2, ord("W"): 2, ord("h"): 1,
    ord("w"): 1, ord("H"): 1, ord("f"): 1, ord("I"): 1, ord("r"): 1, ord("E"): 1,
    ord("P"): 2, ord("b"): 1, ord("$"): 2, ord("\\"): 2, ord("^"): 3, ord(":"): 0,
    ord("T"): 1, ord("V"): 1, ord("g"): 3,
}  # fmt: skip
_FS_LEN = {ord("&"): 0, ord("."): 0, ord("!"): 1, ord("p"): 2, ord("C"): 1, ord("S"): 2, ord("W"): 1}

log = logging.getLogger("fake_printer")


def escpos_to_text(data: bytes, encoding: str = "cp437") -> str:
    """Decode an ESC/POS stream to text; cuts become a marker line."""
    out = []
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == ESC and i + 1 < n:
            cmd = data[i + 1]
            i += 2
            if cmd == ord("*") and i + 3 <= n:
                m, nl, nh = data[i], data[i + 1], data[i + 2]
                i += 3 + (nl + 256 * nh) * (3 if m in (32, 33) else 1)
            elif cmd == ord("D"):
                while i < n and data[i] != 0:
                    i += 1
                i += 1
            else:
                i += _ESC_LEN.get(cmd, 1)
            continue
        if b == GS and i + 1 < n:
            cmd = data[i + 1]
            i += 2
            if cmd == ord("V") and i < n:
                m = data[i]
                i += 2 if m in (65, 66, 97, 98, 103, 104) else 1
                out.append("\n" + CUT_MARKER + "\n")
            elif cmd == ord("k") and i < n:
                m = data[i]
                i += 1
                if m <= 6:
                    while i < n and data[i] != 0:
                        i += 1
                    i += 1
                elif i < n:
                    i += 1 + data[i]
            elif cmd == ord("(") and i + 3 <= n:
                i += 3 + data[i + 1] + 256 * data[i + 2]
            elif cmd == ord("v") and i + 5 <= n and data[i] == ord("0"):
                xl, xh, yl, yh = data[i + 2], data[i + 3], data[i + 4], data[i + 5]
                i += 6 + (xl + 256 * xh) * (yl + 256 * yh)
                out.append("[image]\n")
            else:
                i += _GS_LEN.get(cmd, 1)
            continue
        if b == FS and i + 1 < n:
            i += 2 + _FS_LEN.get(data[i + 1], 1)
            continue
        if b == DLE and i + 1 < n:
            i += 2 + (7 if data[i + 1] == 0x14 else 1)
            continue
        if b == 0x0A:
            out.append("\n")
        elif b == 0x09:
            out.append("\t")
        elif b >= 0x20:
            out.append(bytes([b]).decode(encoding, "replace"))
        i += 1
    return "".join(out)


class FakePrinter:
    def __init__(self, maker: str, model: str, flush_after: float) -> None:
        self.maker = maker
        self.model = model
        self.flush_after = flush_after
        self.jobs = 0

    def _reply(self, query: bytes) -> Optional[bytes]:
        """Answers for the few queries discovery and python-escpos send."""
        if query.startswith(b"\x1d\x49"):
            n = query[2]
            if n == 0x42:  # GS I 66: maker
                return b"_" + self.maker.encode("ascii", "ignore") + b"\x00"
            if n == 0x43:  # GS I 67: model
                return b"_" + self.model.encode("ascii", "ignore") + b"\x00"
            if n == 0x41:  # GS I 65: firmware
                return b"_1.00\x00"
            if n in (1, 49):  # model id byte
                return b"\x20"
            if n in (2, 50):
                return b"\x02"
            return b"\x00"
        if query.startswith(b"\x10\x04"):  # DLE EOT n: real-time status -> "all fine"
            return b"\x12"
        return None

    def _extract_queries(self, chunk: bytes) -> list[bytes]:
        """Find query commands inside a chunk (they can share a packet with data)."""
        found = []
        i = 0
        while i + 2 < len(chunk):
            if chunk[i] == GS and chunk[i + 1] == 0x49:
                found.append(chunk[i : i + 3])
                i += 3
            elif chunk[i] == DLE and chunk[i + 1] == 0x04:
                found.append(chunk[i : i + 3])
                i += 3
            else:
                i += 1
        return found

    def _log_job(self, peer: str, data: bytes) -> None:
        if not data.strip(b"\x00"):
            return
        text = escpos_to_text(data).strip("\n")
        if not text.strip():
            return
        self.jobs += 1
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.info(
            f"===== JOB #{self.jobs} from {peer} at {stamp} ({len(data)} bytes) =====\n"
            f"{text}\n===== END JOB #{self.jobs} ====="
        )

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = ":".join(str(p) for p in writer.get_extra_info("peername")[:2])
        log.info(f"Connection from {peer}")
        buffer = bytearray()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(65536), timeout=self.flush_after)
                except asyncio.TimeoutError:
                    if buffer:
                        self._log_job(peer, bytes(buffer))
                        buffer.clear()
                    continue
                if not chunk:
                    break
                for query in self._extract_queries(chunk):
                    reply = self._reply(query)
                    if reply is not None:
                        writer.write(reply)
                        await writer.drain()
                        log.debug(f"Answered query {query.hex()} with {reply!r}")
                buffer.extend(chunk)
                # Flush on cut so every ticket shows up as one job.
                while True:
                    idx = buffer.find(b"\x1d\x56")
                    if idx < 0:
                        break
                    end = idx + (4 if idx + 2 < len(buffer) and buffer[idx + 2] in (65, 66, 97, 98, 103, 104) else 3)
                    self._log_job(peer, bytes(buffer[:end]))
                    del buffer[:end]
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            if buffer:
                self._log_job(peer, bytes(buffer))
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.info(f"Connection from {peer} closed")


async def serve(host: str, port: int, printer: FakePrinter) -> None:
    server = await asyncio.start_server(printer.handle, host, port)
    log.info(f"Fake printer '{printer.maker} {printer.model}' listening on {host}:{port}")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    async with server:
        await stop.wait()
    log.info(f"Stopped after {printer.jobs} job(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description="ESC/POS network printer emulator (TCP 9100)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--maker", default="OPENEOS", help="Answer to GS I 66 (maker name)")
    parser.add_argument("--model", default="FAKE-9100", help="Answer to GS I 67 (model name)")
    parser.add_argument("--log-file", default=None, help="Also write the log to this file")
    parser.add_argument("--flush-after", type=float, default=2.0, help="Seconds of silence before a job is logged")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(message)s",
        handlers=handlers,
    )
    try:
        asyncio.run(serve(args.host, args.port, FakePrinter(args.maker, args.model, args.flush_after)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
