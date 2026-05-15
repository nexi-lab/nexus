"""Volume compactor — background compaction for CAS volumes.

Periodically calls BlobPackLocalTransport.compact() to reclaim space
from deleted entries in sealed volumes. Compaction copies live entries
to a fresh volume and atomically deletes the old one.

Design:
    - Simple asyncio timer loop (same pattern as TTLVolumeSweeper).
    - Configurable concurrency (default 1) controls how many compact()
      calls can run simultaneously to avoid I/O storms.
    - Idempotent: safe to call compact() at any frequency.
    - Graceful shutdown: cancel + await.
    - Runs compact() via asyncio.to_thread() since the Rust engine
      releases the GIL during I/O-heavy compaction work.

Issue #3408: Volume compaction — reclaim space from deleted entries.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.transports.blob_pack_local_transport import BlobPackLocalTransport

logger = logging.getLogger(__name__)

# Default compaction interval in seconds (every 5 minutes as spec'd in Issue #3408).
DEFAULT_COMPACTION_INTERVAL = 300.0

# Default max concurrent compaction workers (Issue #3408: default 1).
DEFAULT_MAX_CONCURRENT = 1


class VolumeCompactor:
    """Background compactor for CAS volumes.

    Args:
        transport: BlobPackLocalTransport to compact.
        interval: Seconds between compaction cycles.
        max_concurrent: Max concurrent compact() calls (default 1).
            Controls I/O pressure — higher values compact faster but
            use more disk bandwidth.

    Usage::

        compactor = VolumeCompactor(transport, interval=300.0, max_concurrent=1)
        await compactor.start()
        ...
        await compactor.stop()
    """

    def __init__(
        self,
        transport: BlobPackLocalTransport,
        *,
        interval: float = DEFAULT_COMPACTION_INTERVAL,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    ) -> None:
        self._transport = transport
        self._interval = interval
        self._max_concurrent = max(1, max_concurrent)
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._semaphore: asyncio.Semaphore | None = None

    async def start(self) -> None:
        """Start the background compaction loop."""
        if self._running:
            return
        self._running = True
        self._semaphore = asyncio.Semaphore(self._max_concurrent)
        self._task = asyncio.create_task(self._compaction_loop())
        logger.info(
            "Volume compactor started (interval: %.0fs, max_concurrent: %d)",
            self._interval,
            self._max_concurrent,
        )

    async def stop(self) -> None:
        """Stop the background compaction loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("Volume compactor stopped")

    async def compact_once(self) -> tuple[int, int, int]:
        """Run a single compaction cycle.

        Respects the concurrency semaphore — blocks if max_concurrent
        compactions are already in progress.

        Delegates to the Rust engine via asyncio.to_thread() so the
        event loop is not blocked during I/O-heavy compaction.

        Returns:
            (volumes_compacted, blobs_moved, bytes_reclaimed)
        """
        sem = self._semaphore or asyncio.Semaphore(1)
        async with sem:
            try:
                result = await asyncio.to_thread(self._transport.compact)
                volumes, blobs, reclaimed = result
                if volumes > 0:
                    logger.info(
                        "Compaction: %d volumes compacted, %d blobs moved, %d bytes reclaimed",
                        volumes,
                        blobs,
                        reclaimed,
                    )
                return result
            except Exception:
                logger.exception("Volume compaction failed")
                return (0, 0, 0)

    async def _compaction_loop(self) -> None:
        """Main compaction loop — periodic timer."""
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                return

            if not self._running:
                return

            await self.compact_once()

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent
