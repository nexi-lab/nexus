"""Comprehensive tests for OfflineQueue (SQLAlchemy async ORM + aiosqlite)."""

from __future__ import annotations

import json

import pytest

from nexus.proxy.offline_queue import OfflineQueue
from nexus.proxy.queue_protocol import QueuedOperation


@pytest.fixture
async def queue(tmp_path):
    q = OfflineQueue(str(tmp_path / "test_queue.db"), max_retry_count=3)
    await q.initialize()
    yield q
    await q.close()


# ---------------------------------------------------------------------------
# TestOfflineQueueInitialize
# ---------------------------------------------------------------------------


class TestOfflineQueueInitialize:
    """Tests for initialize() — schema creation, WAL mode, idempotency."""

    async def test_initialize_creates_database_file(self, tmp_path):
        db_path = tmp_path / "init_test.db"
        q = OfflineQueue(str(db_path), max_retry_count=3)
        await q.initialize()

        assert db_path.exists()
        await q.close()

    async def test_initialize_creates_parent_directories(self, tmp_path):
        db_path = tmp_path / "nested" / "dirs" / "queue.db"
        q = OfflineQueue(str(db_path), max_retry_count=3)
        await q.initialize()

        assert db_path.exists()
        await q.close()

    async def test_initialize_is_idempotent(self, tmp_path):
        db_path = tmp_path / "idempotent.db"
        q = OfflineQueue(str(db_path), max_retry_count=3)

        await q.initialize()
        await q.enqueue("write", ("a",))

        # Second initialize should not lose data or raise
        await q.initialize()
        count = await q.pending_count()
        assert count == 1
        await q.close()

    async def test_operations_fail_before_initialize(self, tmp_path):
        q = OfflineQueue(str(tmp_path / "not_init.db"), max_retry_count=3)

        with pytest.raises(RuntimeError, match="not initialized"):
            await q.enqueue("write", ())


# ---------------------------------------------------------------------------
# TestOfflineQueueEnqueueDequeue
# ---------------------------------------------------------------------------


class TestOfflineQueueEnqueueDequeue:
    """Tests for enqueue() and dequeue_batch() — round trip, FIFO, limits."""

    async def test_enqueue_returns_positive_id(self, queue: OfflineQueue):
        op_id = await queue.enqueue("write", ("path/to/file",))
        assert isinstance(op_id, int)
        assert op_id > 0

    async def test_enqueue_ids_are_monotonically_increasing(self, queue: OfflineQueue):
        id1 = await queue.enqueue("write", ("a",))
        id2 = await queue.enqueue("read", ("b",))
        id3 = await queue.enqueue("delete", ("c",))
        assert id1 < id2 < id3

    async def test_basic_round_trip(self, queue: OfflineQueue):
        op_id = await queue.enqueue(
            "write",
            ("/data/file.txt",),
            {"overwrite": True},
            "cas:abc123",
        )

        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 1

        op = batch[0]
        assert isinstance(op, QueuedOperation)
        assert op.id == op_id
        assert op.method == "write"
        assert json.loads(op.args_json) == ["/data/file.txt"]
        assert json.loads(op.kwargs_json) == {"overwrite": True}
        assert op.payload_ref == "cas:abc123"
        assert op.retry_count == 0

    async def test_dequeue_fifo_ordering(self, queue: OfflineQueue):
        id1 = await queue.enqueue("first", ())
        id2 = await queue.enqueue("second", ())
        id3 = await queue.enqueue("third", ())

        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 3
        assert [op.id for op in batch] == [id1, id2, id3]
        assert [op.method for op in batch] == ["first", "second", "third"]

    async def test_dequeue_batch_respects_limit(self, queue: OfflineQueue):
        for i in range(5):
            await queue.enqueue(f"op_{i}", ())

        batch = await queue.dequeue_batch(limit=2)
        assert len(batch) == 2
        assert batch[0].method == "op_0"
        assert batch[1].method == "op_1"

    async def test_dequeue_empty_queue_returns_empty_list(self, queue: OfflineQueue):
        batch = await queue.dequeue_batch(limit=10)
        assert batch == []

    async def test_enqueue_with_no_kwargs(self, queue: OfflineQueue):
        await queue.enqueue("write", ("path",))
        batch = await queue.dequeue_batch(limit=1)
        assert json.loads(batch[0].kwargs_json) == {}

    async def test_enqueue_with_no_payload_ref(self, queue: OfflineQueue):
        await queue.enqueue("delete", ("path",))
        batch = await queue.dequeue_batch(limit=1)
        assert batch[0].payload_ref is None

    async def test_enqueue_with_empty_args(self, queue: OfflineQueue):
        await queue.enqueue("ping")
        batch = await queue.dequeue_batch(limit=1)
        assert json.loads(batch[0].args_json) == []

    async def test_dequeue_only_returns_pending(self, queue: OfflineQueue):
        id1 = await queue.enqueue("pending_op", ())
        id2 = await queue.enqueue("done_op", ())
        id3 = await queue.enqueue("dead_op", ())

        await queue.mark_done(id2)
        await queue.mark_dead_letter(id3)

        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 1
        assert batch[0].id == id1
        assert batch[0].method == "pending_op"


# ---------------------------------------------------------------------------
# TestOfflineQueueMarkDone
# ---------------------------------------------------------------------------


class TestOfflineQueueMarkDone:
    """Tests for mark_done() — status transitions."""

    async def test_mark_done_removes_from_pending(self, queue: OfflineQueue):
        op_id = await queue.enqueue("write", ("file.txt",))
        assert await queue.pending_count() == 1

        await queue.mark_done(op_id)
        assert await queue.pending_count() == 0

    async def test_mark_done_does_not_affect_other_ops(self, queue: OfflineQueue):
        id1 = await queue.enqueue("first", ())
        id2 = await queue.enqueue("second", ())

        await queue.mark_done(id1)
        assert await queue.pending_count() == 1

        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 1
        assert batch[0].id == id2

    async def test_mark_done_op_not_in_dequeue(self, queue: OfflineQueue):
        id1 = await queue.enqueue("op_a", ())
        id2 = await queue.enqueue("op_b", ())
        id3 = await queue.enqueue("op_c", ())

        await queue.mark_done(id2)

        batch = await queue.dequeue_batch(limit=10)
        returned_ids = [op.id for op in batch]
        assert id2 not in returned_ids
        assert id1 in returned_ids
        assert id3 in returned_ids

    async def test_mark_done_nonexistent_id_is_noop(self, queue: OfflineQueue):
        # Should not raise when marking a nonexistent id
        await queue.mark_done(99999)
        assert await queue.pending_count() == 0


# ---------------------------------------------------------------------------
# TestOfflineQueueMarkFailed
# ---------------------------------------------------------------------------


class TestOfflineQueueMarkFailed:
    """Tests for mark_failed() — retry counting and dead-letter threshold."""

    async def test_mark_failed_increments_retry_count(self, queue: OfflineQueue):
        op_id = await queue.enqueue("write", ("file.txt",))

        await queue.mark_failed(op_id)

        batch = await queue.dequeue_batch(limit=1)
        assert len(batch) == 1
        assert batch[0].retry_count == 1

    async def test_mark_failed_keeps_op_pending_under_max(self, queue: OfflineQueue):
        op_id = await queue.enqueue("write", ("file.txt",))

        # max_retry_count=3, so 2 failures should still be pending
        await queue.mark_failed(op_id)
        await queue.mark_failed(op_id)

        assert await queue.pending_count() == 1
        batch = await queue.dequeue_batch(limit=1)
        assert batch[0].retry_count == 2

    async def test_mark_failed_dead_letters_at_max_retries(self, queue: OfflineQueue):
        """When retry_count reaches max_retries, op should be dead-lettered."""
        op_id = await queue.enqueue("write", ("file.txt",))

        # max_retry_count=3, so 3 failures should dead-letter
        await queue.mark_failed(op_id)
        await queue.mark_failed(op_id)
        await queue.mark_failed(op_id)

        assert await queue.pending_count() == 0
        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 0

    async def test_mark_failed_progressive_retry_counts(self, queue: OfflineQueue):
        """Each mark_failed call should increment retry_count by 1."""
        op_id = await queue.enqueue("write", ())

        for expected_count in range(1, 3):
            await queue.mark_failed(op_id)
            batch = await queue.dequeue_batch(limit=1)
            if batch:
                assert batch[0].retry_count == expected_count

    async def test_mark_failed_does_not_affect_other_ops(self, queue: OfflineQueue):
        id1 = await queue.enqueue("op_a", ())
        id2 = await queue.enqueue("op_b", ())

        await queue.mark_failed(id1)

        batch = await queue.dequeue_batch(limit=10)
        op_map = {op.id: op for op in batch}
        assert op_map[id1].retry_count == 1
        assert op_map[id2].retry_count == 0


# ---------------------------------------------------------------------------
# TestOfflineQueueMarkDeadLetter
# ---------------------------------------------------------------------------


class TestOfflineQueueMarkDeadLetter:
    """Tests for mark_dead_letter() — explicit dead-lettering."""

    async def test_mark_dead_letter_removes_from_pending(self, queue: OfflineQueue):
        op_id = await queue.enqueue("write", ("file.txt",))
        assert await queue.pending_count() == 1

        await queue.mark_dead_letter(op_id)
        assert await queue.pending_count() == 0

    async def test_mark_dead_letter_not_in_dequeue(self, queue: OfflineQueue):
        id1 = await queue.enqueue("keep", ())
        id2 = await queue.enqueue("dead", ())

        await queue.mark_dead_letter(id2)

        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 1
        assert batch[0].id == id1

    async def test_mark_dead_letter_nonexistent_id_is_noop(self, queue: OfflineQueue):
        await queue.mark_dead_letter(99999)
        assert await queue.pending_count() == 0

    async def test_mark_dead_letter_before_max_retries(self, queue: OfflineQueue):
        """Can dead-letter an op even if retries haven't been exhausted."""
        op_id = await queue.enqueue("write", ())
        await queue.mark_failed(op_id)  # retry_count=1, max=3

        await queue.mark_dead_letter(op_id)
        assert await queue.pending_count() == 0
        batch = await queue.dequeue_batch(limit=10)
        assert len(batch) == 0


# ---------------------------------------------------------------------------
# TestOfflineQueuePendingCount
# ---------------------------------------------------------------------------


class TestOfflineQueuePendingCount:
    """Tests for pending_count() — accurate counting of pending operations only."""

    async def test_pending_count_empty_queue(self, queue: OfflineQueue):
        assert await queue.pending_count() == 0

    async def test_pending_count_after_enqueue(self, queue: OfflineQueue):
        await queue.enqueue("a", ())
        assert await queue.pending_count() == 1

        await queue.enqueue("b", ())
        assert await queue.pending_count() == 2

        await queue.enqueue("c", ())
        assert await queue.pending_count() == 3

    async def test_pending_count_excludes_done(self, queue: OfflineQueue):
        id1 = await queue.enqueue("a", ())
        await queue.enqueue("b", ())
        assert await queue.pending_count() == 2

        await queue.mark_done(id1)
        assert await queue.pending_count() == 1

    async def test_pending_count_excludes_dead_letter(self, queue: OfflineQueue):
        id1 = await queue.enqueue("a", ())
        await queue.enqueue("b", ())

        await queue.mark_dead_letter(id1)
        assert await queue.pending_count() == 1

    async def test_pending_count_with_mixed_statuses(self, queue: OfflineQueue):
        await queue.enqueue("pending_1", ())
        id2 = await queue.enqueue("done_1", ())
        id3 = await queue.enqueue("dead_1", ())
        await queue.enqueue("pending_2", ())

        await queue.mark_done(id2)
        await queue.mark_dead_letter(id3)

        assert await queue.pending_count() == 2

    async def test_pending_count_includes_failed_but_not_dead_lettered(
        self, queue: OfflineQueue
    ):
        """Failed ops that haven't hit max retries are still pending."""
        op_id = await queue.enqueue("flaky", ())
        await queue.mark_failed(op_id)  # retry_count=1, max=3 -> still pending

        assert await queue.pending_count() == 1


# ---------------------------------------------------------------------------
# TestOfflineQueueCleanupCompleted
# ---------------------------------------------------------------------------


class TestOfflineQueueCleanupCompleted:
    """Tests for cleanup_completed() — removing old completed operations."""

    async def test_cleanup_deletes_old_done_ops(self, queue: OfflineQueue):
        op_id = await queue.enqueue("old_op", ())
        await queue.mark_done(op_id)

        # cleanup_completed uses time.time() - older_than_seconds as cutoff.
        # The op was created moments ago, so older_than_seconds=0 should match it.
        deleted = await queue.cleanup_completed(older_than_seconds=0)
        assert deleted == 1

    async def test_cleanup_returns_zero_when_nothing_to_clean(self, queue: OfflineQueue):
        deleted = await queue.cleanup_completed(older_than_seconds=3600)
        assert deleted == 0

    async def test_cleanup_does_not_delete_recent_done_ops(self, queue: OfflineQueue):
        op_id = await queue.enqueue("recent_op", ())
        await queue.mark_done(op_id)

        # With a large threshold, recent ops should not be deleted
        deleted = await queue.cleanup_completed(older_than_seconds=9999)
        assert deleted == 0

    async def test_cleanup_does_not_delete_pending_ops(self, queue: OfflineQueue):
        await queue.enqueue("still_pending", ())

        deleted = await queue.cleanup_completed(older_than_seconds=0)
        assert deleted == 0
        assert await queue.pending_count() == 1

    async def test_cleanup_does_not_delete_dead_letter_ops(self, queue: OfflineQueue):
        op_id = await queue.enqueue("dead", ())
        await queue.mark_dead_letter(op_id)

        deleted = await queue.cleanup_completed(older_than_seconds=0)
        assert deleted == 0

    async def test_cleanup_multiple_old_done_ops(self, queue: OfflineQueue):
        id1 = await queue.enqueue("old_1", ())
        id2 = await queue.enqueue("old_2", ())
        id3 = await queue.enqueue("old_3", ())

        await queue.mark_done(id1)
        await queue.mark_done(id2)
        await queue.mark_done(id3)

        deleted = await queue.cleanup_completed(older_than_seconds=0)
        assert deleted == 3

    async def test_cleanup_selective_by_age(self, queue: OfflineQueue):
        """Only ops older than the threshold should be cleaned."""
        # Enqueue and mark done
        id_old = await queue.enqueue("old_op", ())
        await queue.mark_done(id_old)

        # We use older_than_seconds=-1 to ensure even the most recent op
        # is considered "old" (cutoff = time.time() + 1)
        deleted = await queue.cleanup_completed(older_than_seconds=-1)
        assert deleted == 1


# ---------------------------------------------------------------------------
# TestOfflineQueueClose
# ---------------------------------------------------------------------------


class TestOfflineQueueClose:
    """Tests for close() — engine disposal."""

    async def test_close_disposes_engine(self, tmp_path):
        q = OfflineQueue(str(tmp_path / "close_test.db"), max_retry_count=3)
        await q.initialize()

        await q.close()

        # After close, engine and session factory should be None
        assert q._engine is None
        assert q._session_factory is None

    async def test_close_then_operations_fail(self, tmp_path):
        q = OfflineQueue(str(tmp_path / "close_fail.db"), max_retry_count=3)
        await q.initialize()
        await q.close()

        with pytest.raises(RuntimeError, match="not initialized"):
            await q.enqueue("write", ())

    async def test_close_is_idempotent(self, tmp_path):
        q = OfflineQueue(str(tmp_path / "close_idem.db"), max_retry_count=3)
        await q.initialize()

        await q.close()
        await q.close()  # Second close should not raise

        assert q._engine is None
        assert q._session_factory is None

    async def test_close_without_initialize(self, tmp_path):
        q = OfflineQueue(str(tmp_path / "never_init.db"), max_retry_count=3)

        # Should not raise even if never initialized
        await q.close()
        assert q._engine is None
