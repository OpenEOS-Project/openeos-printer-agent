"""German UI texts (informal "du", like the web app) with English fallback.

German is used when the system language is German or ``OPENEOS_UI_LANG=de``;
every other language gets English.
"""

import locale
import os
import sys
from typing import Any

DE: dict[str, str] = {
    "app_title": "OpenEOS Drucker-Agent",
    # overall states: headline / detail
    "state.connected": "Verbunden",
    "state.connected.detail": "Mit {org} verbunden. Bons werden gedruckt.",
    "state.connected.detail_no_org": "Mit OpenEOS verbunden. Bons werden gedruckt.",
    "state.printer_problem": "Drucker-Problem",
    "state.printer_problem.detail": "{count} von {total} Druckern nicht erreichbar. Papier, Kabel und Strom prüfen.",
    "state.no_printers": "Verbunden – noch keine Drucker",
    "state.no_printers.detail": "Übernimm in OpenEOS unter Drucker einen der gefundenen Drucker.",
    "state.needs_pairing": "Noch nicht verbunden",
    "state.needs_pairing.detail": "Gib den Code unten in OpenEOS ein.",
    "state.waiting_for_org": "Wartet auf Zuordnung",
    "state.waiting_for_org.detail": "Der Agent ist gekoppelt, aber noch keiner Organisation zugeordnet.",
    "state.blocked": "Gesperrt",
    "state.blocked.detail": (
        "Dieser Agent ist in OpenEOS gesperrt. Heb die Sperre unter Drucker → Drucker-Agent auf – "
        "er verbindet sich dann von selbst wieder."
    ),
    "state.disconnected": "Keine Verbindung",
    "state.disconnected.detail": (
        "Keine Verbindung zum OpenEOS-Server. Der Agent versucht es weiter – prüf die Internetverbindung."
    ),
    "state.starting": "Startet …",
    "state.starting.detail": "Der Agent verbindet sich mit OpenEOS.",
    "state.service_stopped": "Agent läuft nicht",
    "state.service_stopped.detail": "Der Hintergrunddienst ist gestoppt – es wird nichts gedruckt.",
    "state.not_installed": "Agent läuft nicht",
    "state.not_installed.detail": (
        "Der Hintergrunddienst ist nicht eingerichtet. Installiere den Drucker-Agent neu oder starte ihn von Hand."
    ),
    "state.unreachable": "Agent antwortet nicht",
    "state.unreachable.detail": (
        "Der Dienst läuft, antwortet aber nicht auf Port {port}. Starte ihn neu; hilft das nicht, schau ins Protokoll."
    ),
    "state.outdated": "Agent zu alt",
    "state.outdated.detail": "Der laufende Drucker-Agent ist älter als dieses Fenster. Installiere die aktuelle Version.",
    "state.agent_down": "Agent läuft nicht",
    "state.agent_down.detail": "Der Drucker-Agent ist auf diesem Rechner nicht erreichbar (Port {port}).",
    # pairing
    "pairing.instructions": "Gib diesen Code in OpenEOS unter Drucker → Drucker-Agent verbinden ein:",
    "pairing.hint": (
        "Der Code gilt 15 Minuten, danach erscheint hier von selbst ein neuer. "
        "Sobald du ihn eingegeben hast, wechselt dieses Fenster zur Übersicht."
    ),
    "pairing.copy": "Code kopieren",
    "pairing.copied": "Kopiert",
    "pairing.waiting_code": "Code wird geholt …",
    # sections
    "section.connection": "Verbindung",
    "section.printers": "Drucker",
    "section.problems": "Fehler und Warnungen",
    "section.power": "Ruhezustand",
    "label.server": "Server",
    "label.organization": "Organisation",
    "label.status": "Status",
    "label.service": "Hintergrunddienst",
    "label.queue": "Warteschlange",
    "label.computer": "Rechner",
    "conn.connected": "Verbunden",
    "conn.disconnected": "Getrennt",
    "printers.empty": (
        "Noch keine Drucker übernommen. Gefundene Drucker übernimmst du in OpenEOS unter Drucker ({found} gefunden)."
    ),
    "printers.col.name": "Drucker",
    "printers.col.connection": "Anschluss",
    "printers.col.status": "Status",
    "printers.col.queue": "Aufträge",
    "printers.queue": "{pending} offen · {completed} gedruckt · {failed} fehlgeschlagen",
    "printers.last_ok": "Zuletzt gedruckt {time}",
    "printers.last_failed": "Fehlgeschlagen {time}: {error}",
    "queue.total": "Seit dem Start: {completed} gedruckt, {failed} fehlgeschlagen, {pending} offen",
    "printer.online": "Online",
    "printer.offline": "Offline",
    "printer.error": "Fehler",
    "printer.busy": "Druckt",
    "printer.unknown": "Unbekannt",
    "conn.usb": "USB",
    "conn.network": "Netzwerk",
    "conn.windows": "Windows-Drucker",
    "problems.none": "Keine Fehler in den letzten 24 Stunden.",
    "down.hint_windows": "Ohne laufenden Agent wird nichts gedruckt. Starte den Dienst hier oder installiere den Drucker-Agent neu.",
    "down.hint_linux": "Ohne laufenden Agent wird nichts gedruckt. Starte ihn mit: sudo systemctl start openeos-printer",
    "service.running": "läuft",
    "service.stopped": "gestoppt",
    "service.starting": "startet",
    "service.stopping": "stoppt",
    "service.not_installed": "nicht eingerichtet",
    "service.unknown": "unbekannt",
    # power
    "power.active": "Solange der Agent verbunden ist, geht der Rechner nicht von selbst in den Ruhezustand.",
    "power.waiting": "Sobald der Agent verbunden ist, hält er den Rechner wach.",
    "power.off": "Der Rechner darf in den Ruhezustand gehen – dann wird nicht gedruckt.",
    "power.failed": "Wachhalten hat nicht geklappt ({error}). Stell den Ruhezustand in den Energieoptionen ab.",
    "power.hint": (
        "Beim Zuklappen des Laptops, mit dem Ein-/Aus-Knopf oder über „Energie sparen“ schläft er trotzdem ein. "
        "Stell in den Energieoptionen „Beim Zuklappen: Nichts unternehmen“ ein und lass den Laptop am Netzteil."
    ),
    "power.hint_linux": (
        "Zuklappen schickt den Laptop trotzdem schlafen. Stell in den Energieeinstellungen ein, "
        "dass beim Zuklappen nichts passiert, und lass ihn am Netzteil."
    ),
    # actions
    "action.open_web": "OpenEOS öffnen",
    "action.status_page": "Statusseite",
    "action.logs": "Protokolle",
    "action.restart_service": "Dienst neu starten",
    "action.start_service": "Dienst starten",
    "action.stop_service": "Dienst stoppen",
    "action.test_print": "Testdruck",
    "action.test_print_sent": "Gesendet",
    "action.power_options": "Energieoptionen",
    "action.show_window": "Fenster öffnen",
    "action.quit": "Beenden",
    "action.quit.tip": "Schließt nur dieses Fenster-Programm. Gedruckt wird weiter im Hintergrund.",
    "busy.service": "Dienst wird {action} …",
    "busy.action.start": "gestartet",
    "busy.action.stop": "gestoppt",
    "busy.action.restart": "neu gestartet",
    "error.service": "Der Dienst ließ sich nicht steuern: {error}",
    "error.service_denied": "Keine Berechtigung. Bestätige die Abfrage von Windows oder frag jemanden mit Admin-Rechten.",
    "error.test_print": "Testdruck nicht möglich: {error}",
    "tray.minimized": "Der Drucker-Agent läuft im Hintergrund weiter. Du findest ihn im Infobereich der Taskleiste.",
    "tray.tooltip": "OpenEOS Drucker-Agent – {state}",
    "notify.disconnected": "Keine Verbindung zu OpenEOS. Es wird gerade nicht gedruckt.",
    "notify.printer_problem": "Ein Drucker ist nicht erreichbar.",
    "notify.needs_pairing": "Der Drucker-Agent braucht einen neuen Code. Öffne das Fenster.",
    "notify.reconnected": "Wieder verbunden.",
    "footer": "Agent {agent_version} · Fenster {ui_version} · Rechner {hostname}",
    "footer_no_agent": "Fenster {ui_version}",
    "time.just_now": "gerade eben",
}

EN: dict[str, str] = {
    "app_title": "OpenEOS Printer Agent",
    "state.connected": "Connected",
    "state.connected.detail": "Connected to {org}. Receipts are printed.",
    "state.connected.detail_no_org": "Connected to OpenEOS. Receipts are printed.",
    "state.printer_problem": "Printer problem",
    "state.printer_problem.detail": "{count} of {total} printers not reachable. Check paper, cable and power.",
    "state.no_printers": "Connected – no printers yet",
    "state.no_printers.detail": "Adopt one of the found printers in OpenEOS under Printers.",
    "state.needs_pairing": "Not connected yet",
    "state.needs_pairing.detail": "Enter the code below in OpenEOS.",
    "state.waiting_for_org": "Waiting for assignment",
    "state.waiting_for_org.detail": "The agent is paired but not assigned to an organization yet.",
    "state.blocked": "Blocked",
    "state.blocked.detail": (
        "This agent is blocked in OpenEOS. Unblock it under Printers → Printer agent – it reconnects by itself."
    ),
    "state.disconnected": "No connection",
    "state.disconnected.detail": (
        "No connection to the OpenEOS server. The agent keeps trying – check the internet connection."
    ),
    "state.starting": "Starting …",
    "state.starting.detail": "The agent is connecting to OpenEOS.",
    "state.service_stopped": "Agent not running",
    "state.service_stopped.detail": "The background service is stopped – nothing is printed.",
    "state.not_installed": "Agent not running",
    "state.not_installed.detail": (
        "The background service is not set up. Reinstall the printer agent or start it by hand."
    ),
    "state.unreachable": "Agent not responding",
    "state.unreachable.detail": (
        "The service is running but does not answer on port {port}. Restart it; if that does not help, check the log."
    ),
    "state.outdated": "Agent outdated",
    "state.outdated.detail": "The running printer agent is older than this window. Install the current version.",
    "state.agent_down": "Agent not running",
    "state.agent_down.detail": "The printer agent is not reachable on this computer (port {port}).",
    "pairing.instructions": "Enter this code in OpenEOS under Printers → Connect printer agent:",
    "pairing.hint": (
        "The code is valid for 15 minutes; a new one appears here by itself. "
        "Once you entered it, this window switches to the overview."
    ),
    "pairing.copy": "Copy code",
    "pairing.copied": "Copied",
    "pairing.waiting_code": "Fetching code …",
    "section.connection": "Connection",
    "section.printers": "Printers",
    "section.problems": "Errors and warnings",
    "section.power": "Sleep",
    "label.server": "Server",
    "label.organization": "Organization",
    "label.status": "Status",
    "label.service": "Background service",
    "label.queue": "Queue",
    "label.computer": "Computer",
    "conn.connected": "Connected",
    "conn.disconnected": "Disconnected",
    "printers.empty": "No printers adopted yet. Adopt found printers in OpenEOS under Printers ({found} found).",
    "printers.col.name": "Printer",
    "printers.col.connection": "Connection",
    "printers.col.status": "Status",
    "printers.col.queue": "Jobs",
    "printers.queue": "{pending} pending · {completed} printed · {failed} failed",
    "printers.last_ok": "Last printed {time}",
    "printers.last_failed": "Failed {time}: {error}",
    "queue.total": "Since start: {completed} printed, {failed} failed, {pending} pending",
    "printer.online": "Online",
    "printer.offline": "Offline",
    "printer.error": "Error",
    "printer.busy": "Printing",
    "printer.unknown": "Unknown",
    "conn.usb": "USB",
    "conn.network": "Network",
    "conn.windows": "Windows printer",
    "problems.none": "No errors in the last 24 hours.",
    "down.hint_windows": "Nothing is printed without a running agent. Start the service here or reinstall the printer agent.",
    "down.hint_linux": "Nothing is printed without a running agent. Start it with: sudo systemctl start openeos-printer",
    "service.running": "running",
    "service.stopped": "stopped",
    "service.starting": "starting",
    "service.stopping": "stopping",
    "service.not_installed": "not set up",
    "service.unknown": "unknown",
    "power.active": "While the agent is connected, the computer does not go to sleep by itself.",
    "power.waiting": "As soon as the agent is connected, it keeps the computer awake.",
    "power.off": "The computer may go to sleep – nothing is printed then.",
    "power.failed": "Keeping awake did not work ({error}). Turn off sleep in the power options.",
    "power.hint": (
        "Closing the lid, the power button or Start → Sleep still put it to sleep. "
        'Set "When I close the lid: Do nothing" in the power options and keep the laptop plugged in.'
    ),
    "power.hint_linux": (
        "Closing the lid still suspends the laptop. Set the lid action to do nothing in the power "
        "settings and keep it plugged in."
    ),
    "action.open_web": "Open OpenEOS",
    "action.status_page": "Status page",
    "action.logs": "Logs",
    "action.restart_service": "Restart service",
    "action.start_service": "Start service",
    "action.stop_service": "Stop service",
    "action.test_print": "Test print",
    "action.test_print_sent": "Sent",
    "action.power_options": "Power options",
    "action.show_window": "Open window",
    "action.quit": "Quit",
    "action.quit.tip": "Closes only this window program. Printing continues in the background.",
    "busy.service": "Service is being {action} …",
    "busy.action.start": "started",
    "busy.action.stop": "stopped",
    "busy.action.restart": "restarted",
    "error.service": "Could not control the service: {error}",
    "error.service_denied": "Permission denied. Confirm the Windows prompt or ask someone with admin rights.",
    "error.test_print": "Test print not possible: {error}",
    "tray.minimized": "The printer agent keeps running in the background. You find it in the notification area.",
    "tray.tooltip": "OpenEOS Printer Agent – {state}",
    "notify.disconnected": "No connection to OpenEOS. Nothing is printed right now.",
    "notify.printer_problem": "A printer is not reachable.",
    "notify.needs_pairing": "The printer agent needs a new code. Open the window.",
    "notify.reconnected": "Connected again.",
    "footer": "Agent {agent_version} · Window {ui_version} · Computer {hostname}",
    "footer_no_agent": "Window {ui_version}",
    "time.just_now": "just now",
}

LANGUAGES = {"de": DE, "en": EN}


def _windows_ui_language() -> str:
    try:
        import ctypes

        langid = int(ctypes.windll.kernel32.GetUserDefaultUILanguage())  # type: ignore[attr-defined]
    except Exception:
        return ""
    return "de" if langid & 0x3FF == 0x07 else "en"


def detect_language(env: "os._Environ[str] | dict[str, str] | None" = None) -> str:
    environ = os.environ if env is None else env
    forced = environ.get("OPENEOS_UI_LANG", "").lower()[:2]
    if forced in LANGUAGES:
        return forced
    if env is None and sys.platform.startswith("win"):
        win = _windows_ui_language()
        if win:
            return win
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = environ.get(var, "")
        if value:
            return "de" if value.lower().startswith("de") else "en"
    if env is None:
        try:
            loc = locale.getlocale()[0] or ""
        except ValueError:
            loc = ""
        if loc.lower().startswith("de") or loc.lower().startswith("german"):
            return "de"
    return "en"


class Translator:
    def __init__(self, lang: str) -> None:
        self.lang = lang if lang in LANGUAGES else "en"
        self._table = LANGUAGES[self.lang]

    def __call__(self, key: str, **kwargs: Any) -> str:
        text = self._table.get(key) or EN.get(key) or key
        return text.format(**kwargs) if kwargs else text

    def get(self, key: str, default: str) -> str:
        """Translation if the key exists, else ``default`` (unknown enum values)."""
        return self._table.get(key) or EN.get(key) or default
