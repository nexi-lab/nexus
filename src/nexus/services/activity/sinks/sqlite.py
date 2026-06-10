"""Append-only SQLite sink for activity events, segmented per UTC day.

Single-writer connection (``check_same_thread=False`` because writes are
dispatched via ``asyncio.to_thread`` so the executor thread, not the loop
thread, owns each ``executemany`` call). Caller is the activity worker,
which serializes calls — there is never more than one writer thread at a
time. Keeping I/O off the loop ensures a SQLite busy-wait cannot stall
unrelated request handlers.

Issue #4336: a single ever-growing activity.db made disk reclaim
impossible (DELETE never shrinks the file; VACUUM needs ~db-size free
space). The sink therefore writes to per-day segment files
(``<segment_dir>/activity-YYYY-MM-DD.db``) that retention deletes whole —
O(1) reclaim, no VACUUM. ``journal_size_limit`` plus a periodic
``wal_checkpoint(TRUNCATE)`` (run on the writer's own executor thread, so
it never contends with a concurrent writer) keep the WAL bounded. Under
disk pressure the sink sheds batches instead of filling the volume that
user file payloads share.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import time
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

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
    # #4336: bound the WAL file size — checkpoints (the periodic TRUNCATE in
    # _maybe_checkpoint plus SQLite's automatic ones) truncate the file back
    # under this limit instead of leaving a multi-GB WAL behind.
    "PRAGMA journal_size_limit=67108864",
)

_INSERT_SQL = (
    "INSERT OR IGNORE INTO activity_events "
    "(id, ts, kind, result, latency_ms, trace_id, "
    " actor_token_hash, actor_agent, actor_user, "
    " subject_zone, subject_extra, meta) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

# How often the (cheap but syscall-bound) statvfs runs at most.
_FREE_CHECK_INTERVAL_S = 10.0


def segment_path(segment_dir: Path, day: date) -> Path:
    """Canonical segment file path for a UTC day (retention matches by glob)."""
    return segment_dir / f"activity-{day.isoformat()}.db"


class SQLiteSink:
    """Durable append-only sink over per-day segments. Bootstrap idempotent."""

    def __init__(
        self,
        *,
        segment_dir: Path | str,
        min_free_bytes: int = 0,
        checkpoint_interval_s: float = 300.0,
        now_fn: Callable[[], datetime] | None = None,
        disk_usage_fn: Callable[[Path], Any] | None = None,
    ) -> None:
        self._segment_dir = Path(segment_dir)
        self._segment_dir.mkdir(parents=True, exist_ok=True)
        self._min_free_bytes = min_free_bytes
        self._checkpoint_interval_s = checkpoint_interval_s
        self._now_fn = now_fn or (lambda: datetime.now(tz=UTC))
        self._disk_usage_fn = disk_usage_fn or shutil.disk_usage
        self._conn: sqlite3.Connection | None = None
        self._segment_date: date | None = None
        self._last_checkpoint = time.monotonic()
        self._free_cached: int | None = None
        self._free_checked_at: float | None = None
        self._shedding = False
        self._closed = False
        # Eager open keeps lifespan's NoopSink-fallback contract: a broken
        # store directory fails construction, not the first write.
        self._open_segment(self._today())

    def _today(self) -> date:
        return self._now_fn().date()

    def _open_segment(self, day: date) -> None:
        path = segment_path(self._segment_dir, day)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            for pragma in _PRAGMAS:
                conn.execute(pragma)
            conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                conn.execute(stmt)
        except sqlite3.Error:
            conn.close()
            raise
        self._conn = conn
        self._segment_date = day
        logger.info("activity segment open at %s", path)

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
        # Shield the executor write from task cancellation: the to_thread
        # future cannot stop the underlying thread, so cancelling here would
        # leave a partial executemany running while shutdown closes the same
        # connection in close(). asyncio.shield ensures the future completes
        # before the awaiter is cancelled.
        try:
            await asyncio.shield(asyncio.to_thread(self._write_rows, rows))
        except sqlite3.Error:
            logger.warning("activity SQLiteSink batch insert failed", exc_info=True)
            raise

    def _write_rows(self, rows: Sequence[tuple[object, ...]]) -> None:
        """Executor-thread write path: closed check → shed check → rollover → insert.

        A failed segment open propagates: the worker counts it in
        ACTIVITY_SINK_ERRORS and the next batch retries the open, so the
        worker itself never crashes.
        """
        if self._closed:
            # SinkProtocol: write_batch must be safe concurrently with
            # close(). A straggler executor write fails fast here instead
            # of reopening the segment close() just released.
            raise sqlite3.OperationalError("activity SQLiteSink is closed")
        if self._shed(len(rows)):
            return
        today = self._today()
        if self._conn is None or today != self._segment_date:
            self._rollover(today)
        conn = self._conn
        if conn is None:  # pragma: no cover - _rollover either set it or raised
            raise sqlite3.OperationalError("activity segment connection unavailable")
        conn.executemany(_INSERT_SQL, rows)
        self._maybe_checkpoint(conn)

    def _rollover(self, day: date) -> None:
        if self._conn is not None:
            old = self._segment_date
            try:
                # Closing the last connection checkpoints and removes the
                # old segment's WAL — closed segments carry no WAL bytes.
                self._conn.close()
            except sqlite3.Error:
                logger.warning("activity segment close failed for %s", old, exc_info=True)
            self._conn = None
            self._segment_date = None
        self._open_segment(day)

    async def maintain(self) -> None:
        """Idle housekeeping: release yesterday's segment handle.

        Retention unlinks expired segment files, but on POSIX the disk
        blocks survive while this sink still holds the previous day's
        connection — a fully idle process would pin them indefinitely.
        The worker calls this on idle ticks; rolling over to today's
        segment closes the stale handle and frees the space.
        """
        # Same shield rationale as write_batch: the to_thread future cannot
        # stop the underlying thread, so a cancelled awaiter must not leave
        # a half-finished rollover racing close().
        await asyncio.shield(asyncio.to_thread(self._maintain))

    def _maintain(self) -> None:
        if self._closed or self._conn is None:
            return
        today = self._today()
        if today != self._segment_date:
            self._rollover(today)

    def _maybe_checkpoint(self, conn: sqlite3.Connection) -> None:
        now = time.monotonic()
        if now - self._last_checkpoint < self._checkpoint_interval_s:
            return
        self._last_checkpoint = now
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            logger.debug("activity WAL checkpoint failed", exc_info=True)
            return
        # A busy checkpoint reports through the result row rather than
        # raising. With this sink's single connection it should never be
        # busy — surface it for debugging if that invariant breaks.
        if row is not None and row[0]:
            logger.debug("activity WAL checkpoint busy: %s", row)

    def _shed(self, n: int) -> bool:
        """True when the batch must be dropped to protect the shared volume."""
        # Run the (cached) free-space probe unconditionally so the
        # disk-free gauge stays fresh even with shedding disabled.
        free = self._free_bytes()
        if self._min_free_bytes <= 0:
            return False
        if free is None:
            return False  # cannot stat → never shed on monitoring failure
        if free >= self._min_free_bytes:
            if self._shedding:
                self._shedding = False
                logger.info(
                    "activity disk pressure cleared (%d bytes free); resuming writes",
                    free,
                )
            return False
        if not self._shedding:
            self._shedding = True
            logger.error(
                "activity disk pressure: %d bytes free < %d minimum — "
                "shedding activity events to protect user file writes",
                free,
                self._min_free_bytes,
            )
        try:
            from nexus.services.activity.metrics import ACTIVITY_SHED

            ACTIVITY_SHED.inc(n)
        except Exception:
            pass
        return True

    def _free_bytes(self) -> int | None:
        now = time.monotonic()
        if (
            self._free_checked_at is not None
            and now - self._free_checked_at < _FREE_CHECK_INTERVAL_S
        ):
            return self._free_cached
        self._free_checked_at = now
        try:
            free = int(self._disk_usage_fn(self._segment_dir).free)
        except OSError:
            logger.debug("activity disk_usage failed", exc_info=True)
            self._free_cached = None
            return None
        self._free_cached = free
        try:
            from nexus.services.activity.metrics import ACTIVITY_DISK_FREE_BYTES

            ACTIVITY_DISK_FREE_BYTES.set(free)
        except Exception:
            pass
        return free

    async def close(self) -> None:
        self._closed = True
        conn = self._conn
        self._conn = None
        self._segment_date = None
        if conn is None:
            return
        try:
            await asyncio.to_thread(conn.close)
        except sqlite3.Error:
            logger.warning("activity SQLiteSink close failed", exc_info=True)
