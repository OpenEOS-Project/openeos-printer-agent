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
        assert "TSE ausgefallen" not in result
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
        assert "<<BOLD>>TSE ausgefallen<</BOLD>>" in result
        assert "Kassen-ID: OEOS-2BDA366D6B8D" in result
        assert "TSE-Transaktion" not in result
        assert "Signatur:" not in result
        assert "QRCODE" not in result.replace("qr_url", "")

    def test_pending_counts_as_failed(self, sample_print_job):
        engine = TemplateEngine()
        block = tse_block(status="pending", failed=True, signature=None, qr_code=None)
        result = engine.render("receipt", {**sample_print_job["payload"], "paper_width": 80, "tse": block})
        assert "TSE ausgefallen" in result

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
