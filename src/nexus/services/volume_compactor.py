"""Volume compactor — background compaction for CAS volumes.

Periodically calls VolumeLocalTransport.compact() to reclaim space
from deleted entries in sealed volumes. Compaction copies live entries
to a fresh volume and atomically deletes the old one.

Design:
    - Simple asyncio timer loop (same pattern as TTLVolumeSweeper).
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
    from nexus.backends.transports.volume_local_transport import VolumeLocalTransport

logger = logging.getLogger(__name__)

# Default compaction interval in seconds (every 5 minutes as spec'd in Issue #3408).
DEFAULT_COMPACTION_INTERVAL = 300.0


class VolumeCompactor:
    """Background compactor for CAS volumes.

    Usage::

        compactor = VolumeCompactor(transport, interval=300.0)
        await compactor.start()
        ...
        await compactor.stop()
    """

    def __init__(
        self,
        transport: VolumeLocalTransport,
        *,
        interval: float = DEFAULT_COMPACTION_INTERVAL,
    ) -> None:
        self._transport = transport
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background compaction loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._compaction_loop())
        logger.info("Volume compactor started (interval: %.0fs)", self._interval)

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

        Delegates to the Rust engine via asyncio.to_thread() so the
        event loop is not blocked during I/O-heavy compaction.

        Returns:
            (volumes_compacted, blobs_moved, bytes_reclaimed)
        """
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
