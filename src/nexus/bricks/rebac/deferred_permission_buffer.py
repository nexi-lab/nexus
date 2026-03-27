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
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation
    from nexus.bricks.rebac.hierarchy_manager import HierarchyManager
    from nexus.bricks.rebac.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)


class DeferredPermissionBuffer:
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
        self._rebac_manager = rebac_manager
        self._hierarchy_manager = hierarchy_manager
        self._flush_interval = flush_interval_sec
        self._max_batch_size = max_batch_size
        self._max_retries = max_retries

        # Pending operations (thread-safe with lock)
        self._pending_hierarchy: deque[tuple[str, str]] = deque()  # (path, zone_id)
        self._pending_grants: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()

        # Retry tracking: maps (path, zone_id) -> attempt count for hierarchy,
        # and (subject, relation, object, zone_id) -> attempt count for grants
        self._hierarchy_retry_counts: dict[tuple[str, str], int] = {}
        self._grants_retry_counts: dict[tuple[Any, ...], int] = {}

        # Dead letter queue for permanently failed items
        self._dead_letter: list[dict[str, Any]] = []

        # Pub/Sub for cross-zone flush coordination (Issue #3192)
        self._pubsub: "PubSubInvalidation | None" = None

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._started = False

        # Stats
        self._total_hierarchy_flushed = 0
        self._total_grants_flushed = 0
        self._flush_count = 0

    async def start(self) -> None:
        """Start the background flush worker thread (PersistentService protocol)."""
        self._start_sync()

    async def stop(self) -> None:
        """Stop the buffer and flush remaining items (PersistentService protocol)."""
        self._stop_sync()

    def _start_sync(self) -> None:
        """Sync start — spawns background flush thread."""
        if self._started:
            return

        self._shutdown.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name="DeferredPermissionBuffer-Flush",
            daemon=True,
        )
        self._flush_thread.start()
        self._started = True
        logger.info(
            f"DeferredPermissionBuffer started (interval={self._flush_interval}s, "
            f"max_batch={self._max_batch_size})"
        )

    def _stop_sync(self, timeout: float = 5.0) -> None:
        """Sync stop — joins thread and flushes remaining items."""
        if not self._started:
            return

        logger.info("DeferredPermissionBuffer stopping...")
        self._shutdown.set()

        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=timeout)

        # Final flush on shutdown should be resilient like the background worker.
        self._flush_sync(catch_unexpected=True)

        self._started = False
        logger.info(
            f"DeferredPermissionBuffer stopped. Stats: "
            f"hierarchy={self._total_hierarchy_flushed}, "
            f"grants={self._total_grants_flushed}, "
            f"flushes={self._flush_count}"
        )

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
        if queue_size >= self._max_batch_size:
            self._trigger_flush()

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
        if queue_size >= self._max_batch_size:
            self._trigger_flush()

    def flush(self) -> None:
        """Synchronously flush all pending operations.

        Call this when you need to ensure all permissions are persisted,
        e.g., before a critical read or during graceful shutdown.
        """
        self._flush_sync(catch_unexpected=False)

    def get_stats(self) -> dict[str, Any]:
        """Get buffer statistics.

        Returns:
            Dict with queue sizes and flush counts
        """
        with self._lock:
            pending_hierarchy = len(self._pending_hierarchy)
            pending_grants = len(self._pending_grants)

        return {
            "pending_hierarchy": pending_hierarchy,
            "pending_grants": pending_grants,
            "total_hierarchy_flushed": self._total_hierarchy_flushed,
            "total_grants_flushed": self._total_grants_flushed,
            "flush_count": self._flush_count,
            "dead_letter_count": len(self._dead_letter),
        }

    def get_dead_letter(self) -> list[dict[str, Any]]:
        """Return a copy of the dead-letter queue for inspection.

        Returns:
            List of dicts, each containing 'type', 'item', 'error', and 'retries'.
        """
        with self._lock:
            return list(self._dead_letter)

    def _trigger_flush(self) -> None:
        """Trigger an immediate flush (called when batch size exceeded)."""
        # For now, just let the background thread handle it
        # The short flush interval (100ms) means we won't wait long
        pass

    def _flush_loop(self) -> None:
        """Background loop that flushes periodically."""
        while not self._shutdown.is_set():
            # Wait for shutdown signal or flush interval
            self._shutdown.wait(timeout=self._flush_interval)

            if not self._shutdown.is_set():
                try:
                    self._flush_sync(catch_unexpected=True)
                except Exception as e:  # fail-safe: background flush must not crash thread
                    logger.error(f"DeferredPermissionBuffer flush error: {e}")

    def _flush_sync(self, *, catch_unexpected: bool = False) -> None:
        """Flush all pending operations using batch APIs."""
        retryable_errors: tuple[type[BaseException], ...]
        if catch_unexpected:
            retryable_errors = (Exception,)
        else:
            retryable_errors = (OperationalError, TimeoutError, RuntimeError)

        # Atomically drain queues
        with self._lock:
            if not self._pending_hierarchy and not self._pending_grants:
                return

            hierarchy_batch = list(self._pending_hierarchy)
            self._pending_hierarchy.clear()

            grants_batch = list(self._pending_grants)
            self._pending_grants.clear()

        start_time = time.perf_counter()
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
                # Background flushes should re-queue unexpected manager errors
                # instead of dropping buffered permission state.
                # Re-queue with retry tracking; dead-letter items that exceed max_retries
                requeue: list[tuple[str, str]] = []
                for item in hierarchy_batch:
                    key = item  # (path, zone_id)
                    count = self._hierarchy_retry_counts.get(key, 0) + 1
                    if count >= self._max_retries:
                        logger.error(
                            f"Hierarchy item dead-lettered after {count} retries: "
                            f"path={item[0]}, zone={item[1]}, error={e}"
                        )
                        with self._lock:
                            self._dead_letter.append(
                                {
                                    "type": "hierarchy",
                                    "item": {"path": item[0], "zone_id": item[1]},
                                    "error": str(e),
                                    "retries": count,
                                }
                            )
                        self._hierarchy_retry_counts.pop(key, None)
                    else:
                        logger.warning(
                            f"Hierarchy flush failed (attempt {count}/{self._max_retries}), "
                            f"re-queueing: path={item[0]}, zone={item[1]}, error={e}"
                        )
                        self._hierarchy_retry_counts[key] = count
                        requeue.append(item)
                if requeue:
                    with self._lock:
                        self._pending_hierarchy.extend(requeue)

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
                # Background flushes should re-queue unexpected manager errors
                # instead of dropping buffered permission state.
                # Re-queue with retry tracking; dead-letter items that exceed max_retries
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
                            f"Grant item dead-lettered after {count} retries: "
                            f"subject={grant['subject']}, object={grant['object']}, error={e}"
                        )
                        with self._lock:
                            self._dead_letter.append(
                                {
                                    "type": "grant",
                                    "item": grant,
                                    "error": str(e),
                                    "retries": count,
                                }
                            )
                        self._grants_retry_counts.pop(gkey, None)
                    else:
                        logger.warning(
                            f"Grant flush failed (attempt {count}/{self._max_retries}), "
                            f"re-queueing: subject={grant['subject']}, "
                            f"object={grant['object']}, error={e}"
                        )
                        self._grants_retry_counts[gkey] = count
                        requeue_grants.append(grant)
                if requeue_grants:
                    with self._lock:
                        self._pending_grants.extend(requeue_grants)

        if hierarchy_count > 0 or grants_count > 0:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._flush_count += 1
            logger.debug(
                f"[DEFERRED-FLUSH] hierarchy={hierarchy_count}, "
                f"grants={grants_count}, elapsed={elapsed_ms:.1f}ms"
            )

            # Pub/Sub: notify other zones about flushed permissions (Issue #3192)
            if self._pubsub:
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


# Singleton instance for easy access
_default_buffer: DeferredPermissionBuffer | None = None


def get_default_buffer() -> DeferredPermissionBuffer | None:
    """Get the default deferred permission buffer instance."""
    return _default_buffer


def set_default_buffer(buffer: DeferredPermissionBuffer | None) -> None:
    """Set the default deferred permission buffer instance."""
    global _default_buffer
    _default_buffer = buffer
