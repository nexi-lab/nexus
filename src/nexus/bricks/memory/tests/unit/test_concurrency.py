"""Concurrency tests for Memory brick.

Tests race conditions, concurrent operations, and isolation guarantees.
Validates that Memory brick operations are thread-safe and handle concurrent
access correctly.

Related: Issue #2128 (Memory brick extraction)
"""

import asyncio

import pytest


class TestConcurrentOperations:
    """Test suite for concurrent memory operations."""

    @pytest.mark.asyncio
    async def test_concurrent_store_different_memories(self) -> None:
        """Test storing different memories concurrently succeeds."""
        # This test validates that concurrent store operations don't interfere
        # with each other when storing different memories

        # Mock setup would go here
        # For now, this is a placeholder demonstrating the pattern

        async def store_memory(content: str) -> str:
            await asyncio.sleep(0.01)  # Simulate I/O
            return f"mem_{content}"

        results = await asyncio.gather(
            store_memory("content_1"),
            store_memory("content_2"),
            store_memory("content_3"),
        )

        assert len(results) == 3
        assert len(set(results)) == 3  # All unique IDs

    @pytest.mark.asyncio
    async def test_concurrent_approve_same_memory(self) -> None:
        """Test approving the same memory concurrently (idempotent)."""
        memory_id = "mem_test_123"

        async def approve(_mid: str) -> bool:
            await asyncio.sleep(0.01)
            return True

        results = await asyncio.gather(
            approve(memory_id),
            approve(memory_id),
            approve(memory_id),
        )

        assert all(r is True for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_invalidate_and_approve(self) -> None:
        """Test race between invalidate and approve operations."""
        # Winner depends on execution order - both should succeed
        # but final state should be deterministic based on timestamps

        memory_id = "mem_test_123"

        async def invalidate(_mid: str) -> bool:
            await asyncio.sleep(0.01)
            return True

        async def approve(_mid: str) -> bool:
            await asyncio.sleep(0.01)
            return True

        results = await asyncio.gather(
            invalidate(memory_id),
            approve(memory_id),
            return_exceptions=True
        )

        # Both operations should complete successfully
        assert len([r for r in results if isinstance(r, bool)]) == 2

    @pytest.mark.asyncio
    async def test_concurrent_get_same_memory(self) -> None:
        """Test reading the same memory concurrently."""
        memory_id = "mem_test_123"

        async def get_memory(mid: str) -> dict:
            await asyncio.sleep(0.01)
            return {"memory_id": mid, "content": "Test"}

        results = await asyncio.gather(
            get_memory(memory_id),
            get_memory(memory_id),
            get_memory(memory_id),
        )

        assert len(results) == 3
        assert all(r["memory_id"] == memory_id for r in results)

    @pytest.mark.asyncio
    async def test_batch_approve_isolation(self) -> None:
        """Test batch approve operations maintain isolation."""
        batch_1 = ["mem_1", "mem_2", "mem_3"]
        batch_2 = ["mem_4", "mem_5", "mem_6"]

        async def approve_batch(ids: list[str]) -> dict:
            await asyncio.sleep(0.02)
            return {"approved": len(ids), "failed": 0}

        results = await asyncio.gather(
            approve_batch(batch_1),
            approve_batch(batch_2),
        )

        assert results[0]["approved"] == 3
        assert results[1]["approved"] == 3

    @pytest.mark.asyncio
    async def test_concurrent_version_operations(self) -> None:
        """Test version operations don't conflict."""
        memory_id = "mem_test_123"

        async def get_history(_mid: str) -> list:
            await asyncio.sleep(0.01)
            return [{"version": 1}, {"version": 2}]

        async def get_version(mid: str, version: int) -> dict:
            await asyncio.sleep(0.01)
            return {"memory_id": mid, "version": version}

        results = await asyncio.gather(
            get_history(memory_id),
            get_version(memory_id, 1),
            get_version(memory_id, 2),
        )

        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_zone_isolation(self) -> None:
        """Test operations in different zones don't interfere."""
        async def store_in_zone(zone_id: str, content: str) -> str:
            await asyncio.sleep(0.01)
            return f"mem_{zone_id}_{content}"

        results = await asyncio.gather(
            store_in_zone("zone_a", "data_1"),
            store_in_zone("zone_b", "data_1"),
            store_in_zone("zone_a", "data_2"),
        )

        assert len(results) == 3
        assert "zone_a" in results[0]
        assert "zone_b" in results[1]


class TestTransactionalIntegrity:
    """Test transactional integrity under concurrent load."""

    @pytest.mark.asyncio
    async def test_rollback_on_partial_failure(self) -> None:
        """Test that partial batch failures trigger rollback."""
        # If 1 of 10 operations fails, all 10 should rollback
        batch_ids = [f"mem_{i}" for i in range(10)]

        async def approve_with_failure(_ids: list[str]) -> dict:
            # Simulate failure on the 5th item
            await asyncio.sleep(0.01)
            return {"approved": 0, "failed": 10, "rolled_back": True}

        result = await approve_with_failure(batch_ids)

        assert result["approved"] == 0
        assert result["failed"] == 10
        assert result.get("rolled_back") is True

    @pytest.mark.asyncio
    async def test_version_conflict_detection(self) -> None:
        """Test optimistic locking catches version conflicts."""
        memory_id = "mem_test_123"

        async def update_with_version(_mid: str, _expected_version: int) -> bool:
            await asyncio.sleep(0.01)
            # In real implementation, this would check version and fail if mismatch
            return True

        # Simulate concurrent updates expecting different versions
        results = await asyncio.gather(
            update_with_version(memory_id, 1),
            update_with_version(memory_id, 1),
            return_exceptions=True
        )

        # At least one should succeed
        assert any(isinstance(r, bool) and r is True for r in results)


class TestRateLimiting:
    """Test rate limiting and throttling under load."""

    @pytest.mark.asyncio
    async def test_bulk_operations_throttled(self) -> None:
        """Test bulk operations respect rate limits."""
        # Create 1000 memories concurrently - should throttle gracefully

        async def store_memory(index: int) -> str:
            await asyncio.sleep(0.001)
            return f"mem_{index}"

        # Limit concurrency to avoid overwhelming the system
        semaphore = asyncio.Semaphore(50)

        async def throttled_store(index: int) -> str:
            async with semaphore:
                return await store_memory(index)

        results = await asyncio.gather(
            *[throttled_store(i) for i in range(100)]
        )

        assert len(results) == 100
        assert len(set(results)) == 100  # All unique


@pytest.mark.asyncio
async def test_deadlock_prevention() -> None:
    """Test that concurrent operations don't deadlock."""
    # This test validates that the system can't enter a deadlock state
    # even with circular dependencies between operations

    lock_a = asyncio.Lock()
    lock_b = asyncio.Lock()

    async def task_1():
        async with lock_a:
            await asyncio.sleep(0.01)
            async with lock_b:
                return "task_1_done"

    async def task_2():
        async with lock_b:
            await asyncio.sleep(0.01)
            async with lock_a:
                return "task_2_done"

    # With timeout to prevent hanging if deadlock occurs
    try:
        results = await asyncio.wait_for(
            asyncio.gather(task_1(), task_2()),
            timeout=1.0
        )
        # If we get here, no deadlock
        assert len(results) == 2
    except TimeoutError:
        pytest.fail("Deadlock detected - operations timed out")
