"""Turn a raw ESC/POS byte stream back into readable text.

Used by the ``--dummy-printer`` mode to log what would have been printed.
Command parameters are skipped by their documented length; unknown commands
skip a single byte. Cuts become a visible marker so multi-ticket output
stays readable. Not a full parser - good enough to eyeball a ticket.
"""

ESC = 0x1B
GS = 0x1D
FS = 0x1C
DLE = 0x10

CUT_MARKER = "--- CUT ---"

# Parameter byte counts for the fixed-length commands.
_ESC_LEN: dict[int, int] = {
    ord("@"): 0, ord("!"): 1, ord("a"): 1, ord("E"): 1, ord("-"): 1, ord("d"): 1,
    ord("J"): 1, ord("p"): 3, ord("t"): 1, ord("M"): 1, ord("{"): 1, ord("G"): 1,
    ord("R"): 1, ord("2"): 0, ord("3"): 1, ord("c"): 2, ord("i"): 0, ord("m"): 0,
    ord("="): 1, ord("S"): 0, ord("T"): 0, ord("L"): 0, ord("W"): 8, ord("$"): 2,
    ord("\\"): 2, ord("e"): 1, ord("f"): 2, ord("s"): 1, ord("r"): 1, ord("u"): 1, ord("v"): 0,
}  # fmt: skip
_GS_LEN: dict[int, int] = {
    ord("!"): 1, ord("B"): 1, ord("a"): 1, ord("L"): 2, ord("W"): 2, ord("h"): 1,
    ord("w"): 1, ord("H"): 1, ord("f"): 1, ord("I"): 1, ord("r"): 1, ord("E"): 1,
    ord("P"): 2, ord("b"): 1, ord("$"): 2, ord("\\"): 2, ord("^"): 3, ord(":"): 0,
    ord("T"): 1, ord("V"): 1, ord("g"): 3,
}  # fmt: skip
_FS_LEN: dict[int, int] = {ord("&"): 0, ord("."): 0, ord("!"): 1, ord("p"): 2, ord("C"): 1, ord("S"): 2, ord("W"): 1}


def escpos_to_text(data: bytes, encoding: str = "cp437") -> str:
    """Decode ``data`` to text, dropping control sequences."""
    out: list[str] = []
    i = 0
    n = len(data)

    while i < n:
        b = data[i]

        if b == ESC and i + 1 < n:
            cmd = data[i + 1]
            i += 2
            if cmd == ord("*") and i + 3 <= n:
                # Bit image: m nL nH + columns * (1 or 3) bytes
                m, nl, nh = data[i], data[i + 1], data[i + 2]
                width = nl + 256 * nh
                i += 3 + width * (3 if m in (32, 33) else 1)
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
                    length = data[i]
                    i += 1 + length
            elif cmd == ord("(") and i + 3 <= n:
                length = data[i + 1] + 256 * data[i + 2]
                i += 3 + length
            elif cmd == ord("v") and i + 5 <= n and data[i] == ord("0"):
                xl, xh, yl, yh = data[i + 2], data[i + 3], data[i + 4], data[i + 5]
                i += 6 + (xl + 256 * xh) * (yl + 256 * yh)
                out.append("[image]\n")
            else:
                i += _GS_LEN.get(cmd, 1)
            continue

        if b == FS and i + 1 < n:
            cmd = data[i + 1]
            i += 2 + _FS_LEN.get(cmd, 1)
            continue

        if b == DLE and i + 1 < n:
            cmd = data[i + 1]
            i += 2 + (7 if cmd == 0x14 else 1)
            continue

        if b == 0x0A:
            out.append("\n")
        elif b == 0x09:
            out.append("\t")
        elif b >= 0x20:
            out.append(bytes([b]).decode(encoding, "replace"))
        # other control bytes (CR, FF, BEL, ...) are dropped
        i += 1

    return "".join(out)
