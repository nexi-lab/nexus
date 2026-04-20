"""Local SQLite push queue for offline resilience (#3804)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_CREATE = """
CREATE TABLE IF NOT EXISTS push_queue (
    profile_id    TEXT PRIMARY KEY,
    payload_hash  TEXT NOT NULL,
    enqueued_at   TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT
)
"""


@dataclass(frozen=True)
class PendingPush:
    profile_id: str
    payload_hash: str
    enqueued_at: datetime
    attempts: int
    last_error: str | None


class PushQueue:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE)
        self._conn.commit()

    def enqueue(self, profile_id: str, *, payload_hash: str) -> None:
        now = datetime.now(UTC).isoformat()
        cur = self._conn.execute(
            "SELECT payload_hash FROM push_queue WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        if cur and cur["payload_hash"] == payload_hash:
            return  # dedupe
        self._conn.execute(
            "INSERT INTO push_queue (profile_id, payload_hash, enqueued_at, attempts) "
            "VALUES (?, ?, ?, 0) "
            "ON CONFLICT(profile_id) DO UPDATE SET "
            "  payload_hash = excluded.payload_hash, "
            "  enqueued_at  = excluded.enqueued_at, "
            "  attempts     = 0, "
            "  last_error   = NULL",
            (profile_id, payload_hash, now),
        )
        self._conn.commit()

    def list_pending(self) -> list[PendingPush]:
        rows = self._conn.execute(
            "SELECT profile_id, payload_hash, enqueued_at, attempts, last_error "
            "FROM push_queue ORDER BY enqueued_at"
        ).fetchall()
        return [
            PendingPush(
                profile_id=r["profile_id"],
                payload_hash=r["payload_hash"],
                enqueued_at=datetime.fromisoformat(r["enqueued_at"]),
                attempts=r["attempts"],
                last_error=r["last_error"],
            )
            for r in rows
        ]

    def mark_success(self, profile_id: str, *, payload_hash: str) -> None:
        """Remove row ONLY if the hash matches (guard against races)."""
        self._conn.execute(
            "DELETE FROM push_queue WHERE profile_id = ? AND payload_hash = ?",
            (profile_id, payload_hash),
        )
        self._conn.commit()

    def record_attempt(self, profile_id: str, *, error: str) -> None:
        self._conn.execute(
            "UPDATE push_queue SET attempts = attempts + 1, last_error = ? WHERE profile_id = ?",
            (error, profile_id),
        )
        self._conn.commit()

    def last_pushed_hash(self, profile_id: str) -> str | None:
        """Currently queued (unflushed) hash, or None if not queued."""
        row = self._conn.execute(
            "SELECT payload_hash FROM push_queue WHERE profile_id = ?",
            (profile_id,),
        ).fetchone()
        return row["payload_hash"] if row else None

    def close(self) -> None:
        self._conn.close()
