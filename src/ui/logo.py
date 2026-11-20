"""The OpenEOS mark (dark square, four white bars forming an open frame) as
plain geometry, shared by the Qt tray icons and tools/make_icons.py.

Coordinates are fractions of the square's edge, taken from the web app's
``icon.svg`` (viewBox 112.5).
"""

_VIEWBOX = 112.5

# (x1, y1, x2, y2) centre lines of the bars, stroke width below
_BARS_SVG = (
    (11.53, 97.07, 74.57, 97.07),  # bottom
    (15.26, 36.99, 15.26, 98.56),  # left
    (37.75, 14.93, 100.79, 14.93),  # top
    (97.06, 13.43, 97.06, 75.00),  # right
)
_STROKE_SVG = 7.47

BARS = tuple(tuple(v / _VIEWBOX for v in bar) for bar in _BARS_SVG)
STROKE = _STROKE_SVG / _VIEWBOX

INK = "#0c0f0d"
PAPER = "#ffffff"

# Status dot colours for the tray icon (see viewmodel.TRAY_*)
DOT_COLORS = {
    "ok": "#2f9e44",
    "warn": "#f08c00",
    "pairing": "#1c7ed6",
    "error": "#e03131",
    "idle": "#868e96",
}
