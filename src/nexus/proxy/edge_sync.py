"""Edge sync manager — reconnection state machine for split-brain resilience.

Orchestrates the reconnection sequence when an edge kernel comes back online:
  DISCONNECTED → RECONNECTING → AUTH_REFRESH → CONFLICT_SCAN → WAL_REPLAY → ONLINE

Dependencies are injected via constructor so the manager is testable in isolation.

Issue #1707: Edge split-brain resilience.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.proxy.auth_cache_manager import AuthCacheManager
    from nexus.proxy.circuit_breaker import AsyncCircuitBreaker
    from nexus.proxy.conflict_detector import ConflictDetector
    from nexus.proxy.queue_protocol import OfflineQueueProtocol
    from nexus.proxy.transport import HttpTransport

logger = logging.getLogger(__name__)


class SyncState(Enum):
    """States in the edge reconnection state machine."""

    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    AUTH_REFRESH = "auth_refresh"
    CONFLICT_SCAN = "conflict_scan"
    WAL_REPLAY = "wal_replay"
    ONLINE = "online"


class EdgeSyncManager:
    """Orchestrates edge-to-cloud reconnection with prioritized state machine.

    Parameters
    ----------
    queue:
        Offline queue for pending operations.
    transport:
        HTTP transport for cloud communication.
    circuit:
        Circuit breaker for connectivity detection.
    auth_manager:
        Auth cache manager for token refresh.
    conflict_detector:
        Conflict detector for split-brain resolution.
    health_check_url:
        Optional URL for connectivity health check.
    node_id:
        Identifier for this edge node.
    """

    def __init__(
        self,
        *,
        queue: "OfflineQueueProtocol",
        transport: "HttpTransport",
        circuit: "AsyncCircuitBreaker",
        auth_manager: "AuthCacheManager | None" = None,
        conflict_detector: "ConflictDetector | None" = None,
        health_check_url: str | None = None,
        node_id: str = "edge",
        replay_wake: Callable[[], None] | None = None,
    ) -> None:
        self._queue = queue
        self._transport = transport
        self._circuit = circuit
        self._auth_manager = auth_manager
        self._conflict_detector = conflict_detector
        self._health_check_url = health_check_url
        self._node_id = node_id
        self._replay_wake = replay_wake
        self._state = SyncState.ONLINE
        self._stopped = False
        self._reconnect_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> SyncState:
        """Current state of the sync manager."""
        return self._state

    def notify_disconnected(self) -> None:
        """Signal that connectivity has been lost."""
        if self._stopped:
            return
        if self._state is not SyncState.DISCONNECTED:
            self._state = SyncState.DISCONNECTED
            if self._auth_manager is not None:
                self._auth_manager.enter_offline_mode()
            logger.warning("Edge node %s disconnected from cloud", self._node_id)

    def notify_connected(self) -> None:
        """Signal that connectivity may have been restored.

        Triggers the reconnection state machine if currently disconnected.
        """
        if self._stopped:
            return
        if self._state is SyncState.DISCONNECTED:
            self._state = SyncState.RECONNECTING
            logger.info("Edge node %s starting reconnection sequence", self._node_id)
            # Cancel any lingering reconnect task before creating a new one
            if self._reconnect_task is not None and not self._reconnect_task.done():
                self._reconnect_task.cancel()
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Execute the prioritized reconnection sequence."""
        try:
            # Step 1: Health check
            if not await self._health_check():
                self._state = SyncState.DISCONNECTED
                return

            # Step 2: Auth refresh
            self._state = SyncState.AUTH_REFRESH
            await self._refresh_auth()

            # Step 3: Conflict scan
            self._state = SyncState.CONFLICT_SCAN
            await self._scan_conflicts()

            # Step 4: WAL replay
            self._state = SyncState.WAL_REPLAY
            await self._replay_wal()

            # Done
            self._state = SyncState.ONLINE
            logger.info("Edge node %s reconnection complete — now ONLINE", self._node_id)

        except asyncio.CancelledError:
            logger.info("Reconnection cancelled for node %s", self._node_id)
            raise
        except Exception:
            logger.exception("Reconnection failed for node %s", self._node_id)
            self._state = SyncState.DISCONNECTED

    async def _health_check(self) -> bool:
        """Verify cloud connectivity before proceeding."""
        if self._health_check_url is None:
            # No health check URL configured — assume connected if circuit allows
            return not self._circuit.is_open

        try:
            await self._transport.call(self._health_check_url, params={})
            logger.info("Health check passed for node %s", self._node_id)
            return True  # Any non-exception response = healthy
        except Exception:
            logger.warning("Health check failed for node %s", self._node_id)
            return False

    async def _refresh_auth(self) -> None:
        """Force-refresh auth tokens before any data operations."""
        if self._auth_manager is None:
            return

        self._auth_manager.exit_offline_mode()
        if self._auth_manager.needs_refresh:
            await self._auth_manager.force_refresh()
            logger.info("Auth tokens refreshed for node %s", self._node_id)

    async def _scan_conflicts(self) -> None:
        """Scan for conflicts between edge and cloud state.

        In the current implementation, conflict detection happens
        per-operation during replay. This method is a hook for
        future batch conflict scanning.
        """
        if self._conflict_detector is None:
            return

        pending = await self._queue.pending_count()
        logger.info(
            "Conflict scan for node %s: %d pending operations to check",
            self._node_id,
            pending,
        )

    async def _replay_wal(self) -> None:
        """Trigger WAL replay for pending operations.

        The actual replay is handled by ReplayEngine. This method
        ensures the queue is ready and wakes the replay engine.
        """
        pending = await self._queue.pending_count()
        if pending > 0:
            logger.info(
                "WAL replay starting for node %s: %d pending operations",
                self._node_id,
                pending,
            )
            if self._replay_wake is not None:
                self._replay_wake()
        else:
            logger.info("No pending operations for node %s — WAL replay skipped", self._node_id)

    async def start(self) -> None:
        """Start the edge sync manager."""
        self._stopped = False
        self._state = SyncState.ONLINE
        logger.info("EdgeSyncManager started for node %s", self._node_id)

    async def stop(self) -> None:
        """Stop the edge sync manager and cancel any pending reconnection."""
        self._stopped = True
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        logger.info("EdgeSyncManager stopped for node %s", self._node_id)

    def to_status_dict(self) -> dict[str, Any]:
        """Return status info for monitoring/debugging."""
        return {
            "node_id": self._node_id,
            "state": self._state.value,
            "auth_offline": self._auth_manager.is_offline if self._auth_manager else False,
            "auth_needs_refresh": self._auth_manager.needs_refresh if self._auth_manager else False,
        }
