"""Smoke test of the Qt window on the offscreen platform (no display).

Skipped where PySide6 or its system libraries (libEGL, ...) are missing.
"""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

QtWidgets = pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from src import service_control as sc  # noqa: E402
from src.ui import app as ui_app  # noqa: E402
from src.ui import viewmodel as vm  # noqa: E402
from src.ui.i18n import Translator  # noqa: E402
from tests.test_ui_viewmodel import _state  # noqa: E402


class StubWorker:
    def __init__(self):
        self.test_prints = []
        self.actions = []

    def test_print(self, printer_id):
        self.test_prints.append(printer_id)

    def service_action(self, action):
        self.actions.append(action)


@pytest.fixture(scope="module")
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


@pytest.fixture
def window(qapp):
    worker = StubWorker()
    win = ui_app.MainWindow(Translator("de"), worker, 8080, quit_app=lambda: None)  # type: ignore[arg-type]
    win.show()
    yield win, worker
    win.hide()
    win.deleteLater()


def _view(data, service=sc.RUNNING):
    return vm.build_view_state(data, service, Translator("de"), service_kind="windows", ui_version="0.3.0")


def test_pairing_page_shows_code(window):
    win, _ = window
    win.show_view(_view(_state(pairing={"status": "pending", "code": "12345678", "organization": None})))
    assert win.stack.currentIndex() == 0
    assert win.code.text() == "1234 5678"
    assert "Noch nicht verbunden" in win.pill.text()


def test_overview_lists_printers_and_test_print(window):
    win, worker = window
    win.show_view(_view(_state()))
    assert win.stack.currentIndex() == 1
    assert win.tree.topLevelItemCount() == 1
    assert win.lbl_org.text() == "TSV Musterstadt"
    button = win.tree.itemWidget(win.tree.topLevelItem(0), 4)
    button.click()
    assert worker.test_prints == ["p1"]
    assert win.tabs.tabText(0) == "Drucker (1)"


def test_service_down_page_offers_start(window):
    win, worker = window
    win.show_view(_view(None, service=sc.STOPPED))
    assert win.stack.currentIndex() == 2
    assert not win.btn_start_big.isHidden()
    win.btn_start_big.click()
    assert worker.actions == ["start"]


def test_tray_icons_render(qapp):
    for flavour in ("ok", "warn", "pairing", "error", "idle", None):
        pixmap = ui_app.logo_pixmap(32, flavour)
        assert not pixmap.isNull() and pixmap.width() == 32
    assert not ui_app.logo_icon("ok").isNull()


def test_parser():
    args = ui_app.build_parser().parse_args(["--minimized", "--port", "18090", "--lang", "en"])
    assert args.minimized and args.port == 18090 and args.lang == "en"
