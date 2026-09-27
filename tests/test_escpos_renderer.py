from unittest.mock import MagicMock, call

from escpos.printer import Dummy
from PIL import Image

from src.escpos_renderer import _handle_tag, render_to_printer


class TestRenderToPrinter:
    def _make_printer(self):
        printer = MagicMock()
        return printer

    def test_plain_text(self):
        printer = self._make_printer()
        render_to_printer(printer, "Hello World")
        printer.text.assert_called()
        # Check the text was passed through
        text_calls = [str(c) for c in printer.text.call_args_list]
        combined = " ".join(text_calls)
        assert "Hello World" in combined

    def test_bold_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{BOLD}}Bold text{{/BOLD}}")
        printer.set.assert_any_call(bold=True)
        printer.set.assert_any_call(bold=False)

    def test_big_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{BIG}}Big text{{/BIG}}")
        # BIG/ /BIG use python-escpos's native double-width/height flags and
        # the normal_textsize reset, not the width=/height= kwargs (those are
        # silent no-ops without custom_size=True).
        printer.set.assert_any_call(double_width=True, double_height=True)
        printer.set.assert_any_call(normal_textsize=True)

    def test_underline_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{UNDERLINE}}Underlined{{/UNDERLINE}}")
        printer.set.assert_any_call(underline=1)
        printer.set.assert_any_call(underline=0)

    def test_center_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{CENTER}}Centered{{/CENTER}}")
        printer.set.assert_any_call(align="center")
        printer.set.assert_any_call(align="left")

    def test_right_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{RIGHT}}Right{{/RIGHT}}")
        printer.set.assert_any_call(align="right")
        printer.set.assert_any_call(align="left")

    def test_cut_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{CUT}}")
        printer.cut.assert_called_once()

    def test_open_drawer_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{OPEN_DRAWER}}")
        printer.cashdraw.assert_called_once_with(2)

    def test_feed_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{FEED:3}}")
        # Should produce 3 newlines
        newline_calls = [c for c in printer.text.call_args_list if c == call("\n")]
        assert len(newline_calls) >= 3

    def test_barcode_tag(self):
        printer = self._make_printer()
        render_to_printer(printer, "{{BARCODE:12345}}")
        # python-barcode is installed transitively (python-escpos depends on
        # it), so barcodes render as a bitmap via printer.image(), not the
        # native printer.barcode() GS-k command. The native path is only a
        # fallback used when python-barcode is unavailable.
        printer.image.assert_called_once()
        assert printer.barcode.call_count == 0

    def test_multiple_copies(self):
        printer = self._make_printer()
        render_to_printer(printer, "Test\n{{CUT}}", {"copies": 3})
        # 3 copies: original CUT in each + 2 extra CUTs between copies
        assert printer.cut.call_count == 5  # 3 from template + 2 between copies

    def test_mixed_tags(self):
        printer = self._make_printer()
        text = "{{CENTER}}{{BOLD}}Title{{/BOLD}}{{/CENTER}}\nNormal text\n{{CUT}}"
        render_to_printer(printer, text)
        printer.set.assert_any_call(align="center")
        printer.set.assert_any_call(bold=True)
        printer.cut.assert_called()

    def test_empty_text(self):
        printer = self._make_printer()
        render_to_printer(printer, "")
        # Should not crash


class TestHandleTag:
    def test_unknown_tag(self):
        printer = MagicMock()
        result = _handle_tag(printer, "UNKNOWN_TAG", None)
        assert result is False


class TestRealPillowRendering:
    """Every test above uses a MagicMock printer, so printer.image() is never
    actually called with a real Pillow image — these tests never exercise
    qrcode/PIL/python-barcode themselves and wouldn't notice a Pillow version
    bump breaking image generation (see #15: Pillow's ceiling was raised to
    admit Python 3.13+ wheels).

    These use python-escpos's own Dummy printer instead, which is real
    (non-mocked) code that turns a Pillow Image into ESC/POS raster bytes —
    so a Pillow API break in printer.image()'s image-handling path would
    surface here as an exception, not just a changed byte count.
    """

    def test_qrcode_renders_without_error(self):
        printer = Dummy()
        render_to_printer(printer, "<<QRCODE:https://openeos.de/order/1>>")
        # A real ESC/POS raster image sequence was emitted, not the
        # "[QR: ...]" fallback text printed on error.
        assert len(printer.output) > 100
        assert b"[QR:" not in printer.output

    def test_barcode_renders_without_error(self):
        printer = Dummy()
        render_to_printer(printer, "<<BARCODE:CODE128:ABC1234567890>>")
        assert len(printer.output) > 100

    def test_image_file_renders_without_error(self, tmp_path):
        logo_path = tmp_path / "logo.png"
        img = Image.new("1", (32, 32))
        for y in range(32):
            for x in range(32):
                img.putpixel((x, y), 1 if (x // 4 + y // 4) % 2 == 0 else 0)
        img.save(logo_path)

        printer = Dummy()
        render_to_printer(printer, f"<<IMAGE:{logo_path}>>")
        assert len(printer.output) > 50
        assert b"[IMG:" not in printer.output
