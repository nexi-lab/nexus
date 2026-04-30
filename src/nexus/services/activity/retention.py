"""Periodic prune of activity_events older than the retention threshold."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


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
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
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
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
