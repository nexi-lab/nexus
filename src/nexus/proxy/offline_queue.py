"""WAL-backed offline queue for proxy operations.

Uses aiosqlite in WAL mode for crash-safe persistence.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any

import aiosqlite


@dataclass(frozen=True, slots=True)
class QueuedOperation:
    """A single queued operation awaiting replay."""

    id: int
    method: str
    args_json: str
    kwargs_json: str
    payload_ref: str | None
    retry_count: int
    created_at: float


_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS pending_ops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    method TEXT NOT NULL,
    args_json TEXT NOT NULL,
    kwargs_json TEXT NOT NULL,
    payload_ref TEXT,
    created_at REAL NOT NULL,
    retry_count INTEGER DEFAULT 0,
    max_retries INTEGER DEFAULT 10,
    status TEXT DEFAULT 'pending',
    idempotency_key TEXT
)
"""

_CREATE_INDEX = """\
CREATE INDEX IF NOT EXISTS idx_pending_ops_status
ON pending_ops (status, id)
"""


class OfflineQueue:
    """Persistent offline operation queue backed by SQLite + WAL.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.
    max_retry_count:
        Default max retries before an operation is dead-lettered.
    """

    def __init__(self, db_path: str, max_retry_count: int = 10) -> None:
        self._db_path = os.path.expanduser(db_path)
        self._max_retry_count = max_retry_count
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """Open the database, enable WAL mode, and create the schema."""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute(_CREATE_TABLE)
        await self._db.execute(_CREATE_INDEX)
        await self._db.commit()

    async def enqueue(
        self,
        method: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        payload_ref: str | None = None,
    ) -> int:
        """Add an operation to the queue.  Returns the row id."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        cursor = await self._db.execute(
            "INSERT INTO pending_ops (method, args_json, kwargs_json, payload_ref, created_at, max_retries) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                method,
                json.dumps(args),
                json.dumps(kwargs or {}),
                payload_ref,
                time.time(),
                self._max_retry_count,
            ),
        )
        await self._db.commit()
        if cursor.lastrowid is None:
            raise RuntimeError("Failed to get lastrowid after INSERT")
        return cursor.lastrowid

    async def dequeue_batch(self, limit: int = 50) -> list[QueuedOperation]:
        """Fetch up to *limit* pending operations (FIFO order)."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        cursor = await self._db.execute(
            "SELECT id, method, args_json, kwargs_json, payload_ref, retry_count, created_at "
            "FROM pending_ops WHERE status = 'pending' ORDER BY id LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            QueuedOperation(
                id=r[0],
                method=r[1],
                args_json=r[2],
                kwargs_json=r[3],
                payload_ref=r[4],
                retry_count=r[5],
                created_at=r[6],
            )
            for r in rows
        ]

    async def mark_done(self, op_id: int) -> None:
        """Mark an operation as successfully replayed."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        await self._db.execute("UPDATE pending_ops SET status = 'done' WHERE id = ?", (op_id,))
        await self._db.commit()

    async def mark_failed(self, op_id: int) -> None:
        """Increment retry count; dead-letter if max retries exceeded."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        await self._db.execute(
            "UPDATE pending_ops SET retry_count = retry_count + 1 WHERE id = ?",
            (op_id,),
        )
        await self._db.execute(
            "UPDATE pending_ops SET status = 'dead_letter' "
            "WHERE id = ? AND retry_count >= max_retries",
            (op_id,),
        )
        await self._db.commit()

    async def mark_dead_letter(self, op_id: int) -> None:
        """Explicitly move an operation to the dead-letter status."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        await self._db.execute(
            "UPDATE pending_ops SET status = 'dead_letter' WHERE id = ?", (op_id,)
        )
        await self._db.commit()

    async def pending_count(self) -> int:
        """Return the number of pending operations."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        cursor = await self._db.execute("SELECT COUNT(*) FROM pending_ops WHERE status = 'pending'")
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError("COUNT(*) query returned None")
        return row[0]  # type: ignore[no-any-return]

    async def cleanup_completed(self, older_than_seconds: float = 3600) -> int:
        """Delete completed operations older than *older_than_seconds*."""
        if self._db is None:
            raise RuntimeError("Database not initialized. Call initialize() first")
        cutoff = time.time() - older_than_seconds
        cursor = await self._db.execute(
            "DELETE FROM pending_ops WHERE status = 'done' AND created_at < ?",
            (cutoff,),
        )
        await self._db.commit()
        return cursor.rowcount

    async def close(self) -> None:
        """Close the database connection."""
        if self._db is not None:
            await self._db.close()
            self._db = None
