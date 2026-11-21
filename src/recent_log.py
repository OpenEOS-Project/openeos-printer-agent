"""In-memory ring buffer of the latest warnings and errors.

The desktop window shows "what went wrong lately" without making the user
dig through ``agent.log``. ``setup_logging`` attaches one shared handler to
the root logger; the local API reads it via ``recent_problems()``.
"""

import logging
import threading
import time
from collections import deque
from typing import Any

MAX_ENTRIES = 30


class RecentProblemsHandler(logging.Handler):
    """Keeps the last ``capacity`` records at WARNING or above."""

    def __init__(self, capacity: int = MAX_ENTRIES) -> None:
        super().__init__(level=logging.WARNING)
        self._entries: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._entries_lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - broken format args
            message = str(record.msg)
        entry = {
            "time": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": message[:500],
        }
        with self._entries_lock:
            self._entries.append(entry)

    def entries(self) -> list[dict[str, Any]]:
        """Newest first."""
        with self._entries_lock:
            return list(reversed(self._entries))

    def clear(self) -> None:
        with self._entries_lock:
            self._entries.clear()


_HANDLER = RecentProblemsHandler()


def handler() -> RecentProblemsHandler:
    return _HANDLER


def install() -> None:
    """Attach the shared handler to the root logger (idempotent)."""
    root = logging.getLogger()
    if _HANDLER not in root.handlers:
        root.addHandler(_HANDLER)


def recent_problems(limit: int = 10, max_age_s: float = 24 * 3600) -> list[dict[str, Any]]:
    cutoff = time.time() - max_age_s
    return [e for e in _HANDLER.entries() if e["time"] >= cutoff][:limit]
