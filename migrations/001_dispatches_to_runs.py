"""Backfill legacy `dispatches` rows into `runs`.

The dispatches table is left in place, unread, for one release.
Idempotent: rows already migrated are skipped via the marker column.
"""

import logging
import sqlite3
import uuid
from contextlib import closing

from data_paths import db_path
from run_store import RunStatus

log = logging.getLogger("jarvis.migrations")

STATUS_MAP = {
    "pending": RunStatus.QUEUED,
    "queued": RunStatus.QUEUED,
    "building": RunStatus.RUNNING,
    "planning": RunStatus.RUNNING,
    "working": RunStatus.RUNNING,
    "running": RunStatus.RUNNING,
    "completed": RunStatus.SUCCEEDED,
    "done": RunStatus.SUCCEEDED,
    "succeeded": RunStatus.SUCCEEDED,
    "failed": RunStatus.FAILED,
    "error": RunStatus.FAILED,
    "timeout": RunStatus.TIMED_OUT,
    "timed_out": RunStatus.TIMED_OUT,
    "cancelled": RunStatus.CANCELLED,
}


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()
    return row is not None


def migrate() -> int:
    with closing(sqlite3.connect(str(db_path()))) as conn:
        conn.row_factory = sqlite3.Row

        if not _has_table(conn, "dispatches"):
            return 0

        cols = {r["name"] for r in conn.execute("PRAGMA table_info(dispatches)")}
        if "migrated_run_id" not in cols:
            conn.execute("ALTER TABLE dispatches ADD COLUMN migrated_run_id TEXT")
            conn.commit()

        rows = conn.execute(
            "SELECT * FROM dispatches WHERE migrated_run_id IS NULL").fetchall()

        migrated = 0
        for row in rows:
            run_id = str(uuid.uuid4())
            status = STATUS_MAP.get((row["status"] or "").lower(), RunStatus.FAILED)
            conn.execute(
                "INSERT INTO runs (id, project_name, project_path, prompt, origin, "
                "status, result_text, summary, created_at, started_at, ended_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, row["project_name"], row["project_path"],
                 row["original_prompt"], "voice", status,
                 row["claude_response"] or "", row["summary"] or "",
                 row["created_at"], row["created_at"], row["completed_at"]))
            conn.execute("UPDATE dispatches SET migrated_run_id=? WHERE id=?",
                         (run_id, row["id"]))
            migrated += 1

        conn.commit()

    if migrated:
        log.info("migrated %d dispatch row(s) into runs", migrated)
    return migrated
