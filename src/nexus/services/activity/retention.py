"""Retention for the segmented activity store: delete expired segment files.

Issue #4336: DELETE-based pruning never returned disk (SQLite keeps pages
until VACUUM, and VACUUM needs ~db-size free space — impossible once the
db passes ~50% of the volume). Per-day segment files flip that: a whole
expired day is reclaimed with one unlink, O(1) and VACUUM-free.

The legacy single-file ``activity.db`` from pre-segment deployments is
frozen (nothing writes to it anymore) and unlinked once its newest row is
older than the retention cutoff — the same data-loss semantics the old
DELETE prune already enforced, without DELETE/WAL churn on a potentially
huge file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import sqlite3
from collections.abc import Callable, MutableMapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from nexus.services.activity.agent_log_store import MemoryBackend

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^activity-(\d{4}-\d{2}-\d{2})\.db$")


def sweep_agent_log(
    store: MemoryBackend, *, retention_days: int, now: datetime | None = None
) -> int:
    """Drop (agent, date) buffers older than `retention_days`.

    Returns the count of date keys dropped. Idempotent. retention_days <= 0
    is treated as "retention disabled" — no keys are dropped.
    """
    if retention_days <= 0:
        return 0
    n = now or datetime.now(tz=UTC)
    cutoff = (n.date() - timedelta(days=retention_days)).isoformat()
    # Snapshot the dates to avoid mutating during iteration.
    dates = store.iter_dates()
    dropped = 0
    for day_key in dates:
        if day_key < cutoff:
            store.drop_date(day_key)
            dropped += 1
    return dropped


def _unlink_db_files(db: Path) -> None:
    """Remove a SQLite db plus its -wal/-shm siblings, ignoring absences.

    Siblings go first: if one of their unlinks fails, the .db file is
    still present as the retry key for the next sweep tick — the reverse
    order would orphan a -wal forever.
    """
    for path in (Path(f"{db}-wal"), Path(f"{db}-shm"), db):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _legacy_expired(db: Path, cutoff_iso: str) -> tuple[bool, str | None]:
    """(expired, max_ts) for the legacy db.

    Missing table and empty table both count as expired (nothing left to
    retain). Locked/corrupt files return (False, None) so the sweep retries
    on a later tick instead of deleting data it could not inspect.
    """
    try:
        # db.resolve().as_uri() percent-encodes '#'/'%'/spaces — a raw
        # f"file:{db}" would truncate at '#' and open the wrong path in
        # create mode, misreading a live legacy db as expired.
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT max(ts) FROM activity_events").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        if isinstance(exc, sqlite3.OperationalError) and (
            "no such table: activity_events" in str(exc).lower()
        ):
            return True, None
        logger.warning("activity legacy db %s unreadable; skipping", db, exc_info=True)
        return False, None
    max_ts = row[0] if row else None
    # ts is ISO-8601 UTC (see emitter._now_iso), so lexicographic
    # comparison matches chronological order.
    return (max_ts is None or max_ts < cutoff_iso), max_ts


def _legacy_expired_cached(
    db: Path, cutoff_iso: str, cache: MutableMapping[str, str] | None
) -> bool:
    """_legacy_expired with a max(ts) cache for the frozen legacy db.

    The legacy db never receives writes after the segment store boots, so a
    successfully-read max(ts) is final. Caching it spares later ticks the
    read-only probe — which replays full WAL recovery (potentially tens of
    GB after a disk-full crash) on every open of a frozen file. A manually
    replaced file is only re-read after a restart; the worst case is
    over-retention of that replacement, never early deletion.
    """
    if cache is not None:
        cached = cache.get(str(db))
        if cached is not None:
            return cached < cutoff_iso
    expired, max_ts = _legacy_expired(db, cutoff_iso)
    if cache is not None and not expired and max_ts is not None:
        cache[str(db)] = max_ts
    return expired


def _update_store_bytes(seg_dir: Path, legacy: Path | None) -> None:
    """Refresh the store-size gauge from on-disk segment + legacy file sizes."""
    total = 0
    try:
        if seg_dir.is_dir():
            for path in seg_dir.glob("activity-*.db*"):
                with contextlib.suppress(OSError):
                    total += path.stat().st_size
        if legacy is not None:
            for path in (legacy, Path(f"{legacy}-wal"), Path(f"{legacy}-shm")):
                with contextlib.suppress(OSError):
                    if path.is_file():
                        total += path.stat().st_size
        from nexus.services.activity.metrics import ACTIVITY_STORE_BYTES

        ACTIVITY_STORE_BYTES.set(total)
    except Exception:  # metrics must never break retention
        pass


def sweep_expired(
    *,
    segment_dir: Path | str,
    legacy_db_path: Path | str | None,
    retention_days: int,
    now_fn: Callable[[], datetime] | None = None,
    legacy_probe_cache: MutableMapping[str, str] | None = None,
) -> int:
    """Delete expired segment files (and the legacy db once fully stale).

    Returns the number of database files unlinked. ``retention_days <= 0``
    disables retention entirely.

    A segment ``activity-D.db`` holds rows up to ``D 23:59:59``, so it is
    deleted only when ``D < (now - retention_days).date()`` — i.e. when its
    newest possible row has expired. Worst-case over-retention is <24h
    (the old DELETE prune was exact-to-the-hour; acceptable coarsening).

    An idle sink may still hold yesterday's (expired) segment open when the
    sweep unlinks it — safe on POSIX: the data is already expired, the open
    handle keeps working against the unlinked inode, and the sink's next
    write rolls over to a fresh segment.
    """
    if retention_days <= 0:
        return 0
    now = (now_fn or (lambda: datetime.now(tz=UTC)))()
    cutoff = now - timedelta(days=retention_days)
    cutoff_date = cutoff.date()
    seg_dir = Path(segment_dir)
    deleted = 0

    if seg_dir.is_dir():
        for path in sorted(seg_dir.glob("activity-*.db")):
            match = _SEGMENT_RE.match(path.name)
            if match is None:
                continue
            try:
                seg_date = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if seg_date >= cutoff_date:
                continue
            try:
                _unlink_db_files(path)
                deleted += 1
            except OSError:
                logger.error("activity segment unlink failed for %s", path, exc_info=True)

    legacy = Path(legacy_db_path) if legacy_db_path is not None else None
    if (
        legacy is not None
        and legacy.is_file()
        and _legacy_expired_cached(legacy, cutoff.isoformat(), legacy_probe_cache)
    ):
        try:
            _unlink_db_files(legacy)
            deleted += 1
            logger.info("activity legacy db %s fully expired; deleted", legacy)
        except OSError:
            logger.error("activity legacy db unlink failed for %s", legacy, exc_info=True)

    if deleted:
        try:
            from nexus.services.activity.metrics import ACTIVITY_SEGMENTS_DELETED

            ACTIVITY_SEGMENTS_DELETED.inc(deleted)
        except Exception:
            pass
    _update_store_bytes(seg_dir, legacy)
    return deleted


class RetentionTask:
    """Async task wrapping sweep_expired on a fixed cadence."""

    def __init__(
        self,
        *,
        segment_dir: Path | str,
        legacy_db_path: Path | str | None,
        retention_days: int,
        interval_s: float = 3600.0,
    ) -> None:
        self._segment_dir = segment_dir
        self._legacy_db_path = legacy_db_path
        self._retention_days = retention_days
        self._interval_s = interval_s
        self._stopping = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._total_deleted = 0
        # max(ts) of the frozen legacy db, learned on the first successful
        # probe — see _legacy_expired_cached.
        self._legacy_probe_cache: dict[str, str] = {}

    @property
    def total_deleted(self) -> int:
        return self._total_deleted

    async def start(self) -> None:
        if self._retention_days <= 0:
            logger.info("activity retention disabled (retention_days=%d)", self._retention_days)
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Wait for the in-flight sweep (if any) to finish, then exit.

        Cancellation cannot stop the executor thread mid-sweep, so a cancel
        here would let the thread keep unlinking files after stop()
        returns. Setting the stopping flag is sufficient: ``_run`` checks
        the flag between iterations and waits on it instead of sleeping, so
        the loop exits as soon as the current sweep finishes.
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
                    sweep_expired,
                    segment_dir=self._segment_dir,
                    legacy_db_path=self._legacy_db_path,
                    retention_days=self._retention_days,
                    legacy_probe_cache=self._legacy_probe_cache,
                )
                self._total_deleted += deleted
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
                    dropped = sweep_agent_log(store, retention_days=retention_days)
                    self._total_deleted += dropped
            except Exception:
                logger.warning("agent_log retention sweep failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue
