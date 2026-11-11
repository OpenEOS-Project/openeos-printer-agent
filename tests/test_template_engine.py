import re

import pytest

from src.template_engine import TemplateEngine, _filter_currency, _filter_strftime


class TestFilters:
    def test_strftime_iso_string(self):
        result = _filter_strftime("2024-06-15T14:30:00Z", "%d.%m.%Y %H:%M")
        assert result == "15.06.2024 14:30"

    def test_strftime_default_format(self):
        result = _filter_strftime("2024-06-15T14:30:00+00:00")
        assert "15.06.2024" in result

    def test_strftime_invalid_string(self):
        assert _filter_strftime("not-a-date") == "not-a-date"

    def test_currency_basic(self):
        assert _filter_currency(10.5) == "10,50 EUR"

    def test_currency_thousands(self):
        result = _filter_currency(1234.56)
        assert "1.234,56" in result

    def test_currency_zero(self):
        assert _filter_currency(0) == "0,00 EUR"


class TestTemplateEngine:
    def test_render_receipt(self, sample_print_job):
        engine = TemplateEngine()
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 80})
        assert "Test Verein" in result
        assert "#42" in result
        assert "Bratwurst" in result
        assert "Cola" in result

    def test_receipt_prints_one_vat_line_per_rate(self, sample_print_job):
        engine = TemplateEngine()
        payload = {
            **sample_print_job["payload"],
            "paper_width": 80,
            "tax_amount": 2.56,
            "tax_lines": [
                {"rate": 19, "net": 5.0, "tax": 0.95, "gross": 5.95},
                {"rate": 7.0, "net": 23.0, "tax": 1.61, "gross": 24.61},
            ],
        }
        result = engine.render("receipt", payload)
        assert "enth. MwSt 19%:" in result
        assert "enth. MwSt 7%:" in result
        assert "0,95 EUR" in result
        assert "1,61 EUR" in result
        # Keine pauschale Summenzeile neben den Einzelzeilen.
        assert "2,56 EUR" not in result

    def test_receipt_without_vat(self, sample_print_job):
        engine = TemplateEngine()
        payload = {k: v for k, v in sample_print_job["payload"].items() if not k.startswith("tax")}
        result = engine.render("receipt", {**payload, "paper_width": 80})
        assert "MwSt" not in result

    def test_receipt_prints_change(self, sample_print_job):
        engine = TemplateEngine()
        payload = {**sample_print_job["payload"], "paper_width": 80, "payment_method": "cash", "change": 4.5}
        result = engine.render("receipt", payload)
        assert "Rueckgeld: 4,50 EUR" in result

    def test_receipt_prints_payment_label(self, sample_print_job):
        engine = TemplateEngine()
        payload = {**sample_print_job["payload"], "paper_width": 80, "payment_method": "cash", "payment_label": "Bar"}
        result = engine.render("receipt", payload)
        assert "Zahlungsart: Bar" in result
        assert "cash" not in result

    def test_receipt_falls_back_to_raw_method_without_label(self, sample_print_job):
        engine = TemplateEngine()
        payload = {**sample_print_job["payload"], "paper_width": 80, "payment_method": "card"}
        result = engine.render("receipt", payload)
        assert "Zahlungsart: card" in result

    def test_order_ticket_prints_payment_label(self, sample_print_job):
        engine = TemplateEngine()
        payload = {
            **sample_print_job["payload"],
            "paper_width": 80,
            "payment_method": "sumup_terminal",
            "payment_label": "Karte (SumUp)",
        }
        result = engine.render("order_ticket", payload)
        assert "Zahlungsart: Karte (SumUp)" in result

    @pytest.mark.parametrize(
        "organization",
        [
            {"name": "Test Verein", "address": "", "phone": ""},
            {"name": "Test Verein", "address": None, "phone": None},
            {"name": "Test Verein"},
        ],
    )
    def test_receipt_header_skips_empty_address_and_phone(self, sample_print_job, organization):
        engine = TemplateEngine()
        payload = {**sample_print_job["payload"], "paper_width": 80, "organization": organization}
        result = engine.render("receipt", payload)
        assert "undefined" not in result
        assert "None" not in result
        assert "Tel:" not in result
        lines = [re.sub(r"<</?[A-Z]+(:[^>]*)?>>", "", line).strip() for line in result.splitlines()]
        name_at = next(i for i, line in enumerate(lines) if "Test Verein" in line)
        # Direkt nach dem Namen die Trennlinie, keine leere Adresszeile.
        assert set(lines[name_at + 1]) == {"="}

    def test_receipt_header_prints_address(self, sample_print_job):
        engine = TemplateEngine()
        payload = {
            **sample_print_job["payload"],
            "paper_width": 80,
            "organization": {"name": "Test Verein", "address": "Weg 1, 12345 Ort", "phone": "0123"},
        }
        result = engine.render("receipt", payload)
        assert "Weg 1, 12345 Ort" in result
        assert "Tel: 0123" in result

    def test_render_kitchen(self, sample_kitchen_job):
        engine = TemplateEngine()
        result = engine.render("kitchen_ticket", {**sample_kitchen_job["payload"], "paper_width": 80})
        assert "KUECHE" in result
        assert "#42" in result
        assert "Bratwurst" in result
        assert "ohne Senf" in result
        assert "extra knusprig" in result

    def test_kitchen_shows_waiter_only_when_known(self, sample_kitchen_job):
        engine = TemplateEngine()
        payload = {**sample_kitchen_job["payload"], "paper_width": 80}
        result = engine.render("kitchen_ticket", {**payload, "waiter_name": "Handy Markus"})
        assert "Bedienung: Handy Markus" in result
        for missing in ({"waiter_name": None}, {"waiter_name": ""}, {}):
            result = engine.render("kitchen_ticket", {**payload, **missing})
            assert "Bedienung" not in result

    def test_kitchen_marks_tse_outage(self, sample_kitchen_job):
        engine = TemplateEngine()
        payload = {**sample_kitchen_job["payload"], "paper_width": 80}
        result = engine.render("kitchen_ticket", {**payload, "tse_outage": True})
        assert "TSE-AUSFALL" in result
        assert "Bestellung nicht abgesichert" in result
        for missing in ({"tse_outage": False}, {}):
            result = engine.render("kitchen_ticket", {**payload, **missing})
            assert "TSE-AUSFALL" not in result

    def test_receipt_prints_service_fee(self, sample_print_job):
        engine = TemplateEngine()
        payload = {**sample_print_job["payload"], "paper_width": 80}
        result = engine.render("receipt", {**payload, "service_fee": 0.5})
        assert "Servicegebuehr:" in result
        assert "Servicegebuehr" not in engine.render("receipt", payload)

    def test_render_order(self, sample_print_job):
        engine = TemplateEngine()
        result = engine.render("order_ticket", {**sample_print_job["payload"], "paper_width": 80})
        assert "BESTELLUNG" in result

    def test_render_pickup(self, sample_print_job):
        engine = TemplateEngine()
        data = {**sample_print_job["payload"], "paper_width": 80, "customer_name": "Max"}
        result = engine.render("pickup", data)
        assert "ABHOLUNG" in result
        assert "Max" in result

    def test_available_templates(self):
        engine = TemplateEngine()
        templates = engine.get_available_templates()
        assert "receipt" in templates
        assert "kitchen_ticket" in templates
        assert "order_ticket" in templates
        assert "pickup" in templates

    def test_missing_template(self):
        engine = TemplateEngine()
        with pytest.raises(Exception, match="not found"):
            engine.render("nonexistent", {})

    def test_server_template_override(self):
        engine = TemplateEngine()
        engine.update_server_templates(
            {
                "custom": "Hello {{ name }}!",
            }
        )
        result = engine.render("custom", {"name": "World"})
        assert result == "Hello World!"

    def test_server_template_takes_priority(self):
        engine = TemplateEngine()
        engine.update_server_templates(
            {
                "receipt": "Custom receipt for {{ organization.name }}",
            }
        )
        result = engine.render("receipt", {"organization": {"name": "Test"}})
        assert result == "Custom receipt for Test"

    def test_58mm_paper_width(self, sample_print_job):
        engine = TemplateEngine()
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 58})
        # Should render without errors (narrower columns)
        assert "Test Verein" in result


class TestRefundTemplates:
    """Gegenbeleg (Erstattung/Storno) und Storno-Bon der Kueche."""

    def _refund_payload(self, **over):
        payload = {
            "organization": {"name": "Test Verein", "address": "Weg 1, 12345 Ort"},
            "refund_number": "20261008-0042-E1",
            "refund_kind": "refund",
            "created_at": "2026-10-08T12:00:00Z",
            "original_created_at": "2026-10-08T11:30:00Z",
            "order_number": "20261008-0042",
            "daily_number": 42,
            "table_number": "A06",
            "items": [{"quantity": 1, "name": "Pils", "total": -3.6}],
            "pfand_amount": -2,
            "tip_amount": 0,
            "tax_lines": [{"rate": 19, "net": -3.03, "tax": -0.57, "gross": -3.6}],
            "total": -5.6,
            "payment_method": "cash",
            "payment_label": "Bar",
            "refund_status": "completed",
            "reason": "Qualitaet: schal",
            "actor_name": "Lena Kasse",
            "device_name": "Kasse 1",
            "paper_width": 80,
        }
        payload.update(over)
        return payload

    def test_refund_receipt_shows_negative_amounts_and_reference(self):
        result = TemplateEngine().render("refund_receipt", self._refund_payload())
        assert "ERSTATTUNG" in result
        assert "Gegenbeleg" in result
        assert "Beleg 20261008-0042-E1" in result
        assert "Zu Bestellung #20261008-0042 (Nr. 42)" in result
        assert "-1x" in result and "Pils" in result
        assert "-3,60 EUR" in result
        assert "Pfand:" in result and "-2,00 EUR" in result
        assert "enth. MwSt 19%:" in result and "-0,57 EUR" in result
        assert "ERSTATTET:" in result and "-5,60 EUR" in result
        assert "Rueckgabe: Bar" in result
        assert "Grund: Qualitaet: schal" in result
        assert "Bediener: Lena Kasse" in result
        assert "NACHDRUCK" not in result
        assert "TESTMODUS" not in result

    def test_refund_receipt_storno_reprint_manual_test(self):
        result = TemplateEngine().render(
            "refund_receipt",
            self._refund_payload(
                refund_kind="cancellation",
                reprint=True,
                is_test=True,
                refund_status="manual",
                payment_label="Karte (SumUp)",
                provider_reference="txn-77",
            ),
        )
        assert "STORNO" in result
        assert "NACHDRUCK" in result
        assert "TESTMODUS" in result
        assert "(manuell erstattet)" in result
        assert "Transaktion: txn-77" in result

    def test_refund_receipt_minimal_payload(self):
        result = TemplateEngine().render(
            "refund_receipt",
            {"organization": {}, "created_at": "2026-10-08T12:00:00Z", "total": -1},
        )
        assert "ERSTATTET:" in result

    def test_cancellation_ticket(self):
        result = TemplateEngine().render(
            "cancellation_ticket",
            {
                "daily_number": 5,
                "table_number": "A06",
                "station_name": "Grill",
                "items": [{"quantity": 2, "name": "Pommes", "options": ["ohne Salz"]}],
                "reason": "Wunsch des Gastes",
                "created_at": "2026-10-08T12:00:00Z",
                "paper_width": 80,
            },
        )
        assert "STORNO" in result
        assert "Nicht zubereiten" in result
        assert "#5" in result
        assert "TISCH A06" in result
        assert "Station: Grill" in result
        assert "-2x Pommes" in result
        assert "ohne Salz" in result
        assert "Grund: Wunsch des Gastes" in result


# Kassenbeleg mit Cloud-TSE (Vertrag: tse-swissbit-plan.md §8.6). Werte aus
# einem echten Beleg der Swissbit-Entwicklungsumgebung.
TSE_SERIAL = "2d38954f7b7307eb0f90abe12fc4c75fb0e67c51528a47e39f3690b4b9fb162a"
TSE_SIGNATURE = "vd4M3r28Ajy+h9awoxaCREPADXzr3CyVM720RM/W0fMXPGy/fYr6l5i6mMw31+Tij7gi9xZwqoIb0P/B2Cv7HQ=="
TSE_PUBLIC_KEY = "BOMQhrdbaRAdsY+lwgytAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
TSE_QR = (
    "V0;OEOS-2BDA366D6B8D;Kassenbeleg-V1;Beleg^9.00_3.50_0.00_0.00_0.00^12.50:Bar;84;183;"
    "2026-11-03T16:02:33.000Z;2026-11-03T16:02:34.000Z;ecdsa-plain-SHA256;unixTime;"
    f"{TSE_SIGNATURE};{TSE_PUBLIC_KEY}"
)


def tse_block(**overrides):
    block = {
        "status": "signed",
        "failed": False,
        "training": False,
        "notice": None,
        "process_type": "Kassenbeleg-V1",
        "serial_number": TSE_SERIAL,
        "client_id": "OEOS-2BDA366D6B8D",
        "transaction_number": 84,
        "signature_counter": 183,
        "start_time": "2026-11-03T16:02:33.000Z",
        "finish_time": "2026-11-03T16:02:34.000Z",
        "signature": TSE_SIGNATURE,
        "signature_algorithm": "ecdsa-plain-SHA256",
        "log_time_format": "unixTime",
        "public_key": TSE_PUBLIC_KEY,
        "qr_code": TSE_QR,
    }
    block.update(overrides)
    return block


def unwrap(text: str) -> str:
    """Zeilen ohne Umbruch zusammenfuegen (Seriennummer, Signatur)."""
    return "".join(line.strip() for line in text.splitlines())


class TestTseReceipt:
    @pytest.mark.parametrize("paper", [80, 58])
    def test_signed_receipt_prints_all_tse_fields(self, sample_print_job, paper):
        engine = TemplateEngine()
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": paper, "tse": tse_block()})
        cols = 42 if paper == 80 else 32
        assert "Kassen-ID: OEOS-2BDA366D6B8D" in result
        assert "TSE-Transaktion: 84" in result
        assert "Signaturzaehler: 183" in result
        # Zeiten in UTC wie geliefert, nicht umgerechnet.
        assert "Start: 2026-11-03T16:02:33.000Z" in result
        assert "Ende:  2026-11-03T16:02:34.000Z" in result
        assert "Algorithmus: ecdsa-plain-SHA256" in result
        assert "Zeitformat: unixTime" in result
        # Seriennummer und Signatur ungekuerzt, nur umbrochen.
        assert TSE_SERIAL in unwrap(result)
        assert TSE_SIGNATURE in unwrap(result)
        tse_section = result[result.index("Kassen-ID") :]
        for line in tse_section.splitlines():
            if "<<" not in line:
                assert len(line) <= cols, line
        assert f"<<QRCODE:{TSE_QR}>>" in result
        assert "TSE nicht erreichbar" not in result
        assert "TRAININGSBUCHUNG" not in result

    def test_failed_tse_prints_notice_without_signature(self, sample_print_job):
        engine = TemplateEngine()
        block = tse_block(
            status="failed",
            failed=True,
            notice="TSE ausgefallen",
            transaction_number=None,
            signature_counter=None,
            start_time=None,
            finish_time=None,
            signature=None,
            qr_code=None,
        )
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        assert "<<BOLD>>TSE nicht erreichbar<</BOLD>>" in result
        assert "Beleg ohne Signatur" in result
        assert "TSE ausgefallen" not in result
        assert "Kassen-ID: OEOS-2BDA366D6B8D" in result
        assert "TSE-Transaktion" not in result
        assert "Signatur:" not in result
        assert "QRCODE" not in result.replace("qr_url", "")

    def test_pending_counts_as_failed(self, sample_print_job):
        engine = TemplateEngine()
        block = tse_block(status="pending", failed=True, signature=None, qr_code=None)
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        assert "TSE nicht erreichbar" in result

    def test_training_receipt_is_marked(self, sample_print_job):
        engine = TemplateEngine()
        qr = TSE_QR.replace("Beleg^", "AVTraining^")
        block = tse_block(status="training", training=True, notice="TRAININGSBUCHUNG – KEIN VERKAUF", qr_code=qr)
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 58, "tse": block})
        assert "<<BOLD>>TRAININGSBUCHUNG - KEIN VERKAUF<</BOLD>>" in result
        assert f"<<QRCODE:{qr}>>" in result

    def test_receipt_without_tse_is_unchanged(self, sample_print_job):
        engine = TemplateEngine()
        for tse in (None, {}):
            result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 80, "tse": tse})
            assert "Kassen-ID" not in result
            assert "TSE" not in result

    def test_refund_receipt_prints_tse_block(self):
        engine = TemplateEngine()
        qr = TSE_QR.replace("Beleg^9.00_3.50_0.00_0.00_0.00^12.50:Bar", "Beleg^-4.50_0.00_0.00_0.00_0.00^-4.50:Bar")
        payload = {
            "paper_width": 80,
            "organization": {"name": "Test Verein"},
            "refund_number": "E-0001",
            "order_number": "20261103-0001",
            "created_at": "2026-11-03T16:10:00Z",
            "items": [{"quantity": 1, "name": "Bier", "total": -4.5}],
            "total": -4.5,
            "payment_method": "cash",
            "device_name": "Kasse 1",
            "tse": tse_block(qr_code=qr, transaction_number=85, signature_counter=185),
        }
        result = engine.render("refund_receipt", payload)
        assert "Kasse: Kasse 1" in result
        assert "Kassen-ID: OEOS-2BDA366D6B8D" in result
        assert "TSE-Transaktion: 85" in result
        assert f"<<QRCODE:{qr}>>" in result


VAT_EXEMPT_NOTICE = "Steuerfrei nach § 19 Abs. 1 UStG (Kleinunternehmer)"
LATE_SIGNATURE = {
    "note": "Signatur nach Belegausgabe",
    "transaction_number": 97,
    "signature_counter": 201,
    "finish_time": "2026-11-03T16:41:07.000Z",
}
ORDER_START = "2026-11-03T15:58:12.000Z"


def failed_tse_block(**overrides):
    """Ausfallbeleg: ohne Transaktion und Signatur, QR-Code bleibt leer."""
    block = tse_block(
        status="failed",
        failed=True,
        transaction_number=None,
        signature_counter=None,
        start_time=None,
        finish_time=None,
        signature=None,
        qr_code=None,
    )
    block.update(overrides)
    return block


def printed_lines(text: str) -> list[str]:
    return [MARKUP.sub("", line).strip() for line in text.splitlines()]


class TestFiscalNotes:
    """Hinweise aus der Steuerberater-Pruefung 2026-11-10 (Fragen 3, 9, 16, 19)."""

    @pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
    def test_late_signature_on_reprinted_outage_receipt(self, sample_print_job, name):
        # Auch ein (versehentlich) mitgeschickter QR-Code darf im Ausfallzweig
        # nie gedruckt werden: der Beleg selbst blieb unsigniert.
        block = failed_tse_block(late_signature=LATE_SIGNATURE, qr_code=TSE_QR, bon_number=12)
        payload = {**sample_print_job["payload"], "paper_width": 80, "reprint": True, "tse": block}
        result = TemplateEngine().render(name, payload)
        lines = printed_lines(result)
        assert "TSE nicht erreichbar" in lines
        assert "Beleg ohne Signatur" in lines
        start = lines.index("Signatur nach Belegausgabe:")
        assert start > lines.index("Beleg ohne Signatur")
        assert lines[start + 1 : start + 4] == [
            "TSE-Transaktion: 97",
            "Signaturzaehler: 201",
            "Ende:  2026-11-03T16:41:07.000Z",
        ]
        assert "QRCODE" not in result
        assert "Signatur:" not in lines

    def test_failed_tse_without_late_signature(self, sample_print_job):
        for late in (None, {}):
            block = failed_tse_block(late_signature=late)
            payload = {**sample_print_job["payload"], "paper_width": 80, "tse": block}
            result = TemplateEngine().render("receipt", payload)
            assert "Signatur nach Belegausgabe" not in result
            assert "TSE-Transaktion" not in result

    def test_late_signature_is_ignored_on_signed_receipts(self, sample_print_job):
        block = tse_block(late_signature=LATE_SIGNATURE)
        result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        assert "Signatur nach Belegausgabe" not in result
        assert "TSE-Transaktion: 84" in result

    @pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
    def test_order_start_time_in_signed_branch(self, sample_print_job, name):
        block = tse_block(order_start_time=ORDER_START)
        result = TemplateEngine().render(name, {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        lines = printed_lines(result)
        begin = lines.index(f"Bestellbeginn: {ORDER_START}")
        assert lines[begin + 1] == "Start: 2026-11-03T16:02:33.000Z"
        assert lines[begin - 1] == "Signaturzaehler: 183"

    @pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
    def test_order_start_time_in_failed_branch(self, sample_print_job, name):
        block = failed_tse_block(order_start_time=ORDER_START, bon_number=12)
        result = TemplateEngine().render(name, {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        lines = printed_lines(result)
        begin = lines.index(f"Bestellbeginn: {ORDER_START}")
        assert lines[begin - 1] == "Beleg-Nr.: 12"
        assert lines[begin + 1] == "TSE-Seriennummer:"
        assert "Start:" not in result

    def test_order_start_time_wraps_on_58mm(self, sample_print_job):
        block = tse_block(order_start_time=ORDER_START)
        result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "paper_width": 58, "tse": block})
        assert f"Bestellbeginn:{ORDER_START}" in unwrap(result)
        assert all(width <= 32 for width, _ in printed_widths(result))

    def test_order_start_time_missing_or_null(self, sample_print_job):
        for block in (tse_block(), tse_block(order_start_time=None), failed_tse_block(order_start_time=None)):
            payload = {**sample_print_job["payload"], "paper_width": 80, "tse": block}
            result = TemplateEngine().render("receipt", payload)
            assert "Bestellbeginn" not in result

    def test_receipt_reprint_marker(self, sample_print_job):
        payload = {**sample_print_job["payload"], "paper_width": 80, "is_test": True, "reprint": True}
        result = TemplateEngine().render("receipt", payload)
        assert "<<CENTER>><<BOLD>>-- NACHDRUCK --<</BOLD>><</CENTER>>" in result
        # Unter dem Testmodus-Banner, ueber dem Kopf.
        assert result.index("TESTMODUS") < result.index("NACHDRUCK") < result.index("Test Verein")
        for reprint in (False, None):
            result = TemplateEngine().render("receipt", {**payload, "reprint": reprint})
            assert "NACHDRUCK" not in result
        assert "NACHDRUCK" not in TemplateEngine().render("receipt", sample_print_job["payload"])

    @pytest.mark.parametrize("cols, paper", [(32, 58), (42, 80)])
    def test_tip_line_before_vat(self, sample_print_job, cols, paper):
        payload = {
            **sample_print_job["payload"],
            "paper_width": paper,
            "tip_amount": 1.5,
            "total": 12.0,
            "tax_lines": [{"rate": 19, "tax": 1.92}],
        }
        result = TemplateEngine().render("receipt", payload)
        lines = printed_lines(result)
        tip = next(i for i, line in enumerate(lines) if line.startswith("Trinkgeld:"))
        assert lines[tip].endswith(" 1,50 EUR") and len(lines[tip]) == cols
        assert tip < next(i for i, line in enumerate(lines) if line.startswith("enth. MwSt 19%:"))
        assert tip < next(i for i, line in enumerate(lines) if line.startswith("GESAMT:"))
        # GESAMT enthaelt das Trinkgeld bereits, die Vorlage rechnet nicht.
        assert "12,00 EUR" in next(line for line in lines if line.startswith("GESAMT:"))

    def test_no_tip_line_without_positive_tip(self, sample_print_job):
        for tip in (None, 0, "0.00", -1):
            result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "tip_amount": tip})
            assert "Trinkgeld" not in result

    def test_discount_and_pfand_lines(self, sample_print_job):
        payload = {
            **sample_print_job["payload"],
            "paper_width": 80,
            "subtotal": 10.5,
            "discount_amount": "1.50",
            "pfand_total": "2.00",
        }
        lines = printed_lines(TemplateEngine().render("receipt", payload))
        discount = next(i for i, line in enumerate(lines) if line.startswith("Rabatt:"))
        pfand = next(i for i, line in enumerate(lines) if line.startswith("Pfand:"))
        assert lines[discount].endswith(" -1,50 EUR") and len(lines[discount]) == 42
        assert lines[pfand].endswith(" 2,00 EUR") and len(lines[pfand]) == 42
        subtotal = next(i for i, line in enumerate(lines) if line.startswith("Zwischensumme:"))
        assert subtotal < discount < pfand < next(i for i, line in enumerate(lines) if line.startswith("GESAMT:"))

    def test_no_discount_or_pfand_lines_when_zero(self, sample_print_job):
        for value in (None, 0, "0.00"):
            payload = {**sample_print_job["payload"], "discount_amount": value, "pfand_total": value}
            result = TemplateEngine().render("receipt", payload)
            assert "Rabatt" not in result and "Pfand" not in result

    @pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
    @pytest.mark.parametrize("cols, paper", [(32, 58), (42, 80)])
    def test_vat_exempt_notice_replaces_vat_lines(self, sample_print_job, name, cols, paper):
        payload = {
            **sample_print_job["payload"],
            "paper_width": paper,
            "vat_exempt_notice": VAT_EXEMPT_NOTICE,
            "tax_lines": [{"rate": 19, "tax": 1.68}],
            "tax_amount": 1.68,
        }
        result = TemplateEngine().render(name, payload)
        # "§" fehlt in CP437, auf Papier steht "Par.".
        assert "§" not in result
        assert "Steuerfrei nach Par. 19 Abs. 1 UStG (Kleinunternehmer)" in " ".join(printed_lines(result))
        assert "MwSt" not in result
        assert all(width <= cols for width, _ in printed_widths(result))
        lines = printed_lines(result)
        notice = next(i for i, line in enumerate(lines) if line.startswith("Steuerfrei"))
        total = next(i for i, line in enumerate(lines) if line.startswith(("GESAMT:", "ERSTATTET:")))
        assert notice < total

    def test_vat_lines_without_notice(self, sample_print_job):
        for notice in (None, ""):
            tax_lines = [{"rate": 19, "tax": 1.68}]
            payload = {**sample_print_job["payload"], "vat_exempt_notice": notice, "tax_lines": tax_lines}
            result = TemplateEngine().render("receipt", payload)
            assert "enth. MwSt 19%:" in result
            assert "Steuerfrei" not in result

    @pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
    def test_old_payloads_render_without_new_lines(self, sample_print_job, name):
        result = TemplateEngine().render(name, {**sample_print_job["payload"], "tse": tse_block()})
        for text in ("Bestellbeginn", "Signatur nach Belegausgabe", "Trinkgeld", "Steuerfrei", "Rabatt"):
            assert text not in result


# Zeilenbreite: Jede Vorlage muss bei 32 (58 mm), 42 (80 mm) und 48 Zeichen
# (80 mm mit 576 Punkten) in die Zeile passen, sonst bricht der Drucker das
# letzte Zeichen in die naechste Zeile um. Die Daten sind absichtlich lang.
LONG_NAME = "Currywurst mit Pommes und extra scharfer Sauce"
LONG_TEXT = "bitte ohne Zwiebeln und Senf, dafuer mit doppelt Ketchup und Mayo"

LONG_COMMON = {
    "is_test": True,
    "organization": {
        "name": "Freiwillige Feuerwehr Musterstadt-Oberdorf e.V.",
        "address": "Hauptstrasse 123, 12345 Musterstadt-Oberdorf am Rhein",
        "phone": "+49 (0) 1234 567890-123",
    },
    "event_name": "Sommerfest der Freiwilligen Feuerwehr Musterstadt 2026",
    "order_number": "20261103-0042",
    "daily_number": 42,
    "order_id": "6f1c2d3e-4b5a-4c6d-8e7f-0123456789ab",
    "order_item_id": "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "created_at": "2026-11-03T16:02:33Z",
    "original_created_at": "2026-11-03T15:30:00Z",
    "table_number": "Terrasse hinten links 12",
    "customer_name": "Maximiliane Mustermann-Schmidtbauer",
    "station_name": "Grill und Fritteuse im Aussenbereich",
    "priority": "high",
    "items": [
        {"quantity": 2, "name": "Bratwurst", "total": 7.0, "notes": LONG_TEXT, "kitchen_notes": LONG_TEXT},
        {"quantity": 12, "name": LONG_NAME, "total": 1234.5, "options": ["Grosse Portion mit extra Kaese", LONG_TEXT]},
        {"quantity": 100, "name": "Pfand", "total": 200.0},
    ],
    "subtotal": 1441.5,
    "tax_lines": [{"rate": 19, "tax": 230.16}, {"rate": 7.5, "tax": 1.23}],
    "total": 12345.67,
    "payment_method": "sumup_terminal",
    "payment_label": "Karte (SumUp Solo Terminal am Eingang)",
    "paid_amount": 12345.67,
    "change": 1.5,
    "qr_url": "https://app.openeos.de/r/0a1b2c3d",
    "refund_number": "20261103-0042-E1",
    "pfand_amount": -200.0,
    "tip_amount": -2.5,
    "reprint": True,
    "refund_status": "manual",
    "provider_reference": "sumup-txn-0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "reason": "Qualitaet: " + LONG_TEXT,
    "actor_name": "Maximiliane Mustermann-Schmidtbauer",
    "device_name": "Kasse 3 am Haupteingang neben der Buehne",
    "waiter_name": "Handy von Maximiliane Mustermann-Schmidtbauer",
    "printerName": "EPSON TM-T20III Theke links (Netzwerk)",
    "jobId": "job-0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d",
    "timestamp": "2026-11-03T16:02:33.000Z",
}

WIDTH_CASES = [
    ("receipt", {"tse": tse_block(bon_number=123456)}),
    ("receipt", {"tse": tse_block(failed=True, signature=None, qr_code=None, bon_number=7)}),
    ("receipt", {"tse": tse_block(training=True, bon_number=8), "tax_lines": None, "tax_amount": 1.5}),
    ("refund_receipt", {"refund_kind": "cancellation", "tse": tse_block(bon_number=9)}),
    # Alle Felder aus der Steuerberater-Pruefung 2026-11-10 auf einmal.
    (
        "receipt",
        {
            "tse": failed_tse_block(bon_number=10, order_start_time=ORDER_START, late_signature=LATE_SIGNATURE),
            "discount_amount": 1234.5,
            "pfand_total": 200.0,
            "tip_amount": 1234.5,
            "vat_exempt_notice": VAT_EXEMPT_NOTICE,
        },
    ),
    (
        "receipt",
        {
            "tse": tse_block(bon_number=11, order_start_time=ORDER_START),
            "discount_amount": 1234.5,
            "pfand_total": 200.0,
            "tip_amount": 1234.5,
        },
    ),
    (
        "refund_receipt",
        {
            "tse": failed_tse_block(bon_number=12, order_start_time=ORDER_START, late_signature=LATE_SIGNATURE),
            "vat_exempt_notice": VAT_EXEMPT_NOTICE,
        },
    ),
    ("refund_receipt", {"tse": tse_block(bon_number=13, order_start_time=ORDER_START)}),
    ("order_ticket", {}),
    ("order_ticket", {"hide_prices": True}),
    ("kitchen_ticket", {"fulfillment_type": "table_service"}),
    ("kitchen_ticket", {"source": "online"}),
    ("kitchen_ticket", {"order_item_id": None}),
    ("kitchen_ticket", {"tse_outage": True}),
    ("cancellation_ticket", {}),
    ("pickup", {}),
    ("_admin_test", {}),
]

MARKUP = re.compile(r"<<(/?\w+)(?::[^>]*)?>>")


def printed_widths(text: str) -> list[tuple[int, str]]:
    """Breite jeder gedruckten Zeile: ohne Markup, Text in BIG doppelt."""
    widths = []
    big = False
    for raw in text.split("\n"):
        # Der Renderer entfernt fuehrende und folgende Leerzeichen.
        line = raw.strip()
        width = 0
        pos = 0
        for m in MARKUP.finditer(line):
            width += len(line[pos : m.start()]) * (2 if big else 1)
            if m.group(1) == "BIG":
                big = True
            elif m.group(1) == "/BIG":
                big = False
            pos = m.end()
        width += len(line[pos:]) * (2 if big else 1)
        widths.append((width, raw))
    return widths


def test_width_cases_cover_every_builtin_template():
    from src.template_engine import BUILTIN_TEMPLATES_DIR

    builtin = {p.stem for p in BUILTIN_TEMPLATES_DIR.glob("*.jinja2")}
    assert builtin == {name for name, _ in WIDTH_CASES}


@pytest.mark.parametrize("cols, paper", [(32, {"paper_width": 58}), (42, {"paper_width": 80}), (48, {"columns": 48})])
@pytest.mark.parametrize("name, extra", WIDTH_CASES)
def test_no_line_exceeds_paper_width(name, extra, cols, paper):
    result = TemplateEngine().render(name, {**LONG_COMMON, **extra, **paper})
    too_wide = [(w, line) for w, line in printed_widths(result) if w > cols]
    assert not too_wide, f"{name} @ {cols} Zeichen: {too_wide}"
    # Trennlinien laufen ueber die volle Breite, nicht ueber die Standardbreite.
    rules = [line.strip() for line in result.splitlines() if set(line.strip()) in ({"="}, {"#"}, {"-"})]
    assert rules and all(len(rule) == cols for rule in rules)


@pytest.mark.parametrize("cols, paper", [(32, 58), (42, 80)])
def test_item_and_total_lines_fill_the_row_exactly(sample_print_job, cols, paper):
    result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "paper_width": paper})
    item = next(line for line in result.splitlines() if line.startswith("2x"))
    assert item.startswith("2x  Bratwurst ")
    assert item.endswith(" 7,00 EUR")
    assert len(item) == cols
    total = next(line for line in result.splitlines() if "GESAMT:" in line)
    assert len(MARKUP.sub("", total)) == cols


def test_columns_setting_overrides_paper_default(sample_print_job):
    result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "paper_width": 80, "columns": 48})
    assert "=" * 48 in result
    # None (Drucker ohne eigene Einstellung) faellt auf die Papierbreite zurueck.
    result = TemplateEngine().render("receipt", {**sample_print_job["payload"], "paper_width": 58, "columns": None})
    assert "=" * 32 in result and "=" * 33 not in result


def test_long_item_names_wrap_instead_of_being_cut():
    payload = {**LONG_COMMON, "paper_width": 58, "items": [{"quantity": 1, "name": LONG_NAME, "total": 9.5}]}
    for name in ("receipt", "refund_receipt", "order_ticket"):
        result = TemplateEngine().render(name, payload)
        words = unwrap_words(result)
        # Alle Woerter in Reihenfolge, auch das letzte (nichts abgeschnitten).
        positions = [words.index(word) for word in LONG_NAME.split()]
        assert positions == sorted(positions), name


def unwrap_words(text: str) -> list[str]:
    return " ".join(MARKUP.sub("", line).strip() for line in text.splitlines()).split()


@pytest.mark.parametrize("name", ["receipt", "refund_receipt"])
@pytest.mark.parametrize("failed", [False, True])
def test_tse_block_prints_bon_number(name, failed):
    block = tse_block(failed=failed, bon_number=4711)
    result = TemplateEngine().render(name, {**LONG_COMMON, "paper_width": 80, "tse": block})
    assert "Beleg-Nr.: 4711" in result
    result = TemplateEngine().render(name, {**LONG_COMMON, "paper_width": 80, "tse": tse_block(failed=failed)})
    assert "Beleg-Nr." not in result
