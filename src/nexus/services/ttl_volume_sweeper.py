"""TTL volume sweeper — background expiry for TTL-bucketed CAS volumes.

Periodically calls BlobPackLocalTransport.expire_ttl_volumes() to remove
expired entries from the in-memory index and delete fully-expired volume
files. Also rotates TTL volumes at their configured intervals.

Three-phase loop:
  1. Expiry: Remove expired entries from volume index, delete empty volumes.
  2. Rotation: Seal active volumes that exceeded their rotation interval.
  3. Metastore cleanup: Batch-delete metastore entries for expired TTL content.

Design:
    - Simple asyncio timer loop (no event-driven mode — volume expiry is
      inherently periodic since entries have absolute timestamps).
    - Idempotent: safe to call expire/rotate at any frequency.
    - Graceful shutdown: cancel + await.

Issue #3405: Volume-level TTL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport
    from nexus.core.metastore import MetastoreABC

logger = logging.getLogger(__name__)

# Default sweep interval in seconds (every 10s as spec'd in Issue #3405).
DEFAULT_SWEEP_INTERVAL = 10.0


class TTLVolumeSweeper:
    """Background sweeper for TTL-bucketed CAS volumes.

    Usage::

        sweeper = TTLVolumeSweeper(transport, metastore=metastore, interval=10.0)
        await sweeper.start()
        ...
        await sweeper.stop()
    """

    def __init__(
        self,
        transport: BlobPackLocalTransport,
        *,
        metastore: MetastoreABC | None = None,
        interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        self._transport = transport
        self._metastore = metastore
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    def set_metastore(self, metastore: MetastoreABC) -> None:
        """Deferred injection — metastore may not be available at construction time."""
        self._metastore = metastore

    async def start(self) -> None:
        """Start the background sweep loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info("TTL volume sweeper started (interval: %.1fs)", self._interval)

    async def stop(self) -> None:
        """Stop the background sweep loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("TTL volume sweeper stopped")

    async def sweep_once(self) -> tuple[int, int]:
        """Run a single sweep + rotation cycle.

        Returns:
            (entries_expired, volumes_sealed)
        """
        entries_expired = 0
        volumes_sealed = 0

        # Phase 1: Expire entries and delete fully-expired volumes (mem_index: instant)
        try:
            results = self._transport.expire_ttl_volumes()
            entries_expired = sum(count for _, count in results)
        except Exception:
            logger.exception("TTL volume expiry failed")

        # Phase 1b: Deferred redb cleanup (background, non-blocking for reads)
        # Best-effort: startup recovery handles orphaned redb entries.
        with contextlib.suppress(Exception):
            self._transport.flush_expired_index()

        # Phase 2: Rotate volumes that exceeded their interval
        try:
            volumes_sealed = self._transport.rotate_ttl_volumes()
        except Exception:
            logger.exception("TTL volume rotation failed")

        # Phase 3: Clean up metastore entries for expired TTL content
        if self._metastore is not None:
            try:
                self._cleanup_metastore()
            except Exception:
                logger.exception("TTL metastore cleanup failed")

        if entries_expired > 0 or volumes_sealed > 0:
            logger.info(
                "TTL sweep: expired %d entries, sealed %d volumes",
                entries_expired,
                volumes_sealed,
            )

        return entries_expired, volumes_sealed

    def _cleanup_metastore(self) -> int:
        """Batch-delete metastore entries whose TTL has expired.

        Scans for entries with ttl_seconds > 0 where
        modified_at + ttl_seconds < now. These entries point to content
        that has been (or will be) expired from the volume index.

        Returns count of metastore entries deleted.
        """
        if self._metastore is None:
            return 0

        now = time.time()
        expired_paths: list[str] = []

        for meta in self._metastore.list_iter():
            if meta.ttl_seconds > 0 and meta.modified_at is not None:
                # modified_at is a datetime; convert to epoch for comparison
                modified_epoch = meta.modified_at.timestamp()
                if modified_epoch + meta.ttl_seconds < now:
                    expired_paths.append(meta.path)

        if expired_paths:
            self._metastore.delete_batch(expired_paths)
            logger.info("TTL metastore cleanup: deleted %d expired entries", len(expired_paths))

        return len(expired_paths)

    async def _sweep_loop(self) -> None:
        """Main sweep loop — periodic timer."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

            if not self._running:
                return

            await self.sweep_once()

    @property
    def is_running(self) -> bool:
        return self._running
