"""Append-only SQLite sink for activity events.

Single-writer connection (``check_same_thread=False`` to allow the worker
to await ``write_batch`` while the connection lives on a different thread
in tests). Caller is the activity worker, which serializes calls.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from nexus.services.activity.events import ActivityEvent

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS activity_events (
    id              TEXT PRIMARY KEY,
    ts              TEXT NOT NULL,
    kind            TEXT NOT NULL,
    result          TEXT NOT NULL,
    latency_ms      INTEGER,
    trace_id        TEXT,
    actor_token_hash TEXT,
    actor_agent     TEXT,
    actor_user      TEXT,
    subject_zone    TEXT,
    subject_extra   TEXT,
    meta            TEXT
) STRICT;
"""

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ae_ts        ON activity_events(ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_kind_ts   ON activity_events(kind, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_token_ts  ON activity_events(actor_token_hash, ts)",
    "CREATE INDEX IF NOT EXISTS idx_ae_zone_ts   ON activity_events(subject_zone, ts)",
)

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA temp_store=MEMORY",
    "PRAGMA busy_timeout=5000",
)


class SQLiteSink:
    """Durable append-only sink. Schema bootstrap is idempotent."""

    def __init__(self, *, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, check_same_thread=False, isolation_level=None)
        try:
            for pragma in _PRAGMAS:
                self._conn.execute(pragma)
            self._conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                self._conn.execute(stmt)
        except sqlite3.Error:
            self._conn.close()
            raise

    async def write_batch(self, events: Sequence[ActivityEvent]) -> None:
        if not events:
            return
        rows = [
            (
                e.id,
                e.ts,
                e.kind.value,
                e.result.value,
                e.latency_ms,
                e.trace_id,
                e.actor.token_hash,
                e.actor.agent,
                e.actor.user,
                e.subject.zone,
                json.dumps(e.subject.extra) if e.subject.extra is not None else None,
                json.dumps(e.meta) if e.meta is not None else None,
            )
            for e in events
        ]
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO activity_events "
                "(id, ts, kind, result, latency_ms, trace_id, "
                " actor_token_hash, actor_agent, actor_user, "
                " subject_zone, subject_extra, meta) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        except sqlite3.Error:
            logger.warning("activity SQLiteSink batch insert failed", exc_info=True)
            raise

    async def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            logger.warning("activity SQLiteSink close failed", exc_info=True)
