"""Unit tests for OfflineQueue."""

from __future__ import annotations

import asyncio
import os

from nexus.proxy.offline_queue import OfflineQueue


async def _make_queue(tmp_path, max_retry_count: int = 3) -> OfflineQueue:  # noqa: ANN001
    db_path = str(tmp_path / "test_queue.db")
    q = OfflineQueue(db_path, max_retry_count=max_retry_count)
    await q.initialize()
    return q


class TestOfflineQueue:
    async def test_enqueue_dequeue_fifo(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            id1 = await queue.enqueue("read", kwargs={"path": "/a"})
            id2 = await queue.enqueue("write", kwargs={"path": "/b"})
            id3 = await queue.enqueue("mkdir", kwargs={"path": "/c"})

            batch = await queue.dequeue_batch(limit=10)
            assert len(batch) == 3
            assert batch[0].id == id1
            assert batch[0].method == "read"
            assert batch[1].id == id2
            assert batch[1].method == "write"
            assert batch[2].id == id3
            assert batch[2].method == "mkdir"
        finally:
            await queue.close()

    async def test_mark_done_removes_from_pending(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            op_id = await queue.enqueue("read", kwargs={"path": "/a"})
            await queue.mark_done(op_id)

            batch = await queue.dequeue_batch()
            assert len(batch) == 0
            assert await queue.pending_count() == 0
        finally:
            await queue.close()

    async def test_mark_failed_increments_retry(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            op_id = await queue.enqueue("read", kwargs={"path": "/a"})
            await queue.mark_failed(op_id)

            batch = await queue.dequeue_batch()
            assert len(batch) == 1
            assert batch[0].retry_count == 1
        finally:
            await queue.close()

    async def test_max_retries_dead_letter(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            op_id = await queue.enqueue("read", kwargs={"path": "/a"})
            for _ in range(3):
                await queue.mark_failed(op_id)

            batch = await queue.dequeue_batch()
            assert len(batch) == 0
            assert await queue.pending_count() == 0
        finally:
            await queue.close()

    async def test_mark_dead_letter_explicit(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            op_id = await queue.enqueue("read", kwargs={"path": "/a"})
            await queue.mark_dead_letter(op_id)
            assert await queue.pending_count() == 0
        finally:
            await queue.close()

    async def test_pending_count(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            assert await queue.pending_count() == 0
            await queue.enqueue("op1")
            await queue.enqueue("op2")
            assert await queue.pending_count() == 2
        finally:
            await queue.close()

    async def test_batch_limit(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            for i in range(10):
                await queue.enqueue(f"op{i}")
            batch = await queue.dequeue_batch(limit=3)
            assert len(batch) == 3
        finally:
            await queue.close()

    async def test_crash_recovery_preserves_queue(self, tmp_path) -> None:  # noqa: ANN001
        db_path = str(tmp_path / "crash_test.db")

        q1 = OfflineQueue(db_path, max_retry_count=3)
        await q1.initialize()
        await q1.enqueue("important_op", kwargs={"key": "value"})
        await q1.close()

        q2 = OfflineQueue(db_path, max_retry_count=3)
        await q2.initialize()
        batch = await q2.dequeue_batch()
        assert len(batch) == 1
        assert batch[0].method == "important_op"
        await q2.close()

    async def test_cleanup_completed(self, tmp_path) -> None:  # noqa: ANN001
        queue = await _make_queue(tmp_path)
        try:
            op_id = await queue.enqueue("read", kwargs={"path": "/a"})
            await queue.mark_done(op_id)
            removed = await queue.cleanup_completed(older_than_seconds=0)
            assert removed == 1
        finally:
            await queue.close()

    async def test_concurrent_enqueue_during_dequeue(self, tmp_path) -> None:  # noqa: ANN001
        """Enqueue and dequeue concurrently without data corruption."""
        queue = await _make_queue(tmp_path)
        try:

            async def enqueue_batch() -> None:
                for i in range(20):
                    await queue.enqueue(f"concurrent_{i}")

            async def dequeue_loop() -> list[int]:
                ids = []
                for _ in range(5):
                    batch = await queue.dequeue_batch(limit=5)
                    ids.extend(op.id for op in batch)
                    for op in batch:
                        await queue.mark_done(op.id)
                    await asyncio.sleep(0.01)
                return ids

            await asyncio.gather(enqueue_batch(), dequeue_loop())
            remaining = await queue.dequeue_batch(limit=100)
            for op in remaining:
                assert op.method.startswith("concurrent_")
        finally:
            await queue.close()

    async def test_expanduser_in_path(self, tmp_path, monkeypatch) -> None:  # noqa: ANN001
        """Queue expands ~ in db_path."""
        monkeypatch.setenv("HOME", str(tmp_path))
        q = OfflineQueue("~/test_queue.db")
        await q.initialize()
        assert os.path.exists(tmp_path / "test_queue.db")
        await q.close()
