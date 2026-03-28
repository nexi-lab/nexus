"""Deferred Permission Buffer for async write optimization.

This module provides an async buffer that defers permission operations
(rebac_write, ensure_parent_tuples) for batch processing in the background.

Key insight: Since owner_id is stored in metadata synchronously, the file
owner can always access their files via the O(1) owner fast-path check.
The ReBAC tuples are only needed for sharing/audit, not owner access.

This allows us to:
1. Write file content + metadata synchronously (owner can access immediately)
2. Defer permission grants to background batch processing
3. Achieve ~10x faster single-file write latency

No WAL or crash recovery needed because:
- owner_id in metadata guarantees owner access
- Worst case: file exists but can't be shared until reconciliation
"""

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

from nexus.lib.deferred_buffer import DeferredBuffer

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation
    from nexus.bricks.rebac.hierarchy_manager import HierarchyManager
    from nexus.bricks.rebac.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)


class DeferredPermissionBuffer(DeferredBuffer):
    """
    Buffers permission operations for batch flushing.

    Thread-safe for use from both sync and async contexts.
    Uses a background thread for flushing to avoid blocking the event loop.
    """

    def __init__(
        self,
        rebac_manager: "ReBACManager | None" = None,
        hierarchy_manager: "HierarchyManager | None" = None,
        flush_interval_sec: float = 0.1,  # 100ms default
        max_batch_size: int = 1000,
        max_retries: int = 3,
    ):
        """Initialize the deferred permission buffer.

        Args:
            rebac_manager: ReBACManager instance for batch permission writes
            hierarchy_manager: HierarchyManager for batch hierarchy tuple creation
            flush_interval_sec: How often to flush pending operations (default 100ms)
            max_batch_size: Trigger immediate flush if queue exceeds this size
            max_retries: Max retry attempts before dead-lettering a failed item
        """
        super().__init__(
            flush_interval_sec=flush_interval_sec,
            max_batch_size=max_batch_size,
            max_retries=max_retries,
            thread_name="DeferredPermissionBuffer-Flush",
        )
        self._rebac_manager = rebac_manager
        self._hierarchy_manager = hierarchy_manager

        # Pending operations (thread-safe via base class _lock)
        from collections import deque

        self._pending_hierarchy: deque[tuple[str, str]] = deque()  # (path, zone_id)
        self._pending_grants: deque[dict[str, Any]] = deque()

        # Retry tracking: maps (path, zone_id) -> attempt count for hierarchy,
        # and (subject, relation, object, zone_id) -> attempt count for grants
        self._hierarchy_retry_counts: dict[tuple[str, str], int] = {}
        self._grants_retry_counts: dict[tuple[Any, ...], int] = {}

        # Pub/Sub for cross-zone flush coordination (Issue #3192)
        self._pubsub: "PubSubInvalidation | None" = None

        # Stats (permission-specific)
        self._total_hierarchy_flushed = 0
        self._total_grants_flushed = 0

    def set_pubsub(self, pubsub: "PubSubInvalidation") -> None:
        """Set Pub/Sub for cross-zone flush coordination."""
        self._pubsub = pubsub

    def queue_hierarchy(self, path: str, zone_id: str) -> None:
        """Queue hierarchy tuple creation (non-blocking).

        Args:
            path: File path that needs parent tuples
            zone_id: Zone ID for the operation
        """
        with self._lock:
            self._pending_hierarchy.append((path, zone_id))
            queue_size = len(self._pending_hierarchy) + len(self._pending_grants)

        # Trigger immediate flush if at capacity
        self._check_batch_overflow(queue_size)

    def queue_owner_grant(
        self,
        user: str,
        path: str,
        zone_id: str,
    ) -> None:
        """Queue owner permission grant (non-blocking).

        Args:
            user: User ID to grant ownership to
            path: File path to grant ownership of
            zone_id: Zone ID for the operation
        """
        with self._lock:
            self._pending_grants.append(
                {
                    "subject": ("user", user),
                    "relation": "direct_owner",
                    "object": ("file", path),
                    "zone_id": zone_id,
                }
            )
            queue_size = len(self._pending_hierarchy) + len(self._pending_grants)

        # Trigger immediate flush if at capacity
        self._check_batch_overflow(queue_size)

    # ── DeferredBuffer abstract method implementations ──

    def _has_items(self) -> bool:
        return bool(self._pending_hierarchy) or bool(self._pending_grants)

    def _drain_items(self) -> list[Any]:
        """Drain both queues and return as a combined structure."""
        hierarchy_batch = list(self._pending_hierarchy)
        self._pending_hierarchy.clear()
        grants_batch = list(self._pending_grants)
        self._pending_grants.clear()
        return [hierarchy_batch, grants_batch]

    def _flush_items(self, items: list[Any], *, catch_unexpected: bool) -> int:
        """Flush hierarchy and grant batches."""
        hierarchy_batch: list[tuple[str, str]] = items[0]
        grants_batch: list[dict[str, Any]] = items[1]

        retryable_errors: tuple[type[BaseException], ...]
        if catch_unexpected:
            retryable_errors = (Exception,)
        else:
            retryable_errors = (OperationalError, TimeoutError, RuntimeError)

        hierarchy_count = 0
        grants_count = 0

        # Batch hierarchy tuples (group by zone)
        if hierarchy_batch and self._hierarchy_manager:
            try:
                # Group by zone_id
                by_zone: dict[str, list[str]] = {}
                for _path, zone_id in hierarchy_batch:
                    by_zone.setdefault(zone_id, []).append(_path)

                for zone_id, paths in by_zone.items():
                    self._hierarchy_manager.ensure_parent_tuples_batch(
                        paths,
                        zone_id=zone_id,
                    )
                    hierarchy_count += len(paths)

                self._total_hierarchy_flushed += hierarchy_count
                # Clear retry counts for successfully flushed items
                for item in hierarchy_batch:
                    self._hierarchy_retry_counts.pop(item, None)
            except retryable_errors as e:
                self._requeue_hierarchy(hierarchy_batch, e)

        # Batch owner grants
        if grants_batch and self._rebac_manager:
            try:
                self._rebac_manager.rebac_write_batch(grants_batch)
                grants_count = len(grants_batch)
                self._total_grants_flushed += grants_count
                # Clear retry counts for successfully flushed items
                for grant in grants_batch:
                    gkey = (
                        grant["subject"],
                        grant["relation"],
                        grant["object"],
                        grant["zone_id"],
                    )
                    self._grants_retry_counts.pop(gkey, None)
            except retryable_errors as e:
                self._requeue_grants(grants_batch, e)

        # Pub/Sub: notify other zones about flushed permissions (Issue #3192)
        if (hierarchy_count > 0 or grants_count > 0) and self._pubsub:
            zones_flushed = set()
            for _path, zone_id in hierarchy_batch:
                zones_flushed.add(zone_id)
            for grant in grants_batch:
                zones_flushed.add(grant.get("zone_id", ""))
            for zone_id in zones_flushed:
                if zone_id:
                    self._pubsub.publish_invalidation(
                        zone_id=zone_id,
                        layer="deferred_flush",
                        payload={
                            "hierarchy_count": hierarchy_count,
                            "grants_count": grants_count,
                        },
                    )

        return hierarchy_count + grants_count

    def _get_item_stats(self) -> dict[str, Any]:
        with self._lock:
            pending_hierarchy = len(self._pending_hierarchy)
            pending_grants = len(self._pending_grants)
        return {
            "pending_hierarchy": pending_hierarchy,
            "pending_grants": pending_grants,
            "total_hierarchy_flushed": self._total_hierarchy_flushed,
            "total_grants_flushed": self._total_grants_flushed,
        }

    # ── Retry helpers ──

    def _requeue_hierarchy(
        self, hierarchy_batch: list[tuple[str, str]], error: BaseException
    ) -> None:
        """Re-queue hierarchy items with retry tracking, dead-letter on max retries."""
        requeue: list[tuple[str, str]] = []
        for item in hierarchy_batch:
            key = item  # (path, zone_id)
            count = self._hierarchy_retry_counts.get(key, 0) + 1
            if count >= self._max_retries:
                logger.error(
                    "Hierarchy item dead-lettered after %d retries: path=%s, zone=%s, error=%s",
                    count,
                    item[0],
                    item[1],
                    error,
                )
                self._dead_letter_item(
                    "hierarchy",
                    {"path": item[0], "zone_id": item[1]},
                    error,
                    count,
                )
                self._hierarchy_retry_counts.pop(key, None)
            else:
                logger.warning(
                    "Hierarchy flush failed (attempt %d/%d), "
                    "re-queueing: path=%s, zone=%s, error=%s",
                    count,
                    self._max_retries,
                    item[0],
                    item[1],
                    error,
                )
                self._hierarchy_retry_counts[key] = count
                requeue.append(item)
        if requeue:
            with self._lock:
                self._pending_hierarchy.extend(requeue)

    def _requeue_grants(self, grants_batch: list[dict[str, Any]], error: BaseException) -> None:
        """Re-queue grant items with retry tracking, dead-letter on max retries."""
        requeue_grants: list[dict[str, Any]] = []
        for grant in grants_batch:
            gkey = (
                grant["subject"],
                grant["relation"],
                grant["object"],
                grant["zone_id"],
            )
            count = self._grants_retry_counts.get(gkey, 0) + 1
            if count >= self._max_retries:
                logger.error(
                    "Grant item dead-lettered after %d retries: subject=%s, object=%s, error=%s",
                    count,
                    grant["subject"],
                    grant["object"],
                    error,
                )
                self._dead_letter_item("grant", grant, error, count)
                self._grants_retry_counts.pop(gkey, None)
            else:
                logger.warning(
                    "Grant flush failed (attempt %d/%d), "
                    "re-queueing: subject=%s, object=%s, error=%s",
                    count,
                    self._max_retries,
                    grant["subject"],
                    grant["object"],
                    error,
                )
                self._grants_retry_counts[gkey] = count
                requeue_grants.append(grant)
        if requeue_grants:
            with self._lock:
                self._pending_grants.extend(requeue_grants)


# Singleton instance for easy access
_default_buffer: DeferredPermissionBuffer | None = None


def get_default_buffer() -> DeferredPermissionBuffer | None:
    """Get the default deferred permission buffer instance."""
    return _default_buffer


def set_default_buffer(buffer: DeferredPermissionBuffer | None) -> None:
    """Set the default deferred permission buffer instance."""
    global _default_buffer
    _default_buffer = buffer
