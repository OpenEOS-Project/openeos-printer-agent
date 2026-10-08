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
