"""Tests for OfflineQueueProtocol and InMemoryQueue (#10-A)."""

import pytest

from nexus.proxy.queue_protocol import InMemoryQueue, QueueFullError


@pytest.fixture()
def queue() -> InMemoryQueue:
    return InMemoryQueue(max_size=10_000)


class TestInMemoryEnqueueDequeue:
    async def test_basic_round_trip(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        assert op_id >= 1

        batch = await queue.dequeue_batch(10)
        assert len(batch) == 1
        assert batch[0].id == op_id
        assert batch[0].method == "read"


class TestInMemoryMarkDone:
    async def test_done_op_disappears(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        batch = await queue.dequeue_batch(10)
        assert len(batch) == 1

        await queue.mark_done(op_id)
        assert await queue.pending_count() == 0


class TestInMemoryMarkFailed:
    async def test_retry_count_incremented(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})

        # Dequeue to get the op (moves to in-flight)
        batch = await queue.dequeue_batch(10)
        assert batch[0].retry_count == 0

        # Mark failed — should re-enqueue from in-flight with retry_count + 1
        await queue.mark_failed(op_id)
        batch2 = await queue.dequeue_batch(10)
        assert len(batch2) == 1
        assert batch2[0].retry_count == 1

    async def test_mark_failed_on_non_dequeued_op(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("write", kwargs={"path": "/b"})
        # Mark failed without dequeuing first (fallback path)
        await queue.mark_failed(op_id)
        batch = await queue.dequeue_batch(10)
        assert len(batch) == 1
        assert batch[0].retry_count == 1


class TestInMemoryMarkDeadLetter:
    async def test_dead_letter_removed(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.mark_dead_letter(op_id)
        assert await queue.pending_count() == 0


class TestInMemoryPendingCount:
    async def test_counts_only_pending(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.enqueue("write", kwargs={"path": "/b"})
        assert await queue.pending_count() == 2

        batch = await queue.dequeue_batch(1)
        await queue.mark_done(batch[0].id)
        assert await queue.pending_count() == 1


class TestInMemoryDequeueBatchRespectsSize:
    async def test_respects_limit(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        for i in range(5):
            await queue.enqueue("read", kwargs={"path": f"/{i}"})

        batch = await queue.dequeue_batch(3)
        assert len(batch) == 3


class TestInMemoryMaxSizeEnforced:
    async def test_queue_full_error(self) -> None:
        small_queue = InMemoryQueue(max_size=3)
        await small_queue.initialize()
        await small_queue.enqueue("a")
        await small_queue.enqueue("b")
        await small_queue.enqueue("c")

        with pytest.raises(QueueFullError):
            await small_queue.enqueue("d")


class TestInMemoryCloseClears:
    async def test_empty_after_close(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.close()
        assert await queue.pending_count() == 0


class TestInMemoryInitializeIdempotent:
    async def test_safe_to_call_twice(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.initialize()  # Should not throw
        assert await queue.pending_count() == 1


class TestInMemoryInFlightTracking:
    """Bug #4: dequeued ops must survive mark_failed / mark_done."""

    async def test_mark_done_clears_in_flight(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        batch = await queue.dequeue_batch(10)
        assert len(batch) == 1

        await queue.mark_done(op_id)
        assert await queue.pending_count() == 0

    async def test_mark_dead_letter_clears_in_flight(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.dequeue_batch(10)

        await queue.mark_dead_letter(op_id)
        assert await queue.pending_count() == 0

    async def test_pending_count_includes_in_flight(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        await queue.enqueue("read", kwargs={"path": "/a"})
        await queue.enqueue("write", kwargs={"path": "/b"})

        # Dequeue 1 — still 2 pending (1 in-flight + 1 in queue)
        await queue.dequeue_batch(1)
        assert await queue.pending_count() == 2


class TestInMemoryHasIdempotencyKey:
    """Bug #5: persistent idempotency tracking in InMemoryQueue."""

    async def test_done_key_is_tracked(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        op_id = await queue.enqueue("read", kwargs={"path": "/a"})
        batch = await queue.dequeue_batch(10)
        idem_key = batch[0].idempotency_key
        assert idem_key is not None

        await queue.mark_done(op_id)
        assert await queue.has_idempotency_key(idem_key) is True

    async def test_unknown_key_returns_false(self, queue: InMemoryQueue) -> None:
        await queue.initialize()
        assert await queue.has_idempotency_key("nonexistent") is False
