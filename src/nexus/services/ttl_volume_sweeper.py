"""TTL volume sweeper — background expiry for TTL-bucketed CAS volumes.

Periodically calls VolumeLocalTransport.expire_ttl_volumes() to remove
expired entries from the in-memory index and delete fully-expired volume
files. Also rotates TTL volumes at their configured intervals.

Two-phase loop:
  1. Expiry: Remove expired entries, delete empty volumes.
  2. Rotation: Seal active volumes that exceeded their rotation interval.

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.backends.transports.volume_local_transport import VolumeLocalTransport

logger = logging.getLogger(__name__)

# Default sweep interval in seconds (every 10s as spec'd in Issue #3405).
DEFAULT_SWEEP_INTERVAL = 10.0


class TTLVolumeSweeper:
    """Background sweeper for TTL-bucketed CAS volumes.

    Usage::

        sweeper = TTLVolumeSweeper(transport, interval=10.0)
        await sweeper.start()
        ...
        await sweeper.stop()
    """

    def __init__(
        self,
        transport: VolumeLocalTransport,
        *,
        interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        self._transport = transport
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

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

        if entries_expired > 0 or volumes_sealed > 0:
            logger.info(
                "TTL sweep: expired %d entries, sealed %d volumes",
                entries_expired,
                volumes_sealed,
            )

        return entries_expired, volumes_sealed

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
