"""Classification of discovered printers: receipt, label, office or unknown.

Only ESC/POS receipt printers are usable through the agent, but discovery
also turns up office lasers, inkjets and label printers (mDNS announces
them all, the spooler lists every queue, port 9100 is open on most of
them). Each ``DiscoveredPrinter`` therefore carries three detail keys that
the web app uses to hide the noise by default:

- ``escpos``: ``yes`` when the device answered the ESC/POS model query
  (``GS I``), ``no`` when it was asked and stayed silent, ``unknown`` when
  it was never asked (USB, spooler, identified office printers).
- ``kind``: ``receipt`` | ``label`` | ``office`` | ``unknown``.
- ``kindReason``: one short line explaining the verdict.

The rules are deliberately simple keyword and vendor-ID checks; the
network probe (see ``portscan.query_model``) is the only active test.
"""

import re
from dataclasses import dataclass
from typing import Literal, Optional

from .types import KNOWN_USB_VENDORS, DiscoveredPrinter

Escpos = Literal["yes", "no", "unknown"]
Kind = Literal["receipt", "label", "office", "unknown"]


@dataclass(frozen=True)
class Classification:
    escpos: Escpos
    kind: Kind
    reason: str

    def apply(self, printer: DiscoveredPrinter) -> None:
        printer.details["escpos"] = self.escpos
        printer.details["kind"] = self.kind
        printer.details["kindReason"] = self.reason


def _compile(patterns: list[tuple[str, str]]) -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(p, re.IGNORECASE), label) for p, label in patterns]


# Receipt printers by model prefix or driver wording. ``pos`` only when it
# is not the start of a longer word ("PostScript" drivers of office lasers).
RECEIPT_PATTERNS = _compile(
    [
        (r"\btm-", "Epson TM-Serie"),
        (r"\btsp\d", "Star TSP-Serie"),
        (r"\bmc-print|\bmpop\b|\bsm-[lst]\d", "Star mC/mPOP/SM-Serie"),
        (r"\bsrp-|\bspp-", "Bixolon SRP/SPP-Serie"),
        (r"\bct-[se]\d", "Citizen CT-Serie"),
        (r"\bcustom\b|\bkube\b", "Custom"),
        (r"\brj-\d|\btd-\d", "Brother RJ/TD-Serie"),
        (r"\blk-t", "Sewoo LK-Serie"),
        (r"\bposiflex\b", "Posiflex"),
        (r"\bpos(?![a-z])", "POS"),
        (r"\breceipt\b|\bbondrucker\b|\bbon\b", "Receipt"),
        (r"\bthermal\b|\b(58|80)\s*mm\b", "Thermodrucker"),
        (r"generic\s*/\s*text\s*only", "Generic / Text Only"),
        (r"\bxprinter\b|\brongta\b|\bhprt\b|\bsunmi\b|\bmetapace\b", "Bondrucker-Marke"),
    ]
)

LABEL_PATTERNS = _compile(
    [
        (r"\bql-\d|\bdk-\d|\bpt-[a-z]?\d|p-touch", "Brother QL/DK/P-touch"),
        (r"\bzebra\b|\bzdesigner\b", "Zebra"),
        (r"\bdymo\b|labelwriter", "Dymo"),
        (r"\btsc\b|\bgodex\b", "TSC/Godex"),
        (r"\blabel", "Label"),
    ]
)

# Office vendors by name, plus the model families that give them away even
# when the vendor string is missing (Epson sells both TM receipt printers
# and WorkForce inkjets, so Epson itself is not an office vendor here).
OFFICE_PATTERNS = _compile(
    [
        (r"\bbrother\b", "Brother"),
        (r"\bhp\b|hewlett[- ]?packard", "HP"),
        (r"\bcanon\b", "Canon"),
        (r"\bkyocera\b", "Kyocera"),
        (r"\blexmark\b", "Lexmark"),
        (r"\bricoh\b", "Ricoh"),
        (r"\bxerox\b", "Xerox"),
        (r"\bkonica\b", "Konica Minolta"),
        (r"\boki\b|\bokidata\b", "OKI"),
        (r"\bsamsung\b", "Samsung"),
        (r"laserjet|officejet|deskjet|\benvy\b|pagewide", "HP"),
        (r"\bpixma\b|imageclass|imagerunner|\bmaxify\b", "Canon"),
        (r"\becosys\b|taskalfa", "Kyocera"),
        (r"\bhl-[a-z]?\d|\bmfc-[a-z]?\d|\bdcp-[a-z]?\d", "Brother"),
        (r"\bworkforce\b|\becotank\b|\bwf-\d|\bet-\d|\bexpression\b", "Epson (Büro)"),
        (r"\blaser\b", "Laser"),
    ]
)

# Default hostnames office printers announce (Brother "BRN"/"BRW" + MAC,
# HP "NPI" + MAC).
OFFICE_HOSTNAME_PATTERNS = _compile(
    [
        (r"^br[nw][0-9a-f]{6,}", "Brother"),
        (r"^npi[0-9a-f]{6}", "HP"),
    ]
)


def _first_match(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> Optional[str]:
    for pattern, label in patterns:
        if pattern.search(text):
            return label
    return None


def _join(*parts: Optional[str]) -> str:
    return " ".join(p for p in parts if p)


def match_office(*texts: Optional[str], hostname: Optional[str] = None) -> Optional[str]:
    """Office vendor label when any text (or the hostname) points at one."""
    label = _first_match(_join(*texts), OFFICE_PATTERNS)
    if label is None and hostname:
        label = _first_match(hostname.strip(), OFFICE_HOSTNAME_PATTERNS)
    return label


def match_label(*texts: Optional[str]) -> Optional[str]:
    return _first_match(_join(*texts), LABEL_PATTERNS)


def match_receipt(*texts: Optional[str]) -> Optional[str]:
    return _first_match(_join(*texts), RECEIPT_PATTERNS)


# -- USB ----------------------------------------------------------------------


def classify_usb(vendor_id: int, manufacturer: Optional[str], product: Optional[str]) -> Classification:
    """Known POS vendor ID = receipt printer; anything else that enumerated
    as a printer (interface class 7) stays unknown."""
    vendor = KNOWN_USB_VENDORS.get(vendor_id)
    if vendor:
        return Classification("yes", "receipt", f"USB-Hersteller-ID {vendor} ({vendor_id:04x})")
    strings = _join(manufacturer, product)
    label = match_receipt(strings)
    if label:
        return Classification("yes", "receipt", f"USB-Produktname {label}")
    label = match_label(strings)
    if label:
        return Classification("unknown", "label", f"USB-Produktname {label}")
    label = match_office(strings)
    if label:
        return Classification("unknown", "office", f"USB-Hersteller {label}")
    return Classification("unknown", "unknown", f"USB-Druckerklasse, Hersteller {vendor_id:04x} unbekannt")


# -- Windows spooler ------------------------------------------------------------


def classify_spooler(name: str, driver: Optional[str], comment: Optional[str] = None) -> Classification:
    """Driver and queue names carry the model on Windows."""
    text = _join(name, driver, comment)
    label = match_receipt(text)
    if label:
        source = "Treiber" if driver and match_receipt(driver) else "Druckername"
        return Classification("unknown", "receipt", f"{source} {label}")
    label = match_label(text)
    if label:
        return Classification("unknown", "label", f"Treiber {label}")
    label = match_office(text)
    if label:
        return Classification("unknown", "office", f"Treiber {label}")
    return Classification("unknown", "unknown", "Treiber ohne bekannte Bondrucker-Merkmale")


# -- Network --------------------------------------------------------------------


def classify_network_without_probe(printer: DiscoveredPrinter) -> Optional[Classification]:
    """Verdict from mDNS/SNMP strings alone, or None when the device has to
    be asked. Identified office printers are never probed: raw bytes on
    port 9100 make a laser eject a page with the query text on it."""
    d = printer.details
    model = str(d.get("model") or "")
    manufacturer = str(d.get("manufacturer") or "")
    hostname = str(d.get("hostname") or "")
    text = _join(manufacturer, model)
    if match_receipt(text) or match_label(text):
        # Looks like a POS device, but only the probe can tell an ESC/POS
        # label printer (Epson TM-L, Bixolon SLP) from a raster one
        # (Brother QL, Zebra ZPL) that the agent cannot drive.
        return None
    label = match_office(text, hostname=hostname)
    if label:
        what = f"Modell {model}" if model else f"Hostname {hostname}" if hostname else "Hersteller"
        return Classification("unknown", "office", f"{what} ({label}, nicht abgefragt)")
    return None


def classify_network_probe(
    printer: DiscoveredPrinter, connected: bool, maker: Optional[str], model: Optional[str], firmware: Optional[str]
) -> Classification:
    """Verdict after the ESC/POS model query."""
    if maker or model or firmware:
        answer = _join(maker, model) or f"Firmware {firmware}"
        return Classification("yes", "receipt", f"antwortet auf ESC/POS-Modellabfrage ({answer})")
    d = printer.details
    known = _join(str(d.get("manufacturer") or ""), str(d.get("model") or "")).strip()
    hostname = str(d.get("hostname") or "")
    why = (
        f"Port {d.get('port') or 9100} nicht erreichbar" if not connected else "keine Antwort auf ESC/POS-Modellabfrage"
    )
    if match_receipt(known):
        return Classification("no", "receipt", f"Modell {known}, {why}")
    if match_label(known):
        return Classification("no", "label", f"Modell {known}, {why}")
    label = match_office(known, hostname=hostname)
    if label:
        return Classification("no", "office", f"{why} ({label})")
    return Classification("no", "unknown", why)
