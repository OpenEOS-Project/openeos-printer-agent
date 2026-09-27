import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
CACHE_DIR = Path(__file__).parent.parent / "cache" / "templates"


def _filter_strftime(value, fmt: str = "%d.%m.%Y %H:%M") -> str:
    """Format a datetime string or object."""
    if isinstance(value, str):
        from datetime import datetime

        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # Timestamps arrive in UTC (…Z); convert to the agent's local time
            # zone before formatting, otherwise the ticket shows UTC (e.g. 2h
            # behind during CEST).
            if dt.tzinfo is not None:
                dt = dt.astimezone()
            return dt.strftime(fmt)
        except ValueError:
            return value
    if hasattr(value, "strftime"):
        return value.strftime(fmt)
    return str(value)


def _filter_rjust(value, width: int, fillchar: str = " ") -> str:
    return str(value).rjust(width, fillchar)


def _filter_center(value, width: int, fillchar: str = " ") -> str:
    return str(value).center(width, fillchar)


def _filter_currency(value, symbol: str = "EUR", decimals: int = 2) -> str:
    """Format a number as currency."""
    try:
        num = float(value)
        formatted = f"{num:,.{decimals}f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{formatted} {symbol}"
    except (ValueError, TypeError):
        return str(value)


class TemplateEngine:
    """Jinja2-based template engine with three-level fallback:
    1. Server templates (in memory, updated via WebSocket)
    2. Cached templates (on disk, persisted across restarts)
    3. Built-in default templates (shipped with agent)
    """

    def __init__(self) -> None:
        self._server_templates: dict[str, str] = {}
        self._env = self._create_env()
        self._load_cached_templates()

    def _create_env(self) -> Environment:
        env = Environment(
            loader=FileSystemLoader(str(BUILTIN_TEMPLATES_DIR)),
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )
        env.filters["strftime"] = _filter_strftime
        env.filters["rjust"] = _filter_rjust
        env.filters["center"] = _filter_center
        env.filters["currency"] = _filter_currency

        # ESC/POS markup tags must survive Jinja rendering so the escpos_renderer
        # can pick them up. We expose them as globals that resolve to angle-bracket
        # markup (e.g. `<<BOLD>>`) — angle brackets don't collide with Jinja2's
        # `{{ ... }}` expression syntax, so closing tags `<</BOLD>>` and
        # argument-bearing tags `<<FEED:3>>` parse cleanly.
        env.globals.update(
            CUT="<<CUT>>",
            BOLD="<<BOLD>>",
            CENTER="<<CENTER>>",
            BIG="<<BIG>>",
            OPEN_DRAWER="<<OPEN_DRAWER>>",
        )
        return env

    def _load_cached_templates(self) -> None:
        """Load cached server templates from disk."""
        if not CACHE_DIR.exists():
            return
        for path in CACHE_DIR.glob("*.jinja2"):
            name = path.stem
            try:
                self._server_templates[name] = path.read_text(encoding="utf-8")
                logger.debug(f"Loaded cached template: {name}")
            except Exception as e:
                logger.warning(f"Failed to load cached template {name}: {e}")

    def render(self, template_name: str, data: dict) -> str:
        """Render a template with data using the fallback chain."""
        # 1. Server templates (memory)
        if template_name in self._server_templates:
            template = self._env.from_string(self._server_templates[template_name])
            return template.render(**data)

        # 2. Built-in templates (filesystem)
        try:
            template = self._env.get_template(f"{template_name}.jinja2")
            return template.render(**data)
        except TemplateNotFound:
            pass

        raise TemplateNotFound(f"Template '{template_name}' not found in server templates, cache, or built-in defaults")

    def update_server_templates(self, templates: dict[str, str]) -> None:
        """Update server templates in memory and persist to disk cache."""
        for name, content in templates.items():
            self._server_templates[name] = content
            # Persist to disk
            try:
                CACHE_DIR.mkdir(parents=True, exist_ok=True)
                cache_path = CACHE_DIR / f"{name}.jinja2"
                cache_path.write_text(content, encoding="utf-8")
                logger.info(f"Cached server template: {name}")
            except Exception as e:
                logger.warning(f"Failed to cache template {name}: {e}")

    def get_available_templates(self) -> list[str]:
        """List all available template names."""
        names = set(self._server_templates.keys())
        # Add built-in templates
        for path in BUILTIN_TEMPLATES_DIR.glob("*.jinja2"):
            names.add(path.stem)
        return sorted(names)
