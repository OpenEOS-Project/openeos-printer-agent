import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Finished jobs are remembered this long so a job the server delivers again
# (it replays jobs still "printing" on its side after a reconnect) is not
# printed twice - the agent only re-sends the outcome.
KEEP_FINISHED_S = 24 * 3600


class JobStore:
    """Crash-safe SQLite store for print jobs.

    Lifecycle of a row:
      1. ``add()`` when a job arrives over the websocket - BEFORE the ack is
         sent, so an acked job can never be lost by a crash. Duplicate
         deliveries (server replay) are detected here by job_id.
      2. ``mark_outcome()`` as soon as the job finished (printed or finally
         failed) - before reporting it, so a crash in between never prints
         it again.
      3. ``mark_reported()`` once the server acknowledged the outcome.
      4. ``prune()`` drops reported rows after ``KEEP_FINISHED_S``.
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
        # Columns added in 0.2.7; older stores are migrated in place.
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(jobs)")}
        if "reported" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN reported INTEGER NOT NULL DEFAULT 0")
        if "finished_at" not in columns:
            self._conn.execute("ALTER TABLE jobs ADD COLUMN finished_at REAL")
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

    def get(self, job_id: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT job_id, status, error_code, error_message, reported, finished_at FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        return dict(row) if row else None

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
        """Record a finished job (status: 'completed' or 'failed'); the
        outcome still has to reach the server."""
        self._conn.execute(
            "UPDATE jobs SET status = ?, error_code = ?, error_message = ?, finished_at = ?, reported = 0 "
            "WHERE job_id = ?",
            (status, error_code, error_message, time.time(), job_id),
        )
        self._conn.commit()

    def mark_reported(self, job_id: str) -> None:
        self._conn.execute("UPDATE jobs SET reported = 1 WHERE job_id = ?", (job_id,))
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
            "WHERE status IN ('completed', 'failed') AND reported = 0 ORDER BY created_at ASC"
        ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, keep_s: float = KEEP_FINISHED_S, now: Optional[float] = None) -> int:
        """Forget reported jobs older than ``keep_s``. Unreported outcomes are
        kept for a week before giving up on them."""
        now = time.time() if now is None else now
        cur = self._conn.execute(
            "DELETE FROM jobs WHERE status IN ('completed', 'failed') AND "
            "((reported = 1 AND COALESCE(finished_at, created_at) < ?) OR COALESCE(finished_at, created_at) < ?)",
            (now - keep_s, now - 7 * 24 * 3600),
        )
        self._conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
