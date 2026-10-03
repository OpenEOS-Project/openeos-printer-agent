import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class JobStore:
    """Crash-safe SQLite store for print jobs.

    Lifecycle of a row:
      1. ``add()`` when a job arrives over the websocket — BEFORE the ack is
         sent, so an acked job can never be lost by a crash. Duplicate
         deliveries (server replay) are detected here by job_id.
      2. ``mark_outcome()`` when the job finished but the outcome could not be
         reported to the server (offline) — re-reported on reconnect.
      3. ``remove()`` once the outcome has been reported successfully.
    """

    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                data TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_code TEXT,
                error_message TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()
        logger.info(f"Job store ready: {path}")

    def add(self, job_id: str, data: dict[str, Any]) -> bool:
        """Persist a newly received job. Returns False if the job_id is
        already known (duplicate delivery, e.g. server replay)."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO jobs (job_id, data, status, created_at) VALUES (?, ?, 'pending', ?)",
            (job_id, json.dumps(data), time.time()),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def remove(self, job_id: str) -> None:
        self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
        self._conn.commit()

    def mark_outcome(
        self,
        job_id: str,
        status: str,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        """Record a finished job whose outcome has not reached the server yet
        (status: 'completed' or 'failed')."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, error_code = ?, error_message = ? WHERE job_id = ?",
            (status, error_code, error_message, job_id),
        )
        self._conn.commit()

    def get_pending(self) -> list[dict[str, Any]]:
        """Job payloads that were received but never finished (re-enqueue on start)."""
        rows = self._conn.execute("SELECT data FROM jobs WHERE status = 'pending' ORDER BY created_at ASC").fetchall()
        jobs = []
        for row in rows:
            try:
                jobs.append(json.loads(row["data"]))
            except json.JSONDecodeError:
                logger.warning("Dropping corrupt persisted job payload")
        return jobs

    def get_unreported(self) -> list[dict[str, Any]]:
        """Finished jobs whose outcome still has to be reported to the server."""
        rows = self._conn.execute(
            "SELECT job_id, status, error_code, error_message FROM jobs "
            "WHERE status IN ('completed', 'failed') ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()
