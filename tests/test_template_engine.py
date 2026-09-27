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
