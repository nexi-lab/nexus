"""TTL expiry sweeper for filesystem-as-IPC.

Background task that periodically scans all agent inboxes for expired
messages and moves them to dead_letter/.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from nexus.ipc.conventions import AGENTS_ROOT, dead_letter_path, inbox_path
from nexus.ipc.envelope import MessageEnvelope
from nexus.ipc.protocols import VFSOperations

logger = logging.getLogger(__name__)

# Default sweep interval in seconds
DEFAULT_SWEEP_INTERVAL = 60


class TTLSweeper:
    """Background sweeper that moves expired messages to dead_letter/.

    Runs as an async task, scanning all agent inboxes at a configurable
    interval. Designed to be started once and run for the lifetime of
    the server process.

    Args:
        vfs: VFS operations for file listing, reading, and renaming.
        zone_id: Zone ID for multi-tenant isolation.
        interval: Seconds between sweep cycles.
    """

    def __init__(
        self,
        vfs: VFSOperations,
        zone_id: str = "default",
        interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        self._vfs = vfs
        self._zone_id = zone_id
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background sweep loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info("TTL sweeper started (interval: %.0fs)", self._interval)

    async def stop(self) -> None:
        """Stop the background sweep loop."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        logger.info("TTL sweeper stopped")

    async def sweep_once(self) -> int:
        """Run a single sweep cycle across all agent inboxes.

        Returns:
            Number of expired messages moved to dead_letter.
        """
        expired_count = 0
        try:
            agent_ids = await self._vfs.list_dir(AGENTS_ROOT, self._zone_id)
        except Exception:
            logger.debug("Cannot list %s for sweep", AGENTS_ROOT)
            return 0

        for agent_id in agent_ids:
            expired_count += await self._sweep_agent(agent_id)

        if expired_count > 0:
            logger.info(
                "TTL sweep: moved %d expired messages to dead_letter",
                expired_count,
            )
        return expired_count

    async def _sweep_loop(self) -> None:
        """Main sweep loop — runs until stop() is called."""
        while self._running:
            try:
                await self.sweep_once()
            except Exception:
                logger.error("TTL sweep cycle failed", exc_info=True)
            await asyncio.sleep(self._interval)

    async def _sweep_agent(self, agent_id: str) -> int:
        """Sweep a single agent's inbox for expired messages."""
        agent_inbox = inbox_path(agent_id)
        expired = 0

        try:
            filenames = await self._vfs.list_dir(agent_inbox, self._zone_id)
        except Exception:
            return 0

        for filename in filenames:
            if not filename.endswith(".json"):
                continue
            msg_path = f"{agent_inbox}/{filename}"
            try:
                data = await self._vfs.read(msg_path, self._zone_id)
                envelope = MessageEnvelope.from_bytes(data)
                if envelope.is_expired():
                    dest = f"{dead_letter_path(agent_id)}/{filename}"
                    await self._vfs.rename(msg_path, dest, self._zone_id)
                    expired += 1
                    logger.debug(
                        "Expired message %s moved to dead_letter for agent %s",
                        envelope.id,
                        agent_id,
                    )
            except Exception:
                # Skip unreadable files — don't crash the sweep
                logger.debug(
                    "Skipping unreadable file during sweep: %s",
                    msg_path,
                )

        return expired
