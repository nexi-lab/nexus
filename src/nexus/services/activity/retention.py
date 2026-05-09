"""Periodic prune of activity_events older than the retention threshold."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from nexus.services.activity.agent_log_store import MemoryBackend

logger = logging.getLogger(__name__)


def sweep_agent_log(
    store: MemoryBackend, *, retention_days: int, now: datetime | None = None
) -> int:
    """Drop (agent, date) buffers older than `retention_days`.

    Returns the count of date keys dropped. Idempotent.
    """
    n = now or datetime.now(tz=UTC)
    cutoff = (n.date() - timedelta(days=retention_days)).isoformat()
    # Snapshot the dates to avoid mutating during iteration. Touching the
    # store internals here is a deliberate sibling-package access; if you
    # prefer, add a `MemoryBackend.iter_dates() -> set[str]` method.
    dates = list({k.date for k in store._buffers})  # noqa: SLF001
    dropped = 0
    for date in dates:
        if date < cutoff:
            store.drop_date(date)
            dropped += 1
    return dropped


def prune_older_than(
    *,
    db_path: Path | str,
    retention_days: int,
    vacuum_threshold: int = 1000,
) -> int:
    """Synchronously delete rows older than now - retention_days.

    ts is always YYYY-MM-DDTHH:MM:SS.ffffff+00:00 (see emitter._now_iso),
    so lexicographic comparison matches chronological order.

    Returns the number of rows deleted. retention_days <= 0 is a no-op.
    Missing DB returns 0 silently. Runs VACUUM after deleting more than
    vacuum_threshold rows (default 1000) to reclaim disk space.
    """
    if retention_days <= 0:
        return 0
    db = Path(db_path)
    if not db.exists():
        return 0
    threshold = (datetime.now(tz=UTC) - timedelta(days=retention_days)).isoformat()
    try:
        conn = sqlite3.connect(db, isolation_level=None)  # autocommit needed for VACUUM
        try:
            cursor = conn.execute(
                "DELETE FROM activity_events WHERE ts < ?",
                (threshold,),
            )
            deleted = cursor.rowcount or 0
            if deleted >= vacuum_threshold:
                try:
                    conn.execute("VACUUM")
                except sqlite3.Error:
                    logger.warning("activity retention VACUUM failed", exc_info=True)
            return deleted
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("activity retention prune failed", exc_info=True)
        return 0


class RetentionTask:
    """Async task wrapping prune_older_than on a fixed cadence."""

    def __init__(
        self,
        *,
        db_path: Path | str,
        retention_days: int,
        interval_s: float = 3600.0,
        vacuum_threshold: int = 1000,
    ) -> None:
        self._db_path = db_path
        self._retention_days = retention_days
        self._interval_s = interval_s
        self._vacuum_threshold = vacuum_threshold
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._total_pruned = 0

    @property
    def total_pruned(self) -> int:
        return self._total_pruned

    async def start(self) -> None:
        if self._retention_days <= 0:
            logger.info("activity retention disabled (retention_days=%d)", self._retention_days)
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Wait for the in-flight prune (if any) to finish, then exit.

        Cancellation cannot stop the executor thread mid-VACUUM, so a
        cancel here would let the thread keep holding the SQLite write
        lock after stop() returns — and the worker's final drain would
        then race that lock. Setting the stopping flag is sufficient:
        ``_run`` checks the flag between iterations and waits on it
        instead of sleeping, so the loop exits as soon as the current
        prune finishes.
        """
        self._stopping.set()
        if self._task is not None:
            with contextlib.suppress(Exception):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                deleted = await asyncio.to_thread(
                    prune_older_than,
                    db_path=self._db_path,
                    retention_days=self._retention_days,
                    vacuum_threshold=self._vacuum_threshold,
                )
                self._total_pruned += deleted
            except Exception:
                logger.warning("activity retention loop tick failed", exc_info=True)
            # Agent-log retention (RAM-only ring buffers, see issue #4081).
            try:
                from nexus.services.activity.lifespan import (
                    get_agent_log_retention_days,
                    get_agent_log_store,
                )

                store = get_agent_log_store()
                retention_days = get_agent_log_retention_days()
                if store is not None and isinstance(retention_days, int):
                    sweep_agent_log(store, retention_days=retention_days)
            except Exception:
                logger.warning("agent_log retention sweep failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
