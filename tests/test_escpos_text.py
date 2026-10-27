from escpos.printer import Dummy

from src.escpos_text import CUT_MARKER, escpos_to_text


class TestEscposToText:
    def test_plain_text_and_newlines(self):
        assert escpos_to_text(b"Hallo\nWelt\n") == "Hallo\nWelt\n"

    def test_strips_style_commands(self):
        data = b"\x1b@\x1ba\x01\x1bE\x01Bold\x1bE\x00\n\x1d!\x11Big\x1d!\x00\n"
        assert escpos_to_text(data) == "Bold\nBig\n"

    def test_cut_marker(self):
        assert escpos_to_text(b"A\n\x1dV\x41\x03B\n") == f"A\n\n{CUT_MARKER}\nB\n"
        assert escpos_to_text(b"A\n\x1dV\x00B\n") == f"A\n\n{CUT_MARKER}\nB\n"

    def test_barcode_and_qr_payloads_skipped(self):
        barcode = b"\x1dk\x04123456\x00"
        qr = b"\x1d(k\x05\x001A\x00\x00\x00"
        assert escpos_to_text(b"x" + barcode + b"y" + qr + b"z") == "xyz"

    def test_real_dummy_output_roundtrip(self):
        p = Dummy()
        p.set(align="center", bold=True)
        p.text("OPENEOS\n")
        p.set(align="left", bold=False)
        p.text("2x Bratwurst   7,00\n")
        p.cut()
        text = escpos_to_text(bytes(p.output))
        assert "OPENEOS" in text
        assert "2x Bratwurst   7,00" in text
        assert CUT_MARKER in text
        assert "\x1b" not in text and "\x1d" not in text
