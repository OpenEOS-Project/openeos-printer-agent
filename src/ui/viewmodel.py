"""Turns the agent's ``/api/ui/state`` (plus the service state) into what
the window and the tray icon show. Pure Python - no Qt - so every mapping
is unit-tested without a display.
"""

import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlsplit

from .. import service_control as sc
from .i18n import Translator

# Overall states, most important first in the docs: what is the user's
# next step?
NEEDS_PAIRING = "needs_pairing"
WAITING_FOR_ORG = "waiting_for_org"
BLOCKED = "blocked"
CONNECTED = "connected"
NO_PRINTERS = "no_printers"
PRINTER_PROBLEM = "printer_problem"
DISCONNECTED = "disconnected"
STARTING = "starting"
SERVICE_STOPPED = "service_stopped"
NOT_INSTALLED = "not_installed"
UNREACHABLE = "unreachable"
OUTDATED = "outdated"
AGENT_DOWN = "agent_down"

# Tray icon flavours
TRAY_OK = "ok"  # green
TRAY_WARN = "warn"  # orange: printer problem
TRAY_PAIRING = "pairing"  # blue: user action needed in the web app
TRAY_ERROR = "error"  # red: nothing is printed
TRAY_IDLE = "idle"  # grey: starting

TRAY_BY_STATE = {
    CONNECTED: TRAY_OK,
    NO_PRINTERS: TRAY_OK,
    PRINTER_PROBLEM: TRAY_WARN,
    NEEDS_PAIRING: TRAY_PAIRING,
    WAITING_FOR_ORG: TRAY_PAIRING,
    STARTING: TRAY_IDLE,
    BLOCKED: TRAY_ERROR,
    DISCONNECTED: TRAY_ERROR,
    SERVICE_STOPPED: TRAY_ERROR,
    NOT_INSTALLED: TRAY_ERROR,
    UNREACHABLE: TRAY_ERROR,
    OUTDATED: TRAY_ERROR,
    AGENT_DOWN: TRAY_ERROR,
}

# Status pill colours (background, text) per tray flavour.
PILL_COLORS = {
    TRAY_OK: ("#d8f3d3", "#1f5a24"),
    TRAY_WARN: ("#fdebc8", "#7a4b00"),
    TRAY_PAIRING: ("#dbe7fb", "#1d3f7a"),
    TRAY_ERROR: ("#f8d7d3", "#7d1f17"),
    TRAY_IDLE: ("#ece7d9", "#4b5049"),
}

PRINTER_KIND = {"online": "ok", "busy": "ok", "offline": "error", "error": "error"}


@dataclass
class PrinterRow:
    printer_id: str
    name: str
    connection: str
    status: str  # online / offline / error / busy / unknown
    status_text: str
    kind: str  # ok / error / unknown
    queue_text: str
    detail: str  # last error or last job
    can_test_print: bool


@dataclass
class ProblemRow:
    time_text: str
    level: str
    message: str


@dataclass
class ViewState:
    state: str
    tray: str
    headline: str
    detail: str
    agent_reachable: bool
    pairing_code: Optional[str] = None
    organization: Optional[str] = None
    server_host: str = ""
    connection_text: str = ""
    connected: bool = False
    printers: list[PrinterRow] = field(default_factory=list)
    printers_empty_text: str = ""
    queue_text: str = ""
    problems: list[ProblemRow] = field(default_factory=list)
    power_text: str = ""
    power_hint: str = ""
    service_kind: Optional[str] = None
    service_state: str = sc.UNKNOWN
    service_text: str = ""
    can_start_service: bool = False
    can_stop_service: bool = False
    can_restart_service: bool = False
    status_page_url: Optional[str] = None
    web_app_url: Optional[str] = None
    log_dir: Optional[str] = None
    footer: str = ""
    tooltip: str = ""


def format_code(code: Optional[str]) -> Optional[str]:
    """ "12345678" -> "1234 5678" (easier to read aloud); other lengths unchanged."""
    if not code:
        return None
    code = str(code).strip()
    if len(code) == 8 and code.isdigit():
        return f"{code[:4]} {code[4:]}"
    return code


def _clock(ts: Optional[float]) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M", time.localtime(ts))


def _problem_time(ts: float, now: float) -> str:
    if now - ts > 20 * 3600:
        return time.strftime("%d.%m. %H:%M", time.localtime(ts))
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _host(url: str) -> str:
    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


def overall_state(data: Optional[dict[str, Any]], service_state: str, outdated: bool = False) -> str:
    if data is None:
        if outdated:
            return OUTDATED
        return {
            sc.STOPPED: SERVICE_STOPPED,
            sc.STOPPING: SERVICE_STOPPED,
            sc.STARTING: STARTING,
            sc.NOT_INSTALLED: NOT_INSTALLED,
            sc.RUNNING: UNREACHABLE,
        }.get(service_state, AGENT_DOWN)

    pairing = data.get("pairing") or {}
    status = pairing.get("status")
    if status == "pending":
        return NEEDS_PAIRING
    if status == "blocked":
        return BLOCKED
    if status == "waiting_for_org":
        return WAITING_FOR_ORG
    if status != "verified":
        # "starting"/"unknown": registering, or the server is not reachable
        # yet (a pending registration keeps retrying).
        return STARTING
    conn = data.get("connection") or {}
    if not conn.get("ready"):
        return STARTING
    if not conn.get("connected"):
        return DISCONNECTED
    printers = data.get("printers") or []
    if not printers:
        return NO_PRINTERS
    if any(PRINTER_KIND.get(p.get("status", ""), "error") == "error" for p in printers):
        return PRINTER_PROBLEM
    return CONNECTED


def build_view_state(
    data: Optional[dict[str, Any]],
    service_state: str,
    tr: Translator,
    *,
    service_kind: Optional[str] = None,
    port: int = 8080,
    ui_version: str = "",
    outdated: bool = False,
    platform: str = "",
    now: Optional[float] = None,
) -> ViewState:
    now = time.time() if now is None else now
    state = overall_state(data, service_state, outdated)
    tray = TRAY_BY_STATE[state]

    printers_raw: list[dict[str, Any]] = (data or {}).get("printers") or []
    bad = [p for p in printers_raw if PRINTER_KIND.get(p.get("status", ""), "error") == "error"]
    org = ((data or {}).get("pairing") or {}).get("organization")

    headline = tr(f"state.{state}")
    if state == CONNECTED:
        detail = tr("state.connected.detail", org=org) if org else tr("state.connected.detail_no_org")
    elif state == PRINTER_PROBLEM:
        detail = tr("state.printer_problem.detail", count=len(bad), total=len(printers_raw))
    elif state in (UNREACHABLE, AGENT_DOWN):
        detail = tr(f"state.{state}.detail", port=port)
    else:
        detail = tr(f"state.{state}.detail")

    installed = service_kind is not None and service_state not in (sc.NOT_INSTALLED, sc.UNKNOWN)
    vs = ViewState(
        state=state,
        tray=tray,
        headline=headline,
        detail=detail,
        agent_reachable=data is not None,
        service_kind=service_kind,
        service_state=service_state,
        service_text=tr(f"service.{service_state}") if service_kind else "",
        can_start_service=installed and service_state == sc.STOPPED,
        can_stop_service=installed and service_state == sc.RUNNING,
        can_restart_service=installed and service_state == sc.RUNNING,
        status_page_url=f"http://localhost:{port}/",
        tooltip=tr("tray.tooltip", state=headline),
        footer=tr("footer_no_agent", ui_version=ui_version),
    )
    if data is None:
        return vs

    agent = data.get("agent") or {}
    conn = data.get("connection") or {}
    pairing = data.get("pairing") or {}
    vs.pairing_code = format_code(pairing.get("code")) if state == NEEDS_PAIRING else None
    vs.organization = org
    vs.server_host = _host(str(conn.get("serverUrl") or ""))
    vs.connected = bool(conn.get("connected"))
    vs.connection_text = tr("conn.connected") if vs.connected else tr("conn.disconnected")
    vs.status_page_url = data.get("statusPageUrl") or vs.status_page_url
    vs.web_app_url = conn.get("webAppUrl")
    vs.log_dir = (data.get("paths") or {}).get("logDir")
    vs.footer = tr(
        "footer",
        agent_version=agent.get("version", "?"),
        ui_version=ui_version,
        hostname=agent.get("hostname", "?"),
    )

    for p in printers_raw:
        status = str(p.get("status") or "unknown")
        q = p.get("queue") or {}
        last = p.get("lastJob") or {}
        if p.get("lastError") and PRINTER_KIND.get(status) == "error":
            detail_text = str(p["lastError"])
        elif last.get("status") == "completed":
            detail_text = tr("printers.last_ok", time=_clock(last.get("finishedAt")))
        elif last.get("status") == "failed":
            detail_text = tr(
                "printers.last_failed", time=_clock(last.get("finishedAt")), error=last.get("error") or "?"
            )
        else:
            detail_text = ""
        vs.printers.append(
            PrinterRow(
                printer_id=str(p.get("printerId")),
                name=str(p.get("name") or p.get("printerId")),
                connection=tr.get(f"conn.{p.get('connectionType')}", str(p.get("connectionType") or "")),
                status=status,
                status_text=tr(f"printer.{status}") if status in PRINTER_KIND else tr("printer.unknown"),
                kind=PRINTER_KIND.get(status, "unknown"),
                queue_text=tr(
                    "printers.queue",
                    pending=q.get("pending", 0),
                    completed=q.get("completed", 0),
                    failed=q.get("failed", 0),
                ),
                detail=detail_text,
                can_test_print=True,
            )
        )
    found = ((data.get("discovery") or {}).get("found")) or 0
    vs.printers_empty_text = tr("printers.empty", found=found)
    totals = data.get("queue") or {}
    vs.queue_text = tr(
        "queue.total",
        completed=totals.get("completed", 0),
        failed=totals.get("failed", 0),
        pending=totals.get("pending", 0),
    )

    for entry in data.get("problems") or []:
        vs.problems.append(
            ProblemRow(
                time_text=_problem_time(float(entry.get("time") or now), now),
                level=str(entry.get("level") or ""),
                message=str(entry.get("message") or ""),
            )
        )

    power = data.get("power") or {}
    if power.get("error"):
        vs.power_text = tr("power.failed", error=power["error"])
    elif power.get("active"):
        vs.power_text = tr("power.active")
    elif power.get("keepAwake"):
        vs.power_text = tr("power.waiting")
    else:
        vs.power_text = tr("power.off")
    plat = platform or str(agent.get("platform") or "")
    vs.power_hint = tr("power.hint") if plat.startswith("win") else tr("power.hint_linux")
    return vs


# Transitions worth a tray notification: (from-group, to-state) -> text key
def notification_for(previous: Optional[str], current: str) -> Optional[str]:
    if previous is None or previous == current:
        return None
    good = (CONNECTED, NO_PRINTERS)
    if current == NEEDS_PAIRING:
        return "notify.needs_pairing"
    if current == DISCONNECTED and previous in good + (PRINTER_PROBLEM,):
        return "notify.disconnected"
    if current == PRINTER_PROBLEM and previous in good:
        return "notify.printer_problem"
    if current in good and previous in (DISCONNECTED, PRINTER_PROBLEM):
        return "notify.reconnected"
    return None
