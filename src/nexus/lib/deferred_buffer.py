"""Generic deferred buffer with background batch flushing.

Provides thread-safe queue + background flush thread infrastructure
used by DeferredPermissionBuffer.

Subclasses implement:
    - _drain_items() -> list[T]  — atomically drain pending items from the queue
    - _flush_items(items, catch_unexpected) — flush a batch of items
    - _has_items() -> bool — check if there are pending items (called under lock)
    - _get_item_stats() -> dict — return subclass-specific stats
"""

import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class DeferredBuffer(ABC, Generic[T]):
    """Base class for deferred buffers with background batch flushing.

    Thread-safe for use from both sync and async contexts.
    Uses a background thread for flushing to avoid blocking the event loop.

    Subclasses must implement _drain_items(), _flush_items(), _has_items(),
    and _get_item_stats().
    """

    def __init__(
        self,
        *,
        flush_interval_sec: float = 0.1,
        max_batch_size: int = 1000,
        max_retries: int = 3,
        thread_name: str = "DeferredBuffer-Flush",
    ):
        self._flush_interval = flush_interval_sec
        self._max_batch_size = max_batch_size
        self._max_retries = max_retries
        self._thread_name = thread_name

        # Shared lock for subclass queue access
        self._lock = threading.Lock()

        # Dead letter queue for permanently failed items
        self._dead_letter: list[dict[str, Any]] = []

        # Background flush thread
        self._flush_thread: threading.Thread | None = None
        self._shutdown = threading.Event()
        self._flush_event = threading.Event()  # C2: wake thread on batch overflow
        self._started = False

        # Stats
        self._flush_count = 0
        self._total_flushed = 0

    # ── Lifecycle (BackgroundService protocol) ──

    async def start(self) -> None:
        """Start the background flush worker thread."""
        self._start_sync()

    async def stop(self) -> None:
        """Stop the buffer and flush remaining items."""
        self._stop_sync()

    def _start_sync(self) -> None:
        """Sync start -- spawns background flush thread."""
        if self._started:
            return

        self._shutdown.clear()
        self._flush_event.clear()
        self._flush_thread = threading.Thread(
            target=self._flush_loop,
            name=self._thread_name,
            daemon=True,
        )
        self._flush_thread.start()
        self._started = True
        logger.info(
            "%s started (interval=%.3fs, max_batch=%d)",
            self._thread_name,
            self._flush_interval,
            self._max_batch_size,
        )

    def _stop_sync(self, timeout: float = 5.0) -> None:
        """Sync stop -- joins thread and flushes remaining items."""
        if not self._started:
            return

        logger.info("%s stopping...", self._thread_name)
        self._shutdown.set()
        self._flush_event.set()  # wake thread so it can exit

        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=timeout)

        # Final flush on shutdown should be resilient
        self._flush_sync(catch_unexpected=True)

        self._started = False
        stats = self.get_stats()
        logger.info("%s stopped. Stats: %s", self._thread_name, stats)

    # ── Public API ──

    def flush(self) -> None:
        """Synchronously flush all pending items.

        Call when you need to ensure all items are persisted,
        e.g., before a critical read or during graceful shutdown.
        """
        self._flush_sync(catch_unexpected=False)

    def get_stats(self) -> dict[str, Any]:
        """Get buffer statistics."""
        stats = {
            "flush_count": self._flush_count,
            "total_flushed": self._total_flushed,
            "dead_letter_count": len(self._dead_letter),
        }
        stats.update(self._get_item_stats())
        return stats

    def get_dead_letter(self) -> list[dict[str, Any]]:
        """Return a copy of the dead-letter queue for inspection."""
        with self._lock:
            return list(self._dead_letter)

    # ── Batch overflow trigger (C2 fix) ──

    def _check_batch_overflow(self, queue_size: int) -> None:
        """Wake the background thread immediately if batch size exceeded."""
        if queue_size >= self._max_batch_size:
            self._trigger_flush()

    # ── Background flush loop ──

    def _flush_loop(self) -> None:
        """Background loop that flushes periodically or on batch overflow."""
        while not self._shutdown.is_set():
            # Wait for shutdown signal or flush interval -- whichever comes first.
            # _flush_event can interrupt this wait for immediate flush on batch overflow.
            self._flush_event.wait(timeout=self._flush_interval)
            self._flush_event.clear()

            if not self._shutdown.is_set():
                try:
                    self._flush_sync(catch_unexpected=True)
                except Exception as e:
                    logger.error("%s flush error: %s", self._thread_name, e)

    def _trigger_flush(self) -> None:
        """Trigger an immediate flush (called when batch size exceeded)."""
        self._flush_event.set()

    # ── Flush implementation ──

    def _flush_sync(self, *, catch_unexpected: bool = False) -> None:
        """Flush all pending items using subclass-specific logic."""
        with self._lock:
            if not self._has_items():
                return
            items = self._drain_items()

        start_time = time.perf_counter()
        flushed_count = self._flush_items(items, catch_unexpected=catch_unexpected)

        if flushed_count > 0:
            self._total_flushed += flushed_count
            self._flush_count += 1
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.debug(
                "[%s] flushed=%d, elapsed=%.1fms",
                self._thread_name,
                flushed_count,
                elapsed_ms,
            )

    # ── Dead-letter helpers ──

    def _dead_letter_item(
        self,
        item_type: str,
        item: Any,
        error: BaseException,
        retries: int,
    ) -> None:
        """Add an item to the dead-letter queue."""
        with self._lock:
            self._dead_letter.append(
                {
                    "type": item_type,
                    "item": item,
                    "error": str(error),
                    "retries": retries,
                }
            )

    # ── Abstract methods for subclasses ──

    @abstractmethod
    def _drain_items(self) -> list[Any]:
        """Atomically drain pending items from the queue.

        Called while self._lock is held.

        Returns:
            List of items to flush.
        """
        ...

    @abstractmethod
    def _flush_items(self, items: list[Any], *, catch_unexpected: bool) -> int:
        """Flush a batch of items.

        Called outside self._lock. Must handle errors, retries, and
        dead-lettering using self._dead_letter_item() and self._max_retries.

        Args:
            items: Items drained from the queue.
            catch_unexpected: If True, catch all exceptions (background mode).

        Returns:
            Number of items successfully flushed.
        """
        ...

    @abstractmethod
    def _has_items(self) -> bool:
        """Check if there are pending items. Called while self._lock is held."""
        ...

    @abstractmethod
    def _get_item_stats(self) -> dict[str, Any]:
        """Return subclass-specific stats to merge into get_stats()."""
        ...
