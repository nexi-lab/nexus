"""Background reconciler — detects and resolves cache/SSOT drift.

Periodically compares the redb cache (RaftMetadataStore) with the SQL SSOT
(SqlMetadataStore) to find and fix inconsistencies such as:
- Stale cache entries (file deleted in SQL but still in redb)
- Orphaned records (file in SQL but missing from redb cache)
- Field mismatches (size, etag, version divergence)

Architecture (Issue #1246, Phase 4.4):
    SQL (PostgreSQL) = SSOT for file metadata
    redb (RaftMetadataStore) = cache for fast reads + locks + revisions
    Reconciler = background thread that detects drift and repairs it

Usage::

    from nexus.storage.reconciler import Reconciler

    reconciler = Reconciler(
        sql_store=sql_metadata_store,
        raft_store=raft_metadata_store,
        interval_seconds=60,
    )
    reconciler.start()
    # ... app runs ...
    reconciler.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadataProtocol

logger = logging.getLogger(__name__)


@dataclass
class ReconciliationStats:
    """Statistics from a single reconciliation run."""

    stale_cache_entries: int = 0
    orphaned_sql_entries: int = 0
    field_mismatches: int = 0
    repairs_applied: int = 0
    errors: int = 0
    duration_ms: float = 0
    details: list[str] = field(default_factory=list)


class Reconciler:
    """Background reconciler for redb cache vs SQL SSOT consistency.

    Runs a periodic comparison loop in a daemon thread. When inconsistencies
    are found, repairs are applied (SQL is always authoritative).

    The reconciler is intentionally conservative:
    - Only reads from both stores and compares
    - Never deletes from SQL (SSOT)
    - Only updates/removes stale entries from redb cache
    - Logs all actions for monitoring

    Args:
        sql_store: SqlMetadataStore (SQL SSOT for file metadata)
        raft_store: RaftMetadataStore (redb cache + locks)
        interval_seconds: How often to run reconciliation (default: 60)
        prefix: Optional path prefix to scope reconciliation
        batch_size: Number of entries to compare per batch (default: 500)
    """

    def __init__(
        self,
        sql_store: FileMetadataProtocol,
        raft_store: Any | None = None,
        *,
        interval_seconds: float = 60,
        prefix: str = "",
        batch_size: int = 500,
    ) -> None:
        self._sql_store = sql_store
        self._raft_store = raft_store
        self._interval = interval_seconds
        self._prefix = prefix
        self._batch_size = batch_size

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_stats: ReconciliationStats | None = None

    @property
    def last_stats(self) -> ReconciliationStats | None:
        """Get statistics from the most recent reconciliation run."""
        return self._last_stats

    def start(self) -> None:
        """Start the background reconciliation thread."""
        if self._thread is not None and self._thread.is_alive():
            logger.warning("Reconciler already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="nexus-reconciler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "Reconciler started (interval=%ds, prefix=%r)",
            self._interval,
            self._prefix,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the background reconciliation thread.

        Args:
            timeout: Maximum seconds to wait for thread to stop
        """
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
        logger.info("Reconciler stopped")

    def reconcile_once(self) -> ReconciliationStats:
        """Run a single reconciliation pass.

        Can be called directly for testing or manual reconciliation.

        Returns:
            ReconciliationStats with results
        """
        start = time.monotonic()
        stats = ReconciliationStats()

        if self._raft_store is None:
            stats.details.append("No raft_store configured — skipping reconciliation")
            return stats

        try:
            self._reconcile(stats)
        except Exception as e:
            stats.errors += 1
            stats.details.append(f"Reconciliation error: {e}")
            logger.error("Reconciliation failed: %s", e, exc_info=True)

        stats.duration_ms = (time.monotonic() - start) * 1000
        self._last_stats = stats

        if stats.repairs_applied > 0 or stats.errors > 0:
            logger.info(
                "Reconciliation complete: stale=%d, orphaned=%d, mismatches=%d, "
                "repairs=%d, errors=%d, duration=%.1fms",
                stats.stale_cache_entries,
                stats.orphaned_sql_entries,
                stats.field_mismatches,
                stats.repairs_applied,
                stats.errors,
                stats.duration_ms,
            )
        else:
            logger.debug("Reconciliation complete: no drift detected (%.1fms)", stats.duration_ms)

        return stats

    def _run_loop(self) -> None:
        """Background loop that runs reconciliation at regular intervals."""
        # Initial delay to let the system stabilize after startup
        if self._stop_event.wait(timeout=min(self._interval, 10)):
            return

        while not self._stop_event.is_set():
            self.reconcile_once()
            if self._stop_event.wait(timeout=self._interval):
                break

    def _reconcile(self, stats: ReconciliationStats) -> None:
        """Compare SQL SSOT with redb cache and repair drift.

        Strategy:
        1. List all entries in SQL (SSOT) for the prefix
        2. For each SQL entry, check if redb cache matches
        3. List all entries in redb cache for the prefix
        4. Find stale entries (in redb but not in SQL)
        """
        # Step 1: Get SQL entries (SSOT)
        sql_entries = self._sql_store.list(prefix=self._prefix, recursive=True)
        sql_paths = {e.path for e in sql_entries}
        sql_map = {e.path: e for e in sql_entries}

        # Step 2: Get redb cache entries
        raft_entries = self._raft_store.list(prefix=self._prefix, recursive=True)

        # Filter out system paths and extended metadata keys
        raft_entries = [
            e for e in raft_entries
            if not e.path.startswith("/__sys__/") and not e.path.startswith("meta:")
        ]
        raft_paths = {e.path for e in raft_entries}
        raft_map = {e.path: e for e in raft_entries}

        # Step 3: Find stale cache entries (in redb but not in SQL)
        stale_paths = raft_paths - sql_paths
        for path in stale_paths:
            stats.stale_cache_entries += 1
            stats.details.append(f"stale cache: {path}")
            try:
                self._raft_store.delete(path)
                stats.repairs_applied += 1
            except Exception as e:
                stats.errors += 1
                stats.details.append(f"failed to remove stale {path}: {e}")

        # Step 4: Find orphaned SQL entries (in SQL but not in redb cache)
        # These are fine — SQL is SSOT, cache misses just mean slower reads.
        # We log them but don't repair (cache will be populated on next read).
        orphaned_paths = sql_paths - raft_paths
        stats.orphaned_sql_entries = len(orphaned_paths)

        # Step 5: Find field mismatches (both exist but differ)
        common_paths = sql_paths & raft_paths
        for path in common_paths:
            sql_meta = sql_map[path]
            raft_meta = raft_map[path]

            mismatches = []
            if sql_meta.etag != raft_meta.etag:
                mismatches.append(f"etag: sql={sql_meta.etag} raft={raft_meta.etag}")
            if sql_meta.size != raft_meta.size:
                mismatches.append(f"size: sql={sql_meta.size} raft={raft_meta.size}")

            if mismatches:
                stats.field_mismatches += 1
                mismatch_str = ", ".join(mismatches)
                stats.details.append(f"mismatch {path}: {mismatch_str}")
                # SQL is SSOT — update redb cache with SQL values
                try:
                    self._raft_store.put(sql_meta)
                    stats.repairs_applied += 1
                except Exception as e:
                    stats.errors += 1
                    stats.details.append(f"failed to repair {path}: {e}")
