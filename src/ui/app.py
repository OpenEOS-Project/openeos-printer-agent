"""Window + tray icon (PySide6).

    openeos-printer-agent-ui [--minimized] [--port 8080] [--lang de|en]

Polls the agent every two seconds in a worker thread and renders the
``ViewState`` from ``viewmodel``. Closing the window hides it to the tray;
"Beenden" in the tray menu quits only this UI - the service keeps printing.

``--capture FILE`` (with ``--wait-state STATE``) takes a screenshot of the
window once the agent reports that state and exits; the release workflow
and the docs use it for real screenshots on Windows and Linux.
"""

import argparse
import getpass
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QCloseEvent,
    QColor,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import service_control as sc
from ..version import get_version
from . import viewmodel as vm
from .agent_client import AgentClient, AgentError, AgentUnreachable, discover_port
from .i18n import Translator, detect_language
from .logo import BARS, DOT_COLORS, INK, PAPER, STROKE

POLL_INTERVAL_MS = 2000
SINGLE_INSTANCE_KEY = "openeos-printer-agent-ui"


# -- icons ------------------------------------------------------------------


def logo_pixmap(size: int, dot: Optional[str] = None) -> QPixmap:
    """The OpenEOS mark, optionally with a status dot (tray)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(QColor(INK)))
    p.drawRoundedRect(QRectF(0, 0, size, size), size * 0.18, size * 0.18)
    pen = QPen(QColor(PAPER))
    pen.setWidthF(max(1.0, STROKE * size))
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    p.setPen(pen)
    for x1, y1, x2, y2 in BARS:
        p.drawLine(QPointF(x1 * size, y1 * size), QPointF(x2 * size, y2 * size))
    if dot:
        r = size * 0.24
        center = QPointF(size - r - size * 0.02, size - r - size * 0.02)
        p.setPen(QPen(QColor(PAPER), max(1.0, size * 0.06)))
        p.setBrush(QBrush(QColor(DOT_COLORS.get(dot, DOT_COLORS["idle"]))))
        p.drawEllipse(center, r, r)
    p.end()
    return pm


def logo_icon(dot: Optional[str] = None) -> QIcon:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(logo_pixmap(size, dot))
    return icon


# -- background work -----------------------------------------------------------


@dataclass
class Snapshot:
    data: Optional[dict[str, Any]]
    service_state: str
    outdated: bool = False


class Worker(QObject):
    """Runs blocking calls (HTTP, service manager) off the GUI thread.
    Signals emitted from the pool threads are queued to the GUI thread."""

    snapshot = Signal(object)
    message = Signal(str, bool)  # text, is_error
    action_finished = Signal()

    def __init__(self, client: AgentClient, controller: Optional[sc.ServiceController], tr: Translator) -> None:
        super().__init__()
        self._client = client
        self._controller = controller
        self._tr = tr
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ui-worker")
        self._polling = False

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def poll(self) -> None:
        if self._polling:
            return
        self._polling = True
        self._pool.submit(self._poll)

    def _poll(self) -> None:
        try:
            data: Optional[dict[str, Any]] = None
            outdated = False
            try:
                data = self._client.state()
            except AgentError as e:
                outdated = e.status == 404
            except AgentUnreachable:
                pass
            service_state = self._controller.state() if self._controller else sc.UNKNOWN
            self.snapshot.emit(Snapshot(data, service_state, outdated))
        except Exception as e:  # never let the poller die silently
            self.message.emit(str(e), True)
        finally:
            self._polling = False

    def service_action(self, action: str) -> None:
        self._pool.submit(self._service_action, action)

    def _service_action(self, action: str) -> None:
        controller = self._controller
        try:
            if controller is None:
                raise sc.ServiceControlError("no service manager")
            sc.run_action(controller, action)
            self.message.emit(self._tr("busy.service", action=self._tr(f"busy.action.{action}")), False)
        except sc.ServiceControlError as e:
            if e.permission_denied and isinstance(controller, sc.WindowsServiceController):
                if self._elevate(controller, action):
                    self.message.emit(self._tr("busy.service", action=self._tr(f"busy.action.{action}")), False)
                else:
                    self.message.emit(self._tr("error.service_denied"), True)
            elif e.permission_denied:
                self.message.emit(self._tr("error.service_denied"), True)
            else:
                self.message.emit(self._tr("error.service", error=str(e)), True)
        finally:
            self.action_finished.emit()
            self.poll()

    @staticmethod
    def _elevate(controller: "sc.WindowsServiceController", action: str) -> bool:
        """UAC prompt: run ``<agent exe> <action>-service`` as administrator."""
        exe = controller.executable()
        if not exe:
            return False
        try:
            import ctypes

            rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "runas", exe, f"{action}-service", None, 0
            )
        except Exception:
            return False
        return int(rc) > 32

    def test_print(self, printer_id: str) -> None:
        self._pool.submit(self._test_print, printer_id)

    def _test_print(self, printer_id: str) -> None:
        try:
            self._client.test_print(printer_id)
            self.message.emit(self._tr("action.test_print_sent"), False)
        except (AgentError, AgentUnreachable) as e:
            self.message.emit(self._tr("error.test_print", error=str(e)), True)
        finally:
            self.poll()


# -- window ----------------------------------------------------------------------


def _section_title(text: str) -> QLabel:
    label = QLabel(text)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


def _muted(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: #5f645c;")
    return label


class MainWindow(QMainWindow):
    def __init__(self, tr: Translator, worker: Worker, port: int, quit_app: Callable[[], None]) -> None:
        super().__init__()
        self._tr = tr
        self._worker = worker
        self._port = port
        self._quit_app = quit_app
        self._view: Optional[vm.ViewState] = None
        self._tray_hint_shown = False
        self.tray_available = False
        self.tray: Optional[QSystemTrayIcon] = None

        self.setWindowTitle(tr("app_title"))
        self.setWindowIcon(logo_icon())
        self.resize(780, 600)
        self.setMinimumSize(560, 420)

        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 18, 20, 12)
        outer.setSpacing(12)

        # header: logo, title, status pill
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(logo_pixmap(36))
        header.addWidget(logo)
        title = QLabel(tr("app_title"))
        tfont = title.font()
        tfont.setPointSizeF(tfont.pointSizeF() * 1.45)
        tfont.setBold(True)
        title.setFont(tfont)
        header.addWidget(title)
        header.addStretch(1)
        self.pill = QLabel()
        self.pill.setObjectName("pill")
        header.addWidget(self.pill)
        outer.addLayout(header)

        self.detail = QLabel()
        self.detail.setWordWrap(True)
        dfont = self.detail.font()
        dfont.setPointSizeF(dfont.pointSizeF() * 1.1)
        self.detail.setFont(dfont)
        outer.addWidget(self.detail)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_pairing_page())
        self.stack.addWidget(self._build_overview_page())
        self.stack.addWidget(self._build_down_page())
        outer.addWidget(self.stack, 1)

        # actions
        actions = QHBoxLayout()
        self.btn_web = QPushButton(tr("action.open_web"))
        self.btn_web.clicked.connect(self._open_web)
        self.btn_status = QPushButton(tr("action.status_page"))
        self.btn_status.clicked.connect(self._open_status_page)
        self.btn_logs = QPushButton(tr("action.logs"))
        self.btn_logs.clicked.connect(self._open_logs)
        for b in (self.btn_web, self.btn_status, self.btn_logs):
            actions.addWidget(b)
        actions.addStretch(1)
        self.btn_service = QToolButton()
        self.btn_service.setText(tr("action.restart_service"))
        self.btn_service.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.btn_service.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        menu = QMenu(self.btn_service)
        self.act_start = menu.addAction(tr("action.start_service"), lambda: self._service("start"))
        self.act_stop = menu.addAction(tr("action.stop_service"), lambda: self._service("stop"))
        self.act_restart = menu.addAction(tr("action.restart_service"), lambda: self._service("restart"))
        self.btn_service.setMenu(menu)
        actions.addWidget(self.btn_service)
        outer.addLayout(actions)

        self.message = QLabel()
        self.message.setWordWrap(True)
        self.message.hide()
        outer.addWidget(self.message)
        self.footer = _muted()
        ffont = self.footer.font()
        ffont.setPointSizeF(ffont.pointSizeF() * 0.9)
        self.footer.setFont(ffont)
        outer.addWidget(self.footer)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)

        self._message_timer = QTimer(self)
        self._message_timer.setSingleShot(True)
        self._message_timer.timeout.connect(self.message.hide)

    # pages

    def _build_pairing_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 8, 0, 8)
        lay.addStretch(1)
        instr = QLabel(self._tr("pairing.instructions"))
        instr.setWordWrap(True)
        instr.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ifont = instr.font()
        ifont.setPointSizeF(ifont.pointSizeF() * 1.15)
        instr.setFont(ifont)
        lay.addWidget(instr)

        self.code = QLabel()
        self.code.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.code.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cfont = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        cfont.setPointSize(46)
        cfont.setBold(True)
        cfont.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 4)
        self.code.setFont(cfont)
        self.code.setStyleSheet(
            "QLabel { background: #fbfaf5; color: #0c0f0d; border: 2px solid #0c0f0d;"
            " border-radius: 14px; padding: 14px 28px; }"
        )
        self.code.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.code)
        row.addStretch(1)
        lay.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.btn_copy = QPushButton(self._tr("pairing.copy"))
        self.btn_copy.clicked.connect(self._copy_code)
        buttons.addWidget(self.btn_copy)
        open_web = QPushButton(self._tr("action.open_web"))
        open_web.setDefault(True)
        open_web.clicked.connect(self._open_web)
        buttons.addWidget(open_web)
        buttons.addStretch(1)
        lay.addLayout(buttons)

        hint = _muted(self._tr("pairing.hint"))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(hint)
        lay.addStretch(1)
        return page

    def _build_overview_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        # compact connection facts: two label/value pairs per row
        facts = QFrame()
        facts.setObjectName("facts")
        facts.setStyleSheet(
            "QFrame#facts { background: palette(base); border: 1px solid palette(mid); border-radius: 8px; }"
        )
        grid = QGridLayout(facts)
        grid.setContentsMargins(14, 10, 14, 10)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)
        self.lbl_org = QLabel()
        self.lbl_server = QLabel()
        self.lbl_conn = QLabel()
        self.lbl_service = QLabel()
        for lbl in (self.lbl_server, self.lbl_org):
            lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pairs = [
            ("label.organization", self.lbl_org),
            ("label.status", self.lbl_conn),
            ("label.server", self.lbl_server),
            ("label.service", self.lbl_service),
        ]
        for i, (key, value) in enumerate(pairs):
            row, col = divmod(i, 2)
            grid.addWidget(_muted(self._tr(key) + ":"), row, col * 2)
            grid.addWidget(value, row, col * 2 + 1)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        lay.addWidget(facts)

        self.tabs = QTabWidget()
        # printers
        printers = QWidget()
        pl = QVBoxLayout(printers)
        self.tree = QTreeWidget()
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(False)
        self.tree.setHeaderLabels(
            [
                self._tr("printers.col.name"),
                self._tr("printers.col.connection"),
                self._tr("printers.col.status"),
                self._tr("printers.col.queue"),
                "",
            ]
        )
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in (1, 2, 3, 4):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        pl.addWidget(self.tree, 1)
        self.printers_empty = _muted()
        pl.addWidget(self.printers_empty)
        pl.addStretch(0)
        self.queue_total = _muted()
        pl.addWidget(self.queue_total)
        self.tabs.addTab(printers, self._tr("section.printers"))

        # problems
        self.problems = QListWidget()
        self.problems.setWordWrap(True)
        self.problems.setAlternatingRowColors(True)
        self.tabs.addTab(self.problems, self._tr("section.problems"))

        # power
        power = QWidget()
        pw = QVBoxLayout(power)
        self.power_text = QLabel()
        self.power_text.setWordWrap(True)
        pw.addWidget(self.power_text)
        self.power_hint = _muted()
        pw.addWidget(self.power_hint)
        if sys.platform.startswith("win"):
            power_btn = QPushButton(self._tr("action.power_options"))
            power_btn.clicked.connect(self._open_power_options)
            row = QHBoxLayout()
            row.addWidget(power_btn)
            row.addStretch(1)
            pw.addLayout(row)
        pw.addStretch(1)
        self.tabs.addTab(power, self._tr("section.power"))
        lay.addWidget(self.tabs, 1)
        return page

    def _build_down_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.addStretch(1)
        self.down_service = _muted()
        self.down_service.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.down_service)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_start_big = QPushButton(self._tr("action.start_service"))
        self.btn_start_big.setDefault(True)
        self.btn_start_big.clicked.connect(lambda: self._service("start"))
        row.addWidget(self.btn_start_big)
        self.btn_restart_big = QPushButton(self._tr("action.restart_service"))
        self.btn_restart_big.clicked.connect(lambda: self._service("restart"))
        row.addWidget(self.btn_restart_big)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        return page

    # rendering

    def show_view(self, view: vm.ViewState) -> None:
        self._view = view
        bg, fg = vm.PILL_COLORS[view.tray]
        self.pill.setText(f"  ●  {view.headline}  ")
        self.pill.setStyleSheet(
            f"QLabel#pill {{ background: {bg}; color: {fg}; border-radius: 12px;"
            " padding: 5px 10px; font-weight: 600; }"
        )
        self.detail.setText(view.detail)

        pairing = view.state in (vm.NEEDS_PAIRING,)
        down = not view.agent_reachable
        if pairing:
            self.stack.setCurrentIndex(0)
            self.code.setText(view.pairing_code or self._tr("pairing.waiting_code"))
        elif down:
            self.stack.setCurrentIndex(2)
            hint = self._tr("down.hint_windows" if sys.platform.startswith("win") else "down.hint_linux")
            service = f"{self._tr('label.service')}: {view.service_text}\n\n" if view.service_kind else ""
            self.down_service.setText(service + hint)
            self.btn_start_big.setVisible(view.can_start_service)
            self.btn_restart_big.setVisible(view.can_restart_service)
        else:
            self.stack.setCurrentIndex(1)
            self._render_overview(view)

        self.problems.clear()
        if view.problems:
            for prob in view.problems:
                item = QListWidgetItem(f"{prob.time_text}  {prob.message}")
                if prob.level in ("ERROR", "CRITICAL"):
                    item.setForeground(QBrush(QColor("#a61e14")))
                self.problems.addItem(item)
        else:
            self.problems.addItem(QListWidgetItem(self._tr("problems.none")))
        self.tabs.setTabText(0, f"{self._tr('section.printers')} ({len(view.printers)})")
        self.tabs.setTabText(1, f"{self._tr('section.problems')} ({len(view.problems)})")
        self.power_text.setText(view.power_text)
        self.power_hint.setText(view.power_hint)

        self.btn_web.setEnabled(bool(view.web_app_url))
        self.btn_status.setEnabled(view.agent_reachable)
        has_service = view.service_kind is not None and view.service_state not in (sc.NOT_INSTALLED, sc.UNKNOWN)
        self.btn_service.setVisible(has_service)
        self.act_start.setEnabled(view.can_start_service)
        self.act_stop.setEnabled(view.can_stop_service)
        self.act_restart.setEnabled(view.can_restart_service)
        default = self.act_start if view.can_start_service else self.act_restart
        if self.btn_service.defaultAction() is not default:
            self.btn_service.setDefaultAction(default)
        self.footer.setText(view.footer)

    def _render_overview(self, view: vm.ViewState) -> None:
        self.lbl_org.setText(view.organization or "–")
        self.lbl_server.setText(view.server_host or "–")
        color = "#1f5a24" if view.connected else "#7d1f17"
        self.lbl_conn.setText(f"<span style='color:{color}; font-weight:600'>● {view.connection_text}</span>")
        self.lbl_service.setText(view.service_text or "–")

        self.tree.clear()
        for row in view.printers:
            item = QTreeWidgetItem([row.name, row.connection, f"● {row.status_text}", row.queue_text, ""])
            color = {"ok": "#2f9e44", "error": "#c92a2a"}.get(row.kind, "#868e96")
            item.setForeground(2, QBrush(QColor(color)))
            if row.detail:
                item.setToolTip(0, row.detail)
                item.setText(0, f"{row.name}\n{row.detail}")
            self.tree.addTopLevelItem(item)
            btn = QPushButton(self._tr("action.test_print"))
            btn.setEnabled(row.can_test_print)
            btn.clicked.connect(lambda _=False, pid=row.printer_id: self._worker.test_print(pid))
            self.tree.setItemWidget(item, 4, btn)
        self.tree.setVisible(bool(view.printers))
        self.printers_empty.setVisible(not view.printers)
        self.printers_empty.setText(view.printers_empty_text)
        self.queue_total.setText(view.queue_text)
        self.queue_total.setVisible(bool(view.printers))

    def show_message(self, text: str, is_error: bool) -> None:
        color = "#a61e14" if is_error else "#1f5a24"
        self.message.setStyleSheet(f"color: {color};")
        self.message.setText(text)
        self.message.show()
        self._message_timer.start(8000)

    # actions

    def _service(self, action: str) -> None:
        self.btn_service.setEnabled(False)
        self.show_message(self._tr("busy.service", action=self._tr(f"busy.action.{action}")), False)
        self._worker.service_action(action)

    def service_action_finished(self) -> None:
        self.btn_service.setEnabled(True)

    def _copy_code(self) -> None:
        view = self._view
        if view and view.pairing_code:
            QGuiApplication.clipboard().setText(view.pairing_code.replace(" ", ""))
            self.btn_copy.setText(self._tr("pairing.copied"))
            QTimer.singleShot(2000, lambda: self.btn_copy.setText(self._tr("pairing.copy")))

    def _open_web(self) -> None:
        view = self._view
        if view and view.web_app_url:
            QDesktopServices.openUrl(QUrl(view.web_app_url.rstrip("/") + "/printers"))

    def _open_status_page(self) -> None:
        url = (self._view.status_page_url if self._view else None) or f"http://localhost:{self._port}/"
        QDesktopServices.openUrl(QUrl(url))

    def _open_logs(self) -> None:
        candidates = [self._view.log_dir] if self._view and self._view.log_dir else []
        candidates += default_log_dirs()
        for path in candidates:
            if path and Path(path).is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
                return
        self.show_message(str(candidates[0]) if candidates else "-", True)

    def _open_power_options(self) -> None:
        try:
            subprocess.Popen(["control.exe", "/name", "Microsoft.PowerOptions", "/page", "pageGlobalSettings"])
        except OSError:
            pass

    # window behaviour

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.tray_available:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                if self.tray is not None:
                    self.tray.showMessage(self._tr("app_title"), self._tr("tray.minimized"), logo_icon(), 6000)
        else:
            event.accept()
            self._quit_app()

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()


def default_log_dirs() -> list[str]:
    if sys.platform.startswith("win"):
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return [str(Path(base) / "OpenEOS Printer Agent" / "logs")]
    return [
        "/var/log/openeos-printer",
        str(Path.home() / ".local" / "share" / "openeos-printer" / "logs"),
    ]


# -- application ---------------------------------------------------------------------


class Controller(QObject):
    def __init__(self, app: QApplication, args: argparse.Namespace) -> None:
        super().__init__()
        self.app = app
        self.args = args
        self.t = Translator(args.lang or detect_language())
        self.port = args.port or discover_port()
        self.client = AgentClient(self.port)
        self.service = None if args.no_service else sc.default_controller()
        self.worker = Worker(self.client, self.service, self.t)
        self.window = MainWindow(self.t, self.worker, self.port, self.quit)
        self._last_state: Optional[str] = None
        self._first_snapshot = True
        self._capture_deadline = time.monotonic() + args.capture_timeout

        self.tray: Optional[QSystemTrayIcon] = None
        if QSystemTrayIcon.isSystemTrayAvailable() and not args.no_tray:
            self.tray = self._build_tray()
            self.window.tray = self.tray
            self.window.tray_available = True

        self.worker.snapshot.connect(self.on_snapshot)
        self.worker.message.connect(self.window.show_message)
        self.worker.action_finished.connect(self.window.service_action_finished)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.worker.poll)
        self.timer.start(POLL_INTERVAL_MS)
        self.worker.poll()

    def _build_tray(self) -> QSystemTrayIcon:
        tray = QSystemTrayIcon(logo_icon("idle"), self)
        tray.setToolTip(self.t("app_title"))
        menu = QMenu()
        menu.addAction(self.t("action.show_window"), self.window.bring_to_front)
        menu.addAction(self.t("action.status_page"), self.window._open_status_page)
        menu.addAction(self.t("action.restart_service"), lambda: self.window._service("restart"))
        menu.addSeparator()
        quit_action = menu.addAction(self.t("action.quit"), self.quit)
        quit_action.setToolTip(self.t("action.quit.tip"))
        menu.setToolTipsVisible(True)
        self._tray_menu = menu
        tray.setContextMenu(menu)
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick):
            if self.window.isVisible() and not self.window.isMinimized():
                self.window.hide()
            else:
                self.window.bring_to_front()

    def on_snapshot(self, snap: Snapshot) -> None:
        view = vm.build_view_state(
            snap.data,
            snap.service_state,
            self.t,
            service_kind=self.service.kind if self.service else None,
            port=self.port,
            ui_version=get_version(),
            outdated=snap.outdated,
            platform=sys.platform,
        )
        self.window.show_view(view)
        if self.tray is not None:
            self.tray.setIcon(logo_icon(view.tray))
            self.tray.setToolTip(view.tooltip)
            note = vm.notification_for(self._last_state, view.state)
            if note and not self.window.isVisible():
                self.tray.showMessage(self.t("app_title"), self.t(note), logo_icon(view.tray), 8000)
        if self._first_snapshot:
            self._first_snapshot = False
            if self.args.minimized and self.tray is not None and view.state != vm.NEEDS_PAIRING:
                pass  # stay in the tray
            else:
                self.window.bring_to_front()
        self._last_state = view.state
        if self.args.capture:
            self._maybe_capture(view)

    # screenshots for docs / CI

    def _maybe_capture(self, view: vm.ViewState) -> None:
        wanted = self.args.wait_state
        timed_out = time.monotonic() > self._capture_deadline
        if wanted and view.state != wanted and not timed_out:
            return
        if self.args.capture_done:
            return
        self.args.capture_done = True
        # Let layouts settle and the window manager draw the frame.
        QTimer.singleShot(1500, lambda: self._capture(timed_out and bool(wanted)))

    def _capture(self, timed_out: bool) -> None:
        if self.args.capture_tab is not None:
            self.window.tabs.setCurrentIndex(self.args.capture_tab)
            QApplication.processEvents()
        target = Path(self.args.capture)
        target.parent.mkdir(parents=True, exist_ok=True)
        win = self.window
        if self.args.capture_tray_menu and self.tray is not None:
            menu = self._tray_menu
            geo = self.tray.geometry()
            pos = geo.topLeft() if not geo.isNull() else win.geometry().center()
            menu.popup(pos)
            QTimer.singleShot(800, lambda: self._finish_capture(menu.grab(), target, timed_out))
            return
        screen = win.screen()
        if QGuiApplication.platformName() in ("offscreen", "minimal") or screen is None:
            pixmap = win.grab()
        else:
            fg = win.frameGeometry()
            pixmap = screen.grabWindow(0, fg.x(), fg.y(), fg.width(), fg.height())
            if self.args.capture_desktop:
                screen.grabWindow(0).save(str(target.with_name(target.stem + "-desktop.png")))
        self._finish_capture(pixmap, target, timed_out)

    def _finish_capture(self, pixmap: QPixmap, target: Path, timed_out: bool) -> None:
        pixmap.save(str(target))
        print(f"captured {target} (state={self._last_state}{', TIMEOUT' if timed_out else ''})", flush=True)
        code = 2 if timed_out else 0
        QTimer.singleShot(int(self.args.capture_hold * 1000), lambda: self.app.exit(code))

    def quit(self) -> None:
        self.timer.stop()
        self.worker.shutdown()
        if self.tray is not None:
            self.tray.hide()
        self.app.quit()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openeos-printer-agent-ui", description="OpenEOS Printer Agent - window and tray")
    p.add_argument("--version", action="version", version=f"openeos-printer-agent-ui {get_version()}")
    p.add_argument("--minimized", action="store_true", help="Start in the tray (autostart at login)")
    p.add_argument("--port", type=int, default=None, help="Local port of the agent (default: from config, 8080)")
    p.add_argument("--lang", choices=["de", "en"], default=None, help="UI language (default: system)")
    p.add_argument("--no-tray", action="store_true", help="No tray icon; closing the window quits")
    p.add_argument("--no-service", action="store_true", help="Do not query/control the service manager")
    p.add_argument("--capture", default=None, help=argparse.SUPPRESS)
    p.add_argument("--wait-state", default=None, help=argparse.SUPPRESS)
    p.add_argument("--capture-timeout", type=float, default=60.0, help=argparse.SUPPRESS)
    p.add_argument("--capture-desktop", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--capture-tray-menu", action="store_true", help=argparse.SUPPRESS)
    # keep the window/menu on screen this long after the capture (for an
    # external screenshot of the whole desktop), and pick a tab first
    p.add_argument("--capture-hold", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--capture-tab", type=int, default=None, help=argparse.SUPPRESS)
    return p


def _hand_over_to_running_instance(key: str) -> bool:
    """Second start (autostart + double-click): bring the first window up."""
    sock = QLocalSocket()
    sock.connectToServer(key)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    args.capture_done = False
    app = QApplication(sys.argv[:1])
    app.setApplicationName("OpenEOS Printer Agent")
    app.setApplicationDisplayName(Translator(args.lang or detect_language())("app_title"))
    app.setDesktopFileName("openeos-printer-agent")
    app.setWindowIcon(logo_icon())
    app.setQuitOnLastWindowClosed(False)

    key = f"{SINGLE_INSTANCE_KEY}-{getpass.getuser()}"
    server: Optional[QLocalServer] = None
    if not args.capture:
        if _hand_over_to_running_instance(key):
            return 0
        QLocalServer.removeServer(key)
        server = QLocalServer()
        server.listen(key)

    controller = Controller(app, args)
    if server is not None:

        def _on_connection() -> None:
            assert server is not None
            conn = server.nextPendingConnection()
            if conn is not None:
                conn.waitForReadyRead(200)
                conn.readAll()
                conn.close()
            controller.window.bring_to_front()

        server.newConnection.connect(_on_connection)

    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
