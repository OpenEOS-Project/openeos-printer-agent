import io
import logging
import re
from typing import Any, Optional

import qrcode
from PIL import Image

logger = logging.getLogger(__name__)

# Markup tag pattern. We use angle-bracket delimiters `<<X>>` for new templates
# because the previous `{{X}}` form collides with Jinja2's expression syntax
# and breaks rendering for closing tags (`{{/BOLD}}`) and argument-bearing
# tags (`{{FEED:3}}`, `{{BARCODE:CODE128:value}}`).
#
# Legacy `{{X}}` markup is still recognized so cached / older server templates
# keep working — but new templates SHOULD emit `<<X>>`.
TAG_PATTERN = re.compile(r"<<(/?\w+)(?::([^>]*))?>>|\{\{(/?\w+)(?::([^}]*))?\}\}")


def render_to_printer(printer: Any, text: str, options: Optional[dict[str, Any]] = None) -> None:
    """Render markup text to an ESC/POS printer.

    Translates markup tags to ESC/POS commands. Runs synchronously
    (should be called via run_in_executor).

    Args:
        printer: python-escpos printer instance
        text: Rendered template text with markup tags
        options: Optional dict with 'copies' (int)
    """
    options = options or {}
    copies = options.get("copies", 1)

    for copy_num in range(copies):
        if copy_num > 0:
            printer.cut()

        _render_text(printer, text)


def _render_text(printer: Any, text: str) -> None:
    """Process text line by line, handling markup tags."""
    lines = text.split("\n")
    buffer = ""

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Empty line -> print buffer and add line feed
            if buffer:
                printer.text(buffer + "\n")
                buffer = ""
            else:
                printer.text("\n")
            continue

        # Process tags in this line
        _process_line(printer, stripped)


def _process_line(printer: Any, line: str) -> None:
    """Process a single line, executing tags and printing text.

    Flush semantics:
    * Block commands (CUT/FEED/QRCODE/...) — flush buffered text WITH \n
      (committing the current line) before invoking the command.
    * Alignment tags (CENTER/LEFT/RIGHT and their closers) — flush WITH \n
      because ESC/POS alignment is line-level: the alignment that's active
      when `\n` arrives is what the printer applies to the buffered text. So
      `<<CENTER>>foo<</CENTER>>` MUST commit "foo\n" while the printer is
      still in center mode, then switch to left for the next line.
    * Inline style tags (BOLD/BIG/UNDERLINE) — flush WITHOUT \n so that
      `<<BIG>>foo<</BIG>>` actually doubles "foo" mid-line.

    Newline accounting: if any flush-with-\n already happened on this input
    line, no trailing \n is added at end-of-line — otherwise we'd emit blank
    lines after every centered header.
    """
    pos = 0
    text_buffer = ""
    emitted_newline_inline = False

    while pos < len(line):
        match = TAG_PATTERN.search(line, pos)

        if not match:
            text_buffer += line[pos:]
            break

        text_buffer += line[pos : match.start()]

        # Group 1/2 = angle-bracket markup, group 3/4 = legacy curly markup.
        tag = match.group(1) or match.group(3)
        arg = match.group(2) if match.group(1) else match.group(4)

        is_block = tag in ("CUT", "OPEN_DRAWER", "FEED", "QRCODE", "IMAGE", "BARCODE")
        is_align = tag in ("CENTER", "/CENTER", "RIGHT", "/RIGHT", "LEFT", "/LEFT")
        needs_newline_flush = is_block or is_align

        if needs_newline_flush:
            if text_buffer:
                printer.text(text_buffer + "\n")
                text_buffer = ""
                emitted_newline_inline = True
        elif text_buffer:
            printer.text(text_buffer)
            text_buffer = ""

        _handle_tag(printer, tag, arg)

        pos = match.end()

    # End-of-line: emit a \n only if we need one. We need one when there's
    # still buffered text, OR when nothing was emitted at all on this line
    # (so the input line still advances to a new physical line).
    if text_buffer:
        printer.text(text_buffer + "\n")
    elif not emitted_newline_inline:
        printer.text("\n")


def _handle_tag(printer: Any, tag: str, arg: Optional[str]) -> bool:
    """Handle a single markup tag. Returns True if tag was a block command."""
    try:
        if tag == "BOLD":
            printer.set(bold=True)
        elif tag == "/BOLD":
            printer.set(bold=False)

        elif tag == "BIG":
            # python-escpos's `width`/`height` params are silent no-ops unless
            # `custom_size=True` is also passed. For 2x scaling we use the
            # native double_width/double_height flags, which emit the GS ! 0x11
            # ESC/POS command supported by virtually every receipt printer.
            printer.set(double_width=True, double_height=True)
        elif tag == "/BIG":
            printer.set(normal_textsize=True)

        elif tag == "UNDERLINE":
            printer.set(underline=1)
        elif tag == "/UNDERLINE":
            printer.set(underline=0)

        elif tag == "CENTER":
            printer.set(align="center")
        elif tag == "/CENTER":
            printer.set(align="left")

        elif tag == "RIGHT":
            printer.set(align="right")
        elif tag == "/RIGHT":
            printer.set(align="left")

        elif tag == "LEFT":
            printer.set(align="left")
        elif tag == "/LEFT":
            printer.set(align="left")

        elif tag == "CUT":
            printer.cut()
            return True

        elif tag == "OPEN_DRAWER":
            printer.cashdraw(2)
            return True

        elif tag == "FEED":
            count = int(arg) if arg else 1
            for _ in range(count):
                printer.text("\n")
            return True

        elif tag == "QRCODE":
            if arg:
                _print_qrcode(printer, arg)
            return True

        elif tag == "IMAGE":
            if arg:
                _print_image(printer, arg)
            return True

        elif tag == "BARCODE":
            # Markup form: <<BARCODE:CODE128:value>> — arg is "CODE128:value".
            # Older form without a type prefix (just value) defaults to CODE128.
            if arg:
                if ":" in arg:
                    barcode_type, code = arg.split(":", 1)
                else:
                    barcode_type, code = "CODE128", arg
                _print_software_barcode(printer, barcode_type, code)
            return True

        else:
            logger.warning(f"Unknown tag: {tag}")

    except Exception as e:
        logger.error(f"Error handling tag {tag}: {e}")

    return False


def _print_software_barcode(printer: Any, bc_type: str, value: str) -> None:
    """Render a barcode as a bitmap and print it.

    We use python-barcode directly (rather than python-escpos's built-in
    barcode helper) because:
    - The hardware CODE128 path is unreliable on cheap thermal printers (some
      accept the GS k command silently and print nothing).
    - python-escpos's software fallback embeds the value as text below the
      bars, includes the `{A`/`{B`/`{C` subset prefix, and offers no way to
      suppress it. We want the human-readable text to be a separate explicit
      template line so the kitchen can read clean text without the prefix.
    - `bitImageRaster` mode is faster on most printers than column mode.
    """
    try:
        from barcode import get_barcode_class  # python-barcode
        from barcode.writer import ImageWriter
    except ImportError:
        # Fall back to the lib's built-in barcode if python-barcode is missing.
        printer.barcode(value, bc_type, function_type="B")
        return

    try:
        cls_name = bc_type.lower().replace("_", "")
        # python-barcode names: "code128", "code39", "ean13", ...
        cls = get_barcode_class(cls_name) if cls_name else get_barcode_class("code128")
        bc = cls(value, writer=ImageWriter())
        img = bc.render(
            writer_options={
                "write_text": False,  # suppress text below bars (we print our own)
                "module_height": 8.0,  # 8 mm tall bars — readable + ~30% less data
                "module_width": 0.25,  # ~2 dots/module @ 203dpi; 36-char UUID fits 80mm
                "quiet_zone": 2.0,
            }
        )
        # Try fastest impl first; fall back if the printer profile rejects it.
        for impl in ("graphics", "bitImageRaster", "bitImageColumn"):
            try:
                printer.image(img, impl=impl)
                return
            except TypeError:
                # Older python-escpos without `impl` kwarg → use default once.
                printer.image(img)
                return
            except Exception:  # noqa: BLE001
                continue
        logger.error(f"All barcode image impls failed for {bc_type}={value!r}")
    except Exception as e:  # noqa: BLE001
        logger.error(f"Software barcode rendering failed for {bc_type}={value!r}: {e}")


def _print_qrcode(printer: Any, data: str) -> None:
    """Generate QR code and print as image."""
    try:
        qr = qrcode.QRCode(version=1, box_size=4, border=2)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")

        # Convert to PIL Image for printer.image()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        pil_img = Image.open(buf)
        printer.image(pil_img)
    except Exception as e:
        logger.error(f"Failed to print QR code: {e}")
        printer.text(f"[QR: {data}]\n")


def _print_image(printer: Any, path: str) -> None:
    """Load and print an image file."""
    try:
        img = Image.open(path)
        printer.image(img)
    except Exception as e:
        logger.error(f"Failed to print image {path}: {e}")
        printer.text(f"[IMG: {path}]\n")
