"""Reactive Subscription Manager with Dependency Tracking (Issue #1167).

Replaces the O(C x P) linear scan in WebSocketManager.broadcast_to_zone()
with O(1) read-set-based overlap detection using ReadSetRegistry (#1166).

Architecture:
    - Subscription: Frozen dataclass representing a single subscription
    - ReactiveSubscriptionManager: Composes ReadSetRegistry for read-set mode

All subscriptions use read-set mode with O(1+d) lookup via ReadSetRegistry
reverse index.

Example:
    >>> from nexus.system_services.lifecycle.reactive_subscriptions import (
    ...     ReactiveSubscriptionManager, Subscription,
    ... )
    >>> from nexus.storage.read_set import ReadSet, ReadSetRegistry
    >>>
    >>> registry = ReadSetRegistry()
    >>> manager = ReactiveSubscriptionManager(registry=registry)
    >>>
    >>> # Register a read-set subscription
    >>> sub = Subscription(
    ...     subscription_id="sub1", connection_id="conn1",
    ...     zone_id="zone1", query_id="q1",
    ... )
    >>> rs = ReadSet(query_id="q1", zone_id="zone1")
    >>> rs.record_read("file", "/inbox/a.txt", revision=10)
    >>> await manager.register(sub, read_set=rs)
"""

import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nexus.storage.read_set import ReadSetRegistry

if TYPE_CHECKING:
    from nexus.storage.read_set import ReadSet
    from nexus.system_services.event_bus.types import FileEvent

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class Subscription:
    """A single reactive subscription binding a connection to event filtering.

    Frozen + slots for immutability and memory efficiency.

    Attributes:
        subscription_id: Unique identifier for this subscription
        connection_id: WebSocket connection this subscription belongs to
        zone_id: Zone for event filtering
        query_id: Links to ReadSetRegistry for O(1) lookup
        event_types: Event type filter, frozenset for O(1) lookup
        created_at: When this subscription was created (epoch seconds)
    """

    subscription_id: str
    connection_id: str
    zone_id: str
    query_id: str | None = None
    event_types: frozenset[str] = frozenset()
    created_at: float = field(default_factory=time.time)


class ReactiveSubscriptionManager:
    """Manages subscriptions with read-set-based event matching.

    Composes around ReadSetRegistry for O(1) read-set lookups.

    Concurrency: Uses asyncio.Lock for its own data structures.
    ReadSetRegistry manages its own threading.RLock internally.

    Example:
        >>> manager = ReactiveSubscriptionManager()
        >>> sub = Subscription(
        ...     subscription_id="s1", connection_id="c1",
        ...     zone_id="z1", query_id="q1",
        ... )
        >>> rs = ReadSet(query_id="q1", zone_id="z1")
        >>> rs.record_read("file", "/inbox/a.txt", revision=10)
        >>> await manager.register(sub, read_set=rs)
        >>> affected = manager.find_affected_connections(event)
    """

    def __init__(self, registry: ReadSetRegistry | None = None) -> None:
        """Initialize the reactive subscription manager.

        Args:
            registry: ReadSetRegistry instance (created if not provided)
        """
        self._registry = registry if registry is not None else ReadSetRegistry()
        self._subscriptions: dict[str, Subscription] = {}
        self._connection_index: dict[str, set[str]] = {}
        self._query_to_sub_id: dict[str, str] = {}  # query_id -> subscription_id
        self._lock = asyncio.Lock()

        # Performance tracking
        self._lookup_count = 0
        self._total_lookup_time = 0.0

    async def register(
        self,
        subscription: Subscription,
        read_set: "ReadSet | None" = None,
    ) -> None:
        """Register a subscription.

        The read_set parameter is required and will be registered in the
        ReadSetRegistry.

        Args:
            subscription: The subscription to register
            read_set: ReadSet for the subscription

        Raises:
            ValueError: If no read_set or query_id provided
        """
        if not subscription.query_id:
            msg = "Subscription requires query_id"
            raise ValueError(msg)
        if not read_set:
            msg = "Subscription requires a ReadSet"
            raise ValueError(msg)

        async with self._lock:
            sub_id = subscription.subscription_id

            # Remove old subscription if re-registering
            if sub_id in self._subscriptions:
                await self._unregister_internal(sub_id)

            # Store subscription
            self._subscriptions[sub_id] = subscription

            # Update connection index
            conn_id = subscription.connection_id
            if conn_id not in self._connection_index:
                self._connection_index[conn_id] = set()
            self._connection_index[conn_id].add(sub_id)

            # Register read set
            self._registry.register(read_set)
            self._query_to_sub_id[subscription.query_id] = sub_id

            logger.debug(
                f"[ReactiveSubManager] Registered {sub_id} "
                f"(conn={conn_id}, zone={subscription.zone_id})"
            )

    async def unregister(self, subscription_id: str) -> bool:
        """Unregister a subscription.

        Args:
            subscription_id: The subscription ID to remove

        Returns:
            True if found and removed, False if not found
        """
        async with self._lock:
            return await self._unregister_internal(subscription_id)

    async def _unregister_internal(self, subscription_id: str) -> bool:
        """Internal unregister (must hold lock).

        Returns:
            True if found and removed, False if not found
        """
        subscription = self._subscriptions.pop(subscription_id, None)
        if not subscription:
            return False

        # Remove from connection index
        conn_id = subscription.connection_id
        if conn_id in self._connection_index:
            self._connection_index[conn_id].discard(subscription_id)
            if not self._connection_index[conn_id]:
                del self._connection_index[conn_id]

        # Cleanup read set
        if subscription.query_id:
            self._registry.unregister(subscription.query_id)
            self._query_to_sub_id.pop(subscription.query_id, None)

        logger.debug(f"[ReactiveSubManager] Unregistered {subscription_id}")
        return True

    async def unregister_connection(self, connection_id: str) -> int:
        """Remove all subscriptions for a connection (called on disconnect).

        Args:
            connection_id: The connection ID being disconnected

        Returns:
            Count of removed subscriptions
        """
        async with self._lock:
            sub_ids = self._connection_index.get(connection_id)
            if not sub_ids:
                return 0

            # Copy to avoid mutation during iteration
            sub_ids_to_remove = set(sub_ids)
            count = 0
            for sub_id in sub_ids_to_remove:
                removed = await self._unregister_internal(sub_id)
                if removed:
                    count += 1

            logger.debug(
                f"[ReactiveSubManager] Cleaned up {count} subscriptions "
                f"for connection {connection_id}"
            )
            return count

    def _iter_matching_subscriptions(self, event: "FileEvent") -> list[tuple[str, Subscription]]:
        """Find all subscriptions matching an event (common core logic).

        Uses O(1+d) read-set lookup via ReadSetRegistry reverse index.
        Event type filters are applied to results.

        Note: Synchronous for performance (hot path). In asyncio's
        cooperative multitasking, sync code runs to completion without
        preemption, so no snapshots are needed — the dicts cannot be mutated
        mid-iteration since there are no await points.

        Args:
            event: The file event to match against subscriptions

        Returns:
            List of (subscription_id, Subscription) pairs.
        """
        results: list[tuple[str, Subscription]] = []

        zone_id = event.zone_id
        event_type = str(event.type)

        # Read-set lookup via registry (O(1+d))
        if zone_id is not None:
            # Use file version as write revision for read-set invalidation.
            # When version is unknown (None), use maxsize to conservatively
            # invalidate all matching read-set entries (safe default).
            version = event.version if event.version is not None else sys.maxsize
            affected_query_ids = self._registry.get_affected_queries(
                write_path=event.path,
                write_revision=version,
                zone_id=zone_id,
            )

            for query_id in affected_query_ids:
                sub_id = self._query_to_sub_id.get(query_id)
                if not sub_id:
                    continue
                sub = self._subscriptions.get(sub_id)
                if sub and self._matches_event_type(event_type, sub.event_types):
                    results.append((sub_id, sub))

        return results

    def find_affected_connections(self, event: "FileEvent") -> set[str]:
        """Find all connection IDs that should receive an event.

        Deduplicates by connection_id (a connection appears once even if
        multiple subscriptions match).

        Args:
            event: The file event to match against subscriptions

        Returns:
            Deduplicated set of connection_ids that should receive the event
        """
        start_time = time.monotonic()
        pairs = self._iter_matching_subscriptions(event)
        result = {sub.connection_id for _, sub in pairs}
        elapsed = time.monotonic() - start_time
        self._lookup_count += 1
        self._total_lookup_time += elapsed
        return result

    def find_affected_subscriptions(self, event: "FileEvent") -> dict[str, list[Subscription]]:
        """Find all subscriptions affected by an event, grouped by connection.

        Returns full Subscription objects grouped by connection_id for
        constructing batch_update messages (#1170). Deduplicates by
        subscription_id within each connection.

        Args:
            event: The file event to match against subscriptions

        Returns:
            dict mapping connection_id to list of matching Subscription objects
        """
        start_time = time.monotonic()
        pairs = self._iter_matching_subscriptions(event)

        # Group by connection, deduplicate by sub_id
        by_connection: dict[str, dict[str, Subscription]] = {}
        for sub_id, sub in pairs:
            conn_subs = by_connection.setdefault(sub.connection_id, {})
            conn_subs[sub_id] = sub

        elapsed = time.monotonic() - start_time
        self._lookup_count += 1
        self._total_lookup_time += elapsed

        return {conn_id: list(subs.values()) for conn_id, subs in by_connection.items()}

    @staticmethod
    def _matches_event_type(
        event_type: str,
        filter_types: frozenset[str],
    ) -> bool:
        """Check if an event type matches the subscription's filter.

        Args:
            event_type: The event type string
            filter_types: Frozenset of allowed event types (empty = match all)

        Returns:
            True if the event type is allowed
        """
        if not filter_types:
            return True
        return event_type in filter_types

    async def cleanup_sweep(self) -> int:
        """Remove expired read sets from the registry.

        Returns:
            Count of expired entries cleaned up
        """
        count = self._registry.cleanup_expired()

        if count > 0:
            # Remove subscriptions whose read sets were cleaned up
            async with self._lock:
                expired_sub_ids = []
                for sub_id, sub in self._subscriptions.items():
                    if sub.query_id and self._registry.get_read_set(sub.query_id) is None:
                        expired_sub_ids.append(sub_id)

                for sub_id in expired_sub_ids:
                    await self._unregister_internal(sub_id)

            logger.info(
                f"[ReactiveSubManager] Cleanup sweep: {count} expired read sets, "
                f"{len(expired_sub_ids)} subscriptions removed"
            )

        return count

    def get_stats(self) -> dict[str, Any]:
        """Get subscription manager statistics.

        Returns:
            Dictionary with stats including counts, registry stats,
            and performance metrics
        """
        avg_lookup_ms = 0.0
        if self._lookup_count > 0:
            avg_lookup_ms = (self._total_lookup_time / self._lookup_count) * 1000

        return {
            "total_subscriptions": len(self._subscriptions),
            "connections_tracked": len(self._connection_index),
            "lookup_count": self._lookup_count,
            "avg_lookup_ms": round(avg_lookup_ms, 3),
            "registry": self._registry.get_stats(),
        }
