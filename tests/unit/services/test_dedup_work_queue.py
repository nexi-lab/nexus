"""Unit tests for DedupWorkQueue — coalescing work queue (Issue #2062).

Tests cover:
- Basic add/get/done lifecycle
- Coalescing: same key added multiple times → processed once
- Re-queue on done: key re-added during processing → re-queued
- Concurrent producers: multiple add() coroutines racing
- Concurrent consumers: multiple get() coroutines (one wins per key)
- Shutdown: pending get() raises ShutdownError
- Edge cases: done() without get(), add() after shutdown, len()
- FIFO ordering: keys come out in insertion order
- Benchmark: throughput under coalescing workload
"""

from __future__ import annotations

import asyncio
import collections
import time

import pytest

from nexus.services.lifecycle.dedup_work_queue import (
    DedupWorkQueue,
    ShutdownError,
    run_worker,
)

# ---------------------------------------------------------------------------
# Fake kernel — in-process pipe backed by collections.deque
# ---------------------------------------------------------------------------


class _FakeKernel:
    """Minimal kernel mock implementing the pipe IPC API used by DedupWorkQueue."""

    def __init__(self) -> None:
        self._pipes: dict[str, tuple[collections.deque[bytes], int, bool]] = {}
        # path → (deque, capacity_bytes, closed)

    def create_pipe(self, path: str, capacity: int) -> None:
        self._pipes[path] = (collections.deque(), capacity, False)

    def pipe_write_nowait(self, path: str, data: bytes) -> int:
        buf, cap, closed = self._pipes[path]
        if closed:
            raise RuntimeError(f"PipeClosed: {path}")
        current = sum(len(d) for d in buf)
        if current + len(data) > cap:
            raise RuntimeError(f"PipeFull: {path}")
        buf.append(data)
        return len(data)

    def pipe_read_nowait(self, path: str) -> bytes | None:
        buf, _cap, closed = self._pipes[path]
        if buf:
            return buf.popleft()
        if closed:
            raise RuntimeError(f"PipeClosed: {path}")
        return None

    def close_pipe(self, path: str) -> None:
        if path not in self._pipes:
            return
        buf, cap, _closed = self._pipes[path]
        self._pipes[path] = (buf, cap, True)

    def destroy_pipe(self, path: str) -> None:
        self._pipes.pop(path, None)

    def has_pipe(self, path: str) -> bool:
        return path in self._pipes


@pytest.fixture()
def fake_kernel() -> _FakeKernel:
    return _FakeKernel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _drain(q: DedupWorkQueue[str], count: int) -> list[str]:
    """Drain exactly `count` items, calling done() for each."""
    results: list[str] = []
    for _ in range(count):
        key = await asyncio.wait_for(q.get(), timeout=2.0)
        results.append(key)
        q.done(key)
    return results


# ---------------------------------------------------------------------------
# 1. Basic add/get/done lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_lifecycle(fake_kernel: _FakeKernel) -> None:
    """Single key: add → get → done works correctly."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    await q.add("a")
    assert len(q) == 1

    key = await asyncio.wait_for(q.get(), timeout=1.0)
    assert key == "a"
    assert q.processing_count == 1
    assert len(q) == 0

    q.done(key)
    assert q.processing_count == 0


# ---------------------------------------------------------------------------
# 2. Coalescing: same key added 10x → only 1 get()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalescing_same_key(fake_kernel: _FakeKernel) -> None:
    """Adding the same key 10 times yields only 1 item from get()."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    for _ in range(10):
        await q.add("file.txt")

    assert len(q) == 1
    assert q.metrics["coalesced"] == 9

    key = await asyncio.wait_for(q.get(), timeout=1.0)
    assert key == "file.txt"
    q.done(key)

    # Queue should now be empty — no more items
    assert len(q) == 0
    assert q.metrics["queue_depth"] == 0


# ---------------------------------------------------------------------------
# 3. Re-queue on done: add during processing → re-queued
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_requeue_on_done(fake_kernel: _FakeKernel) -> None:
    """Key re-added while processing gets re-queued after done()."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    # Add and start processing
    await q.add("key1")
    key = await asyncio.wait_for(q.get(), timeout=1.0)
    assert key == "key1"
    assert q.processing_count == 1

    # Re-add the same key while it's being processed
    await q.add("key1")
    assert len(q) == 1  # In dirty set

    # Complete processing — should trigger re-queue
    q.done(key)
    assert q.processing_count == 0

    # Key should be available again
    key2 = await asyncio.wait_for(q.get(), timeout=1.0)
    assert key2 == "key1"
    q.done(key2)

    # Now truly empty
    assert len(q) == 0
    assert q.metrics["queue_depth"] == 0


# ---------------------------------------------------------------------------
# 4. Concurrent producers: multiple add() coroutines racing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_producers(fake_kernel: _FakeKernel) -> None:
    """Many producers adding overlapping keys concurrently."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    keys = [f"key-{i % 5}" for i in range(50)]  # 50 adds, only 5 unique

    await asyncio.gather(*(q.add(k) for k in keys))

    assert len(q) == 5
    assert q.metrics["coalesced"] == 45

    results = await _drain(q, 5)
    assert sorted(results) == ["key-0", "key-1", "key-2", "key-3", "key-4"]


# ---------------------------------------------------------------------------
# 5. Concurrent consumers: multiple get() coroutines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_consumers(fake_kernel: _FakeKernel) -> None:
    """Multiple consumers each get a distinct key."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    for i in range(3):
        await q.add(f"item-{i}")

    async def consume() -> str:
        key = await asyncio.wait_for(q.get(), timeout=2.0)
        await asyncio.sleep(0.01)  # Simulate work
        q.done(key)
        return key

    results = await asyncio.gather(consume(), consume(), consume())
    assert sorted(results) == ["item-0", "item-1", "item-2"]
    assert len(q) == 0


# ---------------------------------------------------------------------------
# 6. Shutdown: pending get() raises ShutdownError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_unblocks_get(fake_kernel: _FakeKernel) -> None:
    """Shutdown causes pending get() to raise ShutdownError."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    async def delayed_shutdown() -> None:
        await asyncio.sleep(0.05)
        await q.shutdown()

    shutdown_task = asyncio.create_task(delayed_shutdown())

    with pytest.raises(ShutdownError):
        await asyncio.wait_for(q.get(), timeout=2.0)

    await shutdown_task


@pytest.mark.asyncio
async def test_add_after_shutdown_raises(fake_kernel: _FakeKernel) -> None:
    """add() after shutdown raises ShutdownError."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)
    await q.shutdown()

    with pytest.raises(ShutdownError):
        await q.add("x")


# ---------------------------------------------------------------------------
# 7. Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_without_get_is_safe(fake_kernel: _FakeKernel) -> None:
    """Calling done() for a key not in processing is a no-op."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)
    q.done("nonexistent")  # Should not raise
    assert q.processing_count == 0


@pytest.mark.asyncio
async def test_len_empty_queue(fake_kernel: _FakeKernel) -> None:
    """Empty queue has len 0 and correct metrics."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)
    assert len(q) == 0
    assert q.processing_count == 0
    assert q.metrics == {
        "adds": 0,
        "coalesced": 0,
        "gets": 0,
        "pending": 0,
        "processing": 0,
        "queue_depth": 0,
    }


@pytest.mark.asyncio
async def test_multiple_different_keys(fake_kernel: _FakeKernel) -> None:
    """Different keys are all enqueued independently."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    await q.add("a")
    await q.add("b")
    await q.add("c")
    assert len(q) == 3

    results = await _drain(q, 3)
    assert results == ["a", "b", "c"]  # FIFO order


# ---------------------------------------------------------------------------
# 8. FIFO ordering: keys come out in insertion order
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fifo_ordering(fake_kernel: _FakeKernel) -> None:
    """Keys are returned in FIFO (first-added) order."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    for i in range(10):
        await q.add(f"item-{i}")

    results: list[str] = []
    for _ in range(10):
        key = await asyncio.wait_for(q.get(), timeout=1.0)
        results.append(key)
        q.done(key)

    assert results == [f"item-{i}" for i in range(10)]


# ---------------------------------------------------------------------------
# 9. run_worker convenience function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_worker(fake_kernel: _FakeKernel) -> None:
    """run_worker processes items and stops on shutdown."""
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)
    processed: list[str] = []

    async def handler(key: str) -> None:
        processed.append(key)

    for i in range(5):
        await q.add(f"k{i}")

    # Start worker, then shut down after items are processed
    async def shutdown_after_drain() -> None:
        while len(processed) < 5:
            await asyncio.sleep(0.05)
        await q.shutdown()

    worker_task = asyncio.create_task(run_worker(q, handler, name="test-worker"))
    shutdown_task = asyncio.create_task(shutdown_after_drain())

    await asyncio.gather(worker_task, shutdown_task)
    assert sorted(processed) == ["k0", "k1", "k2", "k3", "k4"]


# ---------------------------------------------------------------------------
# 10. Tuple keys (generic type test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tuple_keys(fake_kernel: _FakeKernel) -> None:
    """DedupWorkQueue works with tuple keys (zone_id, path)."""
    q: DedupWorkQueue[tuple[str, str]] = DedupWorkQueue(kernel=fake_kernel)

    await q.add(("zone1", "/file.txt"))
    await q.add(("zone1", "/file.txt"))  # Coalesced
    await q.add(("zone2", "/file.txt"))  # Different zone — not coalesced

    assert len(q) == 2
    assert q.metrics["coalesced"] == 1

    results: list[tuple[str, str]] = []
    for _ in range(2):
        key = await asyncio.wait_for(q.get(), timeout=2.0)
        results.append(key)
        q.done(key)
    assert results == [("zone1", "/file.txt"), ("zone2", "/file.txt")]


# ---------------------------------------------------------------------------
# 11. Benchmark: throughput under coalescing workload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_benchmark_coalescing_throughput(fake_kernel: _FakeKernel) -> None:
    """Benchmark: 100K adds with high duplication + drain.

    Validates that coalescing overhead is negligible.
    Target: > 50K ops/sec (conservative for asyncio).
    """
    q: DedupWorkQueue[str] = DedupWorkQueue(kernel=fake_kernel)

    num_adds = 100_000
    num_unique_keys = 100  # High duplication ratio

    t0 = time.monotonic()
    for i in range(num_adds):
        await q.add(f"key-{i % num_unique_keys}")
    add_elapsed = time.monotonic() - t0

    assert len(q) == num_unique_keys
    assert q.metrics["coalesced"] == num_adds - num_unique_keys

    t1 = time.monotonic()
    drained = await _drain(q, num_unique_keys)
    drain_elapsed = time.monotonic() - t1

    assert len(drained) == num_unique_keys

    add_rate = num_adds / add_elapsed
    drain_rate = num_unique_keys / drain_elapsed

    # Conservative thresholds — CI runners vary widely in performance
    assert add_rate > 10_000, f"add rate too low: {add_rate:.0f} ops/sec"
    assert drain_rate > 1_000, f"drain rate too low: {drain_rate:.0f} ops/sec"
