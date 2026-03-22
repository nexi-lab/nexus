"""TTL expiry sweeper for filesystem-as-IPC.

Background task that moves expired messages to dead_letter/.

Issue #3197: Supports two sweep modes:
  1. Event-driven via CacheStore pub/sub (low-latency, targeted per-agent)
  2. Fallback periodic poll (safety net, full scan)

Events are debounced: rapid TTL schedule events are coalesced into a
single sweep after a short delay (default 2s).  The poll fallback runs
at a longer interval (default 300s) and scans ALL agent inboxes.
"""

import asyncio
import contextlib
import json
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nexus.bricks.ipc.conventions import AGENTS_ROOT, inbox_path
from nexus.bricks.ipc.envelope import MessageEnvelope
from nexus.bricks.ipc.exceptions import DLQReason
from nexus.bricks.ipc.lifecycle import dead_letter_message
from nexus.bricks.ipc.protocols import VFSOperations
from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

# Default sweep interval (fallback poll) in seconds.
# With event-driven sweeping enabled, this is the safety-net interval.
DEFAULT_SWEEP_INTERVAL = 60

# Default debounce delay for event-driven sweeps (seconds).
DEFAULT_DEBOUNCE_SECONDS = 2.0


class TTLSweeper:
    """Background sweeper that moves expired messages to dead_letter/.

    Operates in two modes (can be combined):

    **Event-driven** (when ``cache_store`` is provided):
      MessageSender publishes ``ipc:ttl:schedule:{zone_id}`` events when
      sending messages with TTLs.  The sweeper subscribes, debounces rapid
      events, and sweeps only the targeted agent's inbox.

    **Fallback poll** (always active):
      Periodic full scan of all agent inboxes.  Acts as a safety net for
      missed pub/sub events (subscriber disconnect, restart, etc.).

    Args:
        storage: Storage driver for IPC listing, reading, and renaming.
        zone_id: Zone ID for multi-tenant isolation.
        interval: Seconds between fallback poll cycles.
        cache_store: CacheStoreABC for event-driven TTL pub/sub. Optional.
        debounce_seconds: Delay before sweeping after a pub/sub event.
    """

    def __init__(
        self,
        storage: VFSOperations,
        zone_id: str = ROOT_ZONE_ID,
        interval: float = DEFAULT_SWEEP_INTERVAL,
        cache_store: "CacheStoreABC | None" = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._storage = storage
        self._zone_id = zone_id
        self._interval = interval
        self._running = False
        self._task: asyncio.Task[None] | None = None
        self._cache_store = cache_store
        self._debounce_seconds = debounce_seconds
        self._sub_task: asyncio.Task[None] | None = None
        self._pending_agents: set[str] = set()
        self._sweep_event = asyncio.Event()
        self._debounce_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the background sweep loop and optional pub/sub listener."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())

        if self._cache_store is not None:
            self._sub_task = asyncio.create_task(self._subscribe_loop())

        logger.info(
            "TTL sweeper started (poll_interval: %.0fs, event_driven: %s, debounce: %.1fs)",
            self._interval,
            self._cache_store is not None,
            self._debounce_seconds,
        )

    async def stop(self) -> None:
        """Stop the background sweep loop and pub/sub listener."""
        self._running = False

        if self._debounce_task is not None:
            self._debounce_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._debounce_task
            self._debounce_task = None

        if self._sub_task is not None:
            self._sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sub_task
            self._sub_task = None

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
            agent_ids = await self._storage.list_dir(AGENTS_ROOT, self._zone_id)
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

    # ------------------------------------------------------------------
    # Internal loops
    # ------------------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Main sweep loop — combines event-driven wakeup with periodic fallback.

        Waits for either:
          1. ``_sweep_event`` set by debounced pub/sub events -> targeted sweep
          2. Timeout (``_interval``) -> full scan (safety net)
        """
        while self._running:
            try:
                try:
                    await asyncio.wait_for(
                        self._sweep_event.wait(),
                        timeout=self._interval,
                    )
                    # Event-driven: targeted sweep of specific agents
                    self._sweep_event.clear()
                    agents_to_sweep = self._pending_agents.copy()
                    self._pending_agents.clear()
                    expired = 0
                    for agent_id in agents_to_sweep:
                        expired += await self._sweep_agent(agent_id)
                    if expired > 0:
                        logger.info(
                            "TTL event sweep: moved %d expired messages for %d agents",
                            expired,
                            len(agents_to_sweep),
                        )
                except TimeoutError:
                    # Fallback: full scan of all agent inboxes
                    await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.error("TTL sweep cycle failed", exc_info=True)

    async def _subscribe_loop(self) -> None:
        """Subscribe to CacheStore pub/sub for TTL schedule events.

        Receives events from MessageSender when messages with TTLs are
        created. Each event triggers a debounced targeted sweep.
        """
        if self._cache_store is None:
            return
        channel = f"ipc:ttl:schedule:{self._zone_id}"
        try:
            async with self._cache_store.subscribe(channel) as messages:
                async for msg in messages:
                    if not self._running:
                        break
                    try:
                        data = json.loads(msg)
                        agent_id = data.get("agent_id")
                        if agent_id:
                            self._pending_agents.add(agent_id)
                            self._schedule_debounced_sweep()
                    except Exception:
                        logger.debug("Invalid TTL schedule event", exc_info=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error(
                "TTL sweeper pub/sub listener crashed for zone %s",
                self._zone_id,
                exc_info=True,
            )

    def _schedule_debounced_sweep(self) -> None:
        """Schedule a sweep after debounce delay, resetting if already pending.

        Each new event cancels any pending debounce timer and starts a fresh
        one. The sweep fires only after ``debounce_seconds`` with no new events.
        """
        if self._debounce_task is not None:
            self._debounce_task.cancel()
        self._debounce_task = asyncio.create_task(self._debounced_sweep())

    async def _debounced_sweep(self) -> None:
        """Wait for debounce period, then signal the sweep loop."""
        try:
            await asyncio.sleep(self._debounce_seconds)
            self._sweep_event.set()
        except asyncio.CancelledError:
            pass  # Debounce was reset by a newer event

    # ------------------------------------------------------------------
    # Per-agent sweep
    # ------------------------------------------------------------------

    async def _sweep_agent(self, agent_id: str) -> int:
        """Sweep a single agent's inbox for expired messages."""
        agent_inbox = inbox_path(agent_id)
        expired = 0

        try:
            filenames = await self._storage.list_dir(agent_inbox, self._zone_id)
        except Exception:
            return 0

        now = datetime.now(UTC)
        for filename in filenames:
            if not filename.endswith(".json"):
                continue

            # P1: Skip recently-created messages based on filename timestamp.
            # Messages newer than the sweep interval can't have been sitting
            # long enough to matter — they'll be checked on the next cycle.
            if self._is_recent_by_filename(filename, now):
                continue

            msg_path = f"{agent_inbox}/{filename}"
            try:
                data = await self._storage.sys_read(msg_path, self._zone_id)
                envelope = MessageEnvelope.from_bytes(data)
                if envelope.is_expired():
                    await dead_letter_message(
                        self._storage,
                        msg_path,
                        agent_id,
                        self._zone_id,
                        DLQReason.TTL_EXPIRED,
                        msg_id=envelope.id,
                        timestamp=envelope.timestamp,
                        detail=f"TTL {envelope.ttl_seconds}s expired (sweeper)",
                    )
                    expired += 1
            except Exception:
                # Skip unreadable files — don't crash the sweep
                logger.debug(
                    "Skipping unreadable file during sweep: %s",
                    msg_path,
                )

        return expired

    def _is_recent_by_filename(self, filename: str, now: datetime) -> bool:
        """Check if a message is too recent to be expired based on filename.

        Parses the timestamp prefix from ``{YYYYMMDDTHHMMSS}_{msg_id}.json``.
        If the message was created less than ``interval`` seconds ago, skip it.
        Unparseable filenames are never skipped (conservative).
        """
        try:
            ts_str = filename.split("_", 1)[0]
            file_ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
            age_seconds = (now - file_ts).total_seconds()
            return age_seconds < self._interval
        except (ValueError, IndexError):
            return False
