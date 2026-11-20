#!/usr/bin/env python3
"""Render the app icons from src/ui/logo.py with Pillow.

    python tools/make_icons.py

Writes packaging/icons/openeos-printer-agent.png (256 px, Linux desktop
entry) and .ico (16-256 px, Windows exe, installer, shortcuts). The files
are committed; rerun only when the mark changes.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ui.logo import BARS, INK, PAPER, STROKE  # noqa: E402

OUT = ROOT / "packaging" / "icons"


def render(size: int, scale: int = 4) -> Image.Image:
    big = size * scale
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    radius = int(big * 0.18)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=radius, fill=INK)
    width = max(1, round(STROKE * big))
    for x1, y1, x2, y2 in BARS:
        draw.line((x1 * big, y1 * big, x2 * big, y2 * big), fill=PAPER, width=width)
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    render(256).save(OUT / "openeos-printer-agent.png")
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    render(256).save(OUT / "openeos-printer-agent.ico", sizes=[(s, s) for s in sizes])
    print(f"Wrote icons to {OUT}")


if __name__ == "__main__":
    main()
