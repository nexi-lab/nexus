"""TTL cleanup worker for expired transactional snapshots (Issue #1752).

Async background task that periodically sweeps for expired transactions
and rolls them back. Follows the EventDeliveryWorker pattern.

Configuration:
    - sweep_interval: seconds between sweeps (default 300 = 5 min)
    - batch_limit: max transactions per sweep (default 100)
"""

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.services.snapshot.service import TransactionalSnapshotService

logger = logging.getLogger(__name__)

class SnapshotCleanupWorker:
    """Background worker that cleans up expired transactions.

    Runs as an asyncio task, sweeping every ``sweep_interval`` seconds.
    Each sweep processes up to ``batch_limit`` expired transactions.
    """

    __slots__ = ("_batch_limit", "_service", "_sweep_interval", "_task")

    def __init__(
        self,
        snapshot_service: "TransactionalSnapshotService",
        sweep_interval: float = 300.0,
        batch_limit: int = 100,
    ) -> None:
        self._service = snapshot_service
        self._sweep_interval = sweep_interval
        self._batch_limit = batch_limit
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the cleanup worker (idempotent)."""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="snapshot-cleanup-worker")
        logger.info(
            "Snapshot cleanup worker started (interval=%.0fs, batch=%d)",
            self._sweep_interval,
            self._batch_limit,
        )

    async def stop(self) -> None:
        """Stop the cleanup worker (idempotent)."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("Snapshot cleanup worker stopped")

    @property
    def is_running(self) -> bool:
        """Whether the worker task is currently running."""
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        """Main loop: sleep -> cleanup -> yield between batches."""
        while True:
            try:
                await asyncio.sleep(self._sweep_interval)
                cleaned = await self._service.cleanup_expired(limit=self._batch_limit)
                if cleaned > 0:
                    logger.debug("Snapshot cleanup sweep: %d transactions expired", cleaned)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Snapshot cleanup sweep failed")
