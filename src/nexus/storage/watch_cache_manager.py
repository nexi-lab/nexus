"""WatchCacheManager — Kubernetes Informer-style watch cache for metadata.

Polls ``MetastoreABC.drain_changes()`` on a configurable interval and routes
each change through ``ReadSetAwareCache.invalidate_for_write()`` — the SSOT
invalidation gateway.  This closes the staleness window for shared metadata
reads in multi-node / federation deployments where Raft replication updates
the local redb but the Python LRU cache is unaware.

Design choices:
    - Polling (not push): avoids GIL contention; Rust ring buffer is polled
      every 10 ms which is well within the 300 s TTL window.
    - ``drain_changes()`` is offloaded to a thread via ``asyncio.to_thread``
      to avoid blocking the event loop (Raft FFI calls may acquire a mutex).
    - Fire-and-forget error policy: same as ``PostMutationHook`` — a poll
      failure is logged but never crashes the task.
    - asyncio.Task lifecycle: same pattern as WriteBuffer / Scheduler.

Issue #2065.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.metadata_change import MetadataChange
    from nexus.core.metastore import MetastoreABC
    from nexus.storage.read_set_cache import ReadSetAwareCache

logger = logging.getLogger(__name__)


class WatchCacheManager:
    """Polls metastore for changes and invalidates the read-set cache.

    Constructor DI — no global state, fully testable.

    Args:
        metastore: Source of changes (``drain_changes()``).
        invalidation_gateway: SSOT cache invalidation
            (``ReadSetAwareCache.invalidate_for_write()``).
        poll_interval_ms: Polling interval in milliseconds (default 10).
        buffer_overflow_threshold: If a single poll returns this many
            changes, trigger a full cache clear (default 4096).
    """

    def __init__(
        self,
        metastore: MetastoreABC,
        invalidation_gateway: ReadSetAwareCache,
        *,
        poll_interval_ms: int = 10,
        buffer_overflow_threshold: int = 4096,
    ) -> None:
        self._metastore = metastore
        self._gateway = invalidation_gateway
        self._poll_interval_s = poll_interval_ms / 1000.0
        self._buffer_overflow_threshold = buffer_overflow_threshold

        self._last_revision: int = 0
        self._task: asyncio.Task[None] | None = None
        self._running = False

        # Stats
        self._stats: dict[str, int] = {
            "watch_polls": 0,
            "watch_empty_polls": 0,
            "watch_invalidations": 0,
            "watch_overflow_clears": 0,
            "watch_errors": 0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background poll loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="watch-cache-poll")
        logger.info(
            "WatchCacheManager started (poll_interval=%.1fms, overflow_threshold=%d)",
            self._poll_interval_s * 1000,
            self._buffer_overflow_threshold,
        )

    async def stop(self) -> None:
        """Cancel the poll task and wait for cleanup."""
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("WatchCacheManager stopped")

    def request_stop(self) -> None:
        """Signal the poll loop to exit (synchronous, safe from NexusFS.close).

        The loop will stop after its current sleep cycle.  The asyncio.Task
        may still be alive briefly but will not call ``drain_changes()`` again.
        """
        self._running = False

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Main poll loop — runs until cancelled."""
        while self._running:
            try:
                # Offload drain_changes to a thread to avoid blocking the
                # event loop if the underlying store (e.g. Raft FFI) holds
                # a mutex or does disk I/O.
                changes = await asyncio.to_thread(
                    self._metastore.drain_changes,
                    since_revision=self._last_revision,
                )
                self._process_changes(changes)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._stats["watch_errors"] += 1
                logger.exception("WatchCacheManager poll error (will retry)")
            await asyncio.sleep(self._poll_interval_s)

    def _poll_once(self) -> None:
        """Execute a single poll cycle (synchronous, for tests)."""
        changes = self._metastore.drain_changes(since_revision=self._last_revision)
        self._process_changes(changes)

    def _process_changes(self, changes: list[MetadataChange]) -> None:
        """Process a batch of changes — shared by async and sync paths."""
        self._stats["watch_polls"] += 1

        if not changes:
            self._stats["watch_empty_polls"] += 1
            return

        # Overflow detection — too many changes means the cache is too stale;
        # do a full clear instead of individual invalidations.
        if len(changes) >= self._buffer_overflow_threshold:
            self._stats["watch_overflow_clears"] += 1
            logger.warning(
                "WatchCacheManager: batch size %d >= threshold %d, clearing cache",
                len(changes),
                self._buffer_overflow_threshold,
            )
            self._gateway.clear()
            # Changes are revision-ordered, so last element is max.
            self._last_revision = changes[-1].revision
            return

        # Normal path: invalidate each changed path through the SSOT gateway.
        for change in changes:
            self._gateway.invalidate_for_write(
                change.path,
                change.revision,
                zone_id=change.zone_id,
            )
            self._stats["watch_invalidations"] += 1

        # Changes are revision-ordered, so last element is max.
        self._last_revision = changes[-1].revision

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return watch cache manager statistics."""
        return {
            **self._stats,
            "watch_last_revision": self._last_revision,
            "watch_running": self._running,
        }
