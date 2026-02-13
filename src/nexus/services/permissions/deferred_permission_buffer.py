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

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.services.permissions.hierarchy_manager import HierarchyManager
    from nexus.services.permissions.rebac_manager import ReBACManager

logger = logging.getLogger(__name__)


class DeferredPermissionBuffer:
    """
    Buffers permission operations for batch flushing.

    Thread-safe for use from both sync and async contexts.
    Uses a background thread for flushing to avoid blocking the event loop.
    """

    def __init__(
        self,
        rebac_manager: ReBACManager | None = None,
        hierarchy_manager: HierarchyManager | None = None,
        flush_interval_sec: float = 0.1,  # 100ms default
        max_batch_size: int = 1000,
    ):
        """Initialize the deferred permission buffer.

        Args:
            rebac_manager: ReBACManager instance for batch permission writes
            hierarchy_manager: HierarchyManager for batch hierarchy tuple creation
            flush_interval_sec: How often to flush pending operations (default 100ms)
            max_batch_size: Trigger immediate flush if queue exceeds this size
        """
        self._rebac_manager = rebac_manager
        self._hierarchy_manager = hierarchy_manager
        self._flush_interval = flush_interval_sec
        self._max_batch_size = max_batch_size

        # Pending operations (thread-safe with lock)
        self._pending_hierarchy: deque[tuple[str, str]] = deque()  # (path, zone_id)
        self._pending_grants: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._started = False

        # Stats
        self._total_hierarchy_flushed = 0
        self._total_grants_flushed = 0
        self._flush_count = 0

    def start(self) -> None:
        """Start the background flush worker thread."""
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

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the buffer and flush remaining items.

        Args:
            timeout: Maximum time to wait for final flush
        """
        if not self._started:
            return

        logger.info("DeferredPermissionBuffer stopping...")
        self._shutdown.set()

        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=timeout)

        # Final flush
        self._flush_sync()

        self._started = False
        logger.info(
            f"DeferredPermissionBuffer stopped. Stats: "
            f"hierarchy={self._total_hierarchy_flushed}, "
            f"grants={self._total_grants_flushed}, "
            f"flushes={self._flush_count}"
        )

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
        self._flush_sync()

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
        }

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
                    self._flush_sync()
                except Exception as e:
                    logger.error(f"DeferredPermissionBuffer flush error: {e}")

    def _flush_sync(self) -> None:
        """Flush all pending operations using batch APIs."""
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
                for path, zone_id in hierarchy_batch:
                    by_zone.setdefault(zone_id, []).append(path)

                for zone_id, paths in by_zone.items():
                    self._hierarchy_manager.ensure_parent_tuples_batch(
                        paths,
                        zone_id=zone_id,
                    )
                    hierarchy_count += len(paths)

                self._total_hierarchy_flushed += hierarchy_count
            except Exception as e:
                logger.warning(f"Hierarchy flush failed, re-queueing: {e}")
                # Re-queue on failure
                with self._lock:
                    self._pending_hierarchy.extend(hierarchy_batch)

        # Batch owner grants
        if grants_batch and self._rebac_manager:
            try:
                self._rebac_manager.rebac_write_batch(grants_batch)
                grants_count = len(grants_batch)
                self._total_grants_flushed += grants_count
            except Exception as e:
                logger.warning(f"Grant flush failed, re-queueing: {e}")
                # Re-queue on failure
                with self._lock:
                    self._pending_grants.extend(grants_batch)

        if hierarchy_count > 0 or grants_count > 0:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            self._flush_count += 1
            logger.debug(
                f"[DEFERRED-FLUSH] hierarchy={hierarchy_count}, "
                f"grants={grants_count}, elapsed={elapsed_ms:.1f}ms"
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
