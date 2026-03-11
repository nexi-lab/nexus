"""DedupWorkQueue — coalescing work queue backed by DT_PIPE RingBuffer (#2062).

Follows the Kubernetes controller-runtime workqueue pattern:
  - 10 rapid writes to the same file → 10 events recorded (audit complete)
    but only 1 processing run.
  - Does NOT replace EventLog — dedup is for *processing*, not *recording*.

Three invariants maintained by add/get/done:
  1. An item in ``dirty`` but not ``processing`` has exactly one entry in the buffer.
  2. An item in ``processing`` has zero entries in the buffer.
  3. An item can be in both ``dirty`` and ``processing`` (re-added during
     processing → will be re-queued on ``done()``).

Transport: DT_PIPE RingBuffer (Rust-backed kfifo, ~0.5μs/op) replaces
asyncio.Queue. The pipe carries 8-byte sequence tokens; actual items stay
in a Python dict — no serialization needed. Dedup logic (dirty/processing
sets) remains in Python.

Architecture: Tier 3 System Service (NEXUS-LEGO-ARCHITECTURE.md §12.5).
Consumer-side dedup: each subscriber opts in independently.

References:
  - https://github.com/kubernetes/client-go/blob/master/util/workqueue/queue.go
  - NEXUS-LEGO-ARCHITECTURE.md §12.5
  - KERNEL-ARCHITECTURE.md §6 (DT_PIPE kfifo tier)
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from nexus.core.pipe import PipeClosedError, PipeFullError, RingBuffer

logger = logging.getLogger(__name__)

T = TypeVar("T")


class ShutdownError(Exception):
    """Raised when get() is called on a shut-down queue."""


class DedupWorkQueue(Generic[T]):
    """Deduplicating async work queue.

    Coalesces duplicate keys so that rapid additions of the same key
    result in at most one processing run.

    **Transport:** Uses DT_PIPE RingBuffer (Rust-backed kfifo) for the
    internal FIFO, replacing asyncio.Queue. The pipe carries 8-byte
    sequence tokens; actual items stay in a Python dict (no serialization).
    Dedup logic (dirty/processing sets) remains in Python.

    **Threading model:** This queue is designed for single-event-loop asyncio.
    ``add()`` and ``get()`` are async and acquire a lock.  ``done()`` is
    intentionally synchronous (for use in ``finally`` blocks) and is safe
    ONLY when called from a coroutine on the **same** event loop as
    ``add()``/``get()``.  Never call ``done()`` from a thread pool executor
    or a different event loop.

    Usage::

        q: DedupWorkQueue[str] = DedupWorkQueue()

        # Producer side — events coalesce by key
        await q.add("/data/file.txt")
        await q.add("/data/file.txt")  # coalesced

        # Consumer side — processes each key once
        key = await q.get()            # "/data/file.txt"
        try:
            await process(key)
        finally:
            q.done(key)
    """

    def __init__(self, *, capacity: int = 65_536) -> None:
        self._buf = RingBuffer(capacity)
        self._seq = 0  # monotonic sequence counter (pipe carries 8-byte tokens)
        self._items: dict[int, T] = {}  # seq → item (actual data stays in Python)
        self._dirty: set[T] = set()
        self._processing: set[T] = set()
        self._lock = asyncio.Lock()
        self._shutting_down = False

        # Metrics (monotonic counters)
        self._adds = 0
        self._coalesced = 0
        self._gets = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def add(self, key: T) -> None:
        """Enqueue a key for processing, coalescing duplicates.

        If the key is already pending (in dirty set) and not currently
        being processed, this is a no-op (coalesced).

        If the key is currently being processed, it is added to the
        dirty set so that ``done()`` will re-queue it.

        Raises:
            ShutdownError: If the queue has been shut down.
        """
        async with self._lock:
            if self._shutting_down:
                raise ShutdownError("DedupWorkQueue has been shut down")

            self._adds += 1

            if key in self._dirty:
                # Already pending — coalesce
                self._coalesced += 1
                return

            self._dirty.add(key)

            if key in self._processing:
                # Currently being processed — will re-queue on done()
                return

            # New work item — enqueue token via pipe
            seq = self._seq
            self._seq += 1
            self._items[seq] = key
            self._buf.write_u64_nowait(seq)

    async def get(self) -> T:
        """Get the next key to process (blocks until available).

        The caller MUST call ``done(key)`` when processing is complete,
        even if processing fails.  Use a try/finally block.

        Returns:
            The next key to process.

        Raises:
            ShutdownError: If the queue is shut down while waiting.
        """
        while True:
            if self._shutting_down and self._buf._core.is_empty():
                raise ShutdownError("DedupWorkQueue has been shut down")

            try:
                seq = await asyncio.wait_for(self._buf.read_u64(), timeout=0.5)
            except (TimeoutError, PipeClosedError):
                if self._shutting_down:
                    raise ShutdownError("DedupWorkQueue has been shut down") from None
                continue
            key = self._items.pop(seq)

            async with self._lock:
                self._gets += 1
                self._dirty.discard(key)
                self._processing.add(key)

            return key

    def done(self, key: T) -> None:
        """Mark a key as done processing.

        If the key was re-added while being processed (present in dirty),
        it is re-queued for another processing run.

        This method is intentionally synchronous so it can be called in
        ``finally`` blocks without ``await``.  It is safe in single-event-loop
        asyncio because it contains no suspension points — CPython's cooperative
        scheduling guarantees atomic execution with respect to other coroutines.

        **Do NOT call from a thread pool executor or different event loop.**

        Args:
            key: The key that was returned by ``get()``.
        """
        self._processing.discard(key)

        if key in self._dirty:
            # Re-added during processing — re-queue via pipe
            try:
                seq = self._seq
                self._seq += 1
                self._items[seq] = key
                self._buf.write_u64_nowait(seq)
            except (PipeClosedError, PipeFullError):
                # Shutting down or buffer full — item stays in dirty
                # and will be picked up if queue restarts
                pass

    async def shutdown(self) -> None:
        """Shut down the queue gracefully.

        After shutdown, ``add()`` raises ``ShutdownError`` and ``get()``
        drains remaining items then raises ``ShutdownError``.
        """
        async with self._lock:
            self._shutting_down = True

        logger.info(
            "DedupWorkQueue shutdown (adds=%d, coalesced=%d, gets=%d)",
            self._adds,
            self._coalesced,
            self._gets,
        )

    @property
    def is_shutting_down(self) -> bool:
        """Whether the queue has been shut down."""
        return self._shutting_down

    def __len__(self) -> int:
        """Number of items pending processing (in dirty set)."""
        return len(self._dirty)

    def __repr__(self) -> str:
        return (
            f"DedupWorkQueue(pending={len(self._dirty)}, "
            f"processing={len(self._processing)}, "
            f"shutting_down={self._shutting_down})"
        )

    @property
    def processing_count(self) -> int:
        """Number of items currently being processed."""
        return len(self._processing)

    @property
    def metrics(self) -> dict[str, int]:
        """Queue metrics for observability."""
        return {
            "adds": self._adds,
            "coalesced": self._coalesced,
            "gets": self._gets,
            "pending": len(self._dirty),
            "processing": len(self._processing),
            "queue_depth": self._buf.stats.get("msg_count", 0),
        }


async def run_worker(
    queue: DedupWorkQueue[T],
    handler: Callable[[T], Awaitable[None]],
    *,
    name: str = "dedup-worker",
) -> None:
    """Run a worker loop that processes items from a DedupWorkQueue.

    Convenience function for the common consumer pattern.

    Args:
        queue: The dedup work queue to consume from.
        handler: Async callable that processes each key.
        name: Worker name for logging.
    """
    logger.info("[%s] Worker started", name)
    while True:
        try:
            key = await queue.get()
        except ShutdownError:
            logger.info("[%s] Worker stopped (queue shut down)", name)
            return

        t0 = time.monotonic()
        try:
            await handler(key)
        except Exception:
            logger.exception("[%s] Handler error for key=%s", name, key)
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            queue.done(key)
            logger.debug("[%s] Processed key=%s in %.1fms", name, key, elapsed_ms)
