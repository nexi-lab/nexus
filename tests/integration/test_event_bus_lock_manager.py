"""Integration tests for distributed event system.

These tests verify the Redis Pub/Sub integration for distributed file events
and locking across multiple Nexus nodes.

Requirements:
- Redis or Dragonfly running at localhost:6379 (or NEXUS_REDIS_URL)
- Use pytest marker `@pytest.mark.redis` to skip if Redis unavailable

Related: Issue #1106 Block 2
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from nexus.core.distributed_lock import RedisLockManager
    from nexus.core.event_bus import RedisEventBus

# Skip entire module if Redis is not available
pytestmark = pytest.mark.skipif(
    not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
    reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
async def redis_client():
    """Create a DragonflyClient for testing."""
    from nexus.core.cache.dragonfly import DragonflyClient

    redis_url = os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6379")
    client = DragonflyClient(url=redis_url)

    # Connect first, then verify
    try:
        await client.connect()
        is_healthy = await client.health_check()
        if not is_healthy:
            pytest.skip("Redis not healthy")
    except Exception as e:
        pytest.skip(f"Redis connection failed: {e}")

    yield client

    # Cleanup
    await client.disconnect()


@pytest.fixture
async def event_bus(redis_client):
    """Create a RedisEventBus for testing."""
    from nexus.core.event_bus import RedisEventBus

    bus = RedisEventBus(redis_client)
    await bus.start()

    yield bus

    await bus.stop()


@pytest.fixture
async def lock_manager(redis_client):
    """Create a RedisLockManager for testing."""
    from nexus.core.distributed_lock import RedisLockManager

    manager = RedisLockManager(redis_client)

    yield manager


# =============================================================================
# Event Bus Integration Tests
# =============================================================================


class TestRedisEventBusIntegration:
    """Integration tests for RedisEventBus with real Redis."""

    @pytest.mark.asyncio
    async def test_publish_event(self, event_bus):
        """Test publishing an event to Redis."""
        from nexus.core.event_bus import FileEvent, FileEventType

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            tenant_id="test-tenant",
        )

        # Publish should not raise
        num_subscribers = await event_bus.publish(event)
        # May be 0 if no subscribers, but should not raise
        assert num_subscribers >= 0

    @pytest.mark.asyncio
    async def test_wait_for_event_timeout(self, event_bus):
        """Test that wait_for_event times out correctly."""

        # Wait for event that won't come
        result = await event_bus.wait_for_event(
            tenant_id="test-tenant",
            path_pattern="/nonexistent/",
            timeout=0.5,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_publish_and_receive_event(self, redis_client):
        """Test publishing and receiving an event."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        # Use unique tenant to avoid cross-test contamination in parallel runs
        tenant_id = f"pubsub-test-{uuid.uuid4().hex[:8]}"

        # Create two separate bus instances (simulating different nodes)
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                tenant_id=tenant_id,
                size=1024,
            )

            # Start waiting in background
            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/",
                    timeout=5.0,
                )

            wait_task = asyncio.create_task(wait_for_event())

            # Give subscriber time to subscribe
            await asyncio.sleep(0.2)

            # Publish event
            await publisher.publish(event)

            # Wait for result
            received = await wait_task

            assert received is not None
            assert received.type == "file_write"
            assert received.path == "/inbox/test.txt"
            assert received.tenant_id == tenant_id
            assert received.size == 1024

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_event_filtering_by_path(self, redis_client):
        """Test that events are filtered by path pattern."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        # Use unique tenant to avoid cross-test contamination in parallel runs
        tenant_id = f"filter-test-{uuid.uuid4().hex[:8]}"

        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            # Subscriber watches /inbox/
            async def wait_for_inbox_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_inbox_event())
            await asyncio.sleep(0.2)

            # Publish event to /other/ (should not match)
            other_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/other/file.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(other_event)

            # Publish event to /inbox/ (should match)
            inbox_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/file.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(inbox_event)

            received = await wait_task

            # Should receive the inbox event, not the other event
            assert received is not None
            assert received.path == "/inbox/file.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_multi_tenant_isolation(self, redis_client):
        """Test that events are isolated per tenant."""
        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            # Subscriber watches tenant-A
            async def wait_for_tenant_a_event():
                return await subscriber.wait_for_event(
                    tenant_id="tenant-A",
                    path_pattern="/inbox/",
                    timeout=1.0,
                )

            wait_task = asyncio.create_task(wait_for_tenant_a_event())
            await asyncio.sleep(0.2)

            # Publish event to tenant-B (should not be received)
            tenant_b_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/file.txt",
                tenant_id="tenant-B",
            )
            await publisher.publish(tenant_b_event)

            # Wait should timeout (no matching tenant)
            received = await wait_task

            assert received is None

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_health_check(self, event_bus):
        """Test event bus health check with real Redis."""
        result = await event_bus.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_stats(self, event_bus):
        """Test getting event bus stats."""
        stats = await event_bus.get_stats()

        assert stats["backend"] == "redis_pubsub"
        assert stats["status"] == "running"
        assert "channel_prefix" in stats


# =============================================================================
# Lock Manager Integration Tests
# =============================================================================


class TestRedisLockManagerIntegration:
    """Integration tests for RedisLockManager with real Redis."""

    @pytest.mark.asyncio
    async def test_acquire_and_release_lock(self, lock_manager):
        """Test basic lock acquire and release."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/file.txt",
            timeout=5.0,
            ttl=30.0,
        )

        assert lock_id is not None
        assert len(lock_id) == 36  # UUID format

        # Verify lock exists
        is_locked = await lock_manager.is_locked("test-tenant", "/file.txt")
        assert is_locked is True

        # Release
        released = await lock_manager.release(lock_id, "test-tenant", "/file.txt")
        assert released is True

        # Verify lock is gone
        is_locked = await lock_manager.is_locked("test-tenant", "/file.txt")
        assert is_locked is False

    @pytest.mark.asyncio
    async def test_lock_exclusivity(self, lock_manager):
        """Test that locks are exclusive."""
        # First lock
        lock_id1 = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/exclusive.txt",
            timeout=5.0,
        )
        assert lock_id1 is not None

        try:
            # Second lock should fail (timeout)
            lock_id2 = await lock_manager.acquire(
                tenant_id="test-tenant",
                path="/exclusive.txt",
                timeout=0.3,
            )
            assert lock_id2 is None

        finally:
            await lock_manager.release(lock_id1, "test-tenant", "/exclusive.txt")

    @pytest.mark.asyncio
    async def test_lock_extend_heartbeat(self, lock_manager):
        """Test lock extension (heartbeat pattern)."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/heartbeat.txt",
            timeout=5.0,
            ttl=2.0,  # Short TTL
        )
        assert lock_id is not None

        try:
            # Extend lock
            extended = await lock_manager.extend(lock_id, "test-tenant", "/heartbeat.txt", ttl=30.0)
            assert extended is True

            # Verify still locked
            is_locked = await lock_manager.is_locked("test-tenant", "/heartbeat.txt")
            assert is_locked is True

        finally:
            await lock_manager.release(lock_id, "test-tenant", "/heartbeat.txt")

    @pytest.mark.asyncio
    async def test_lock_auto_expiry(self, lock_manager):
        """Test that locks auto-expire after TTL."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/expiring.txt",
            timeout=5.0,
            ttl=0.5,  # Very short TTL for testing
        )
        assert lock_id is not None

        # Lock should exist
        is_locked = await lock_manager.is_locked("test-tenant", "/expiring.txt")
        assert is_locked is True

        # Wait for TTL to expire
        await asyncio.sleep(0.7)

        # Lock should be gone
        is_locked = await lock_manager.is_locked("test-tenant", "/expiring.txt")
        assert is_locked is False

        # Release should fail (already expired)
        released = await lock_manager.release(lock_id, "test-tenant", "/expiring.txt")
        assert released is False

    @pytest.mark.asyncio
    async def test_lock_wrong_owner_cannot_release(self, lock_manager):
        """Test that wrong owner cannot release lock."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/owned.txt",
            timeout=5.0,
        )
        assert lock_id is not None

        try:
            # Try to release with wrong lock_id
            released = await lock_manager.release("wrong-lock-id", "test-tenant", "/owned.txt")
            assert released is False

            # Lock should still exist
            is_locked = await lock_manager.is_locked("test-tenant", "/owned.txt")
            assert is_locked is True

        finally:
            await lock_manager.release(lock_id, "test-tenant", "/owned.txt")

    @pytest.mark.asyncio
    async def test_lock_wrong_owner_cannot_extend(self, lock_manager):
        """Test that wrong owner cannot extend lock."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/extend.txt",
            timeout=5.0,
        )
        assert lock_id is not None

        try:
            # Try to extend with wrong lock_id
            extended = await lock_manager.extend("wrong-lock-id", "test-tenant", "/extend.txt")
            assert extended is False

        finally:
            await lock_manager.release(lock_id, "test-tenant", "/extend.txt")

    @pytest.mark.asyncio
    async def test_get_lock_info(self, lock_manager):
        """Test getting lock information."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/info.txt",
            timeout=5.0,
            ttl=30.0,
        )
        assert lock_id is not None

        try:
            info = await lock_manager.get_lock_info("test-tenant", "/info.txt")

            assert info is not None
            assert info["lock_id"] == lock_id
            assert info["tenant_id"] == "test-tenant"
            assert info["path"] == "/info.txt"
            assert 0 < info["ttl"] <= 30

        finally:
            await lock_manager.release(lock_id, "test-tenant", "/info.txt")

    @pytest.mark.asyncio
    async def test_get_lock_info_not_locked(self, lock_manager):
        """Test getting lock info when not locked."""
        info = await lock_manager.get_lock_info("test-tenant", "/not-locked.txt")
        assert info is None

    @pytest.mark.asyncio
    async def test_force_release(self, lock_manager):
        """Test administrative force release."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/force.txt",
            timeout=5.0,
        )
        assert lock_id is not None

        # Force release (admin operation)
        released = await lock_manager.force_release("test-tenant", "/force.txt")
        assert released is True

        # Lock should be gone
        is_locked = await lock_manager.is_locked("test-tenant", "/force.txt")
        assert is_locked is False

    @pytest.mark.asyncio
    async def test_health_check(self, lock_manager):
        """Test lock manager health check."""
        result = await lock_manager.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_multi_tenant_lock_isolation(self, lock_manager):
        """Test that locks are isolated per tenant."""
        # Lock same path for different tenants
        lock_a = await lock_manager.acquire(
            tenant_id="tenant-A",
            path="/shared.txt",
            timeout=5.0,
        )
        lock_b = await lock_manager.acquire(
            tenant_id="tenant-B",
            path="/shared.txt",
            timeout=5.0,
        )

        try:
            # Both should succeed (different tenants)
            assert lock_a is not None
            assert lock_b is not None
            assert lock_a != lock_b

        finally:
            await lock_manager.release(lock_a, "tenant-A", "/shared.txt")
            await lock_manager.release(lock_b, "tenant-B", "/shared.txt")


# =============================================================================
# Combined Event + Lock Workflow Tests
# =============================================================================


class TestDistributedWorkflows:
    """Integration tests for combined event and lock workflows."""

    @pytest.mark.asyncio
    async def test_lock_then_write_emits_event(self, redis_client, lock_manager):
        """Test workflow: acquire lock, write file, emit event."""
        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        bus = RedisEventBus(redis_client)
        await bus.start()

        try:
            # Acquire lock
            lock_id = await lock_manager.acquire(
                tenant_id="test-tenant",
                path="/workflow.txt",
                timeout=5.0,
            )
            assert lock_id is not None

            # Emit write event
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/workflow.txt",
                tenant_id="test-tenant",
            )
            num_subscribers = await bus.publish(event)
            assert num_subscribers >= 0

            # Release lock
            released = await lock_manager.release(lock_id, "test-tenant", "/workflow.txt")
            assert released is True

        finally:
            await bus.stop()

    @pytest.mark.asyncio
    async def test_meeting_floor_control_pattern(self, lock_manager):
        """Test meeting floor control pattern with heartbeat."""
        # Acquire "floor" lock
        lock_id = await lock_manager.acquire(
            tenant_id="meeting-123",
            path="/floor",
            timeout=2.0,
            ttl=5.0,
        )
        assert lock_id is not None

        try:
            # Simulate speaking with heartbeats
            for _ in range(3):
                await asyncio.sleep(0.3)
                extended = await lock_manager.extend(lock_id, "meeting-123", "/floor", ttl=5.0)
                assert extended is True

            # Still have the floor
            is_locked = await lock_manager.is_locked("meeting-123", "/floor")
            assert is_locked is True

        finally:
            # Release floor
            released = await lock_manager.release(lock_id, "meeting-123", "/floor")
            assert released is True

    @pytest.mark.asyncio
    async def test_concurrent_lock_contention(self, lock_manager):
        """Test multiple clients contending for same lock."""
        results = {"acquired": 0, "failed": 0}

        async def try_acquire(client_id: int):
            lock_id = await lock_manager.acquire(
                tenant_id="test-tenant",
                path="/contended.txt",
                timeout=0.5,
                ttl=2.0,
            )
            if lock_id:
                results["acquired"] += 1
                # Hold lock briefly
                await asyncio.sleep(0.1)
                await lock_manager.release(lock_id, "test-tenant", "/contended.txt")
            else:
                results["failed"] += 1

        # Run concurrent attempts
        await asyncio.gather(*[try_acquire(i) for i in range(5)])

        # At least one should acquire, others may fail or acquire after release
        assert results["acquired"] >= 1
        assert results["acquired"] + results["failed"] == 5


# =============================================================================
# Path Pattern Filtering Tests (Layer 1 Parity + Additional)
# =============================================================================


class TestPathPatternFiltering:
    """Comprehensive tests for path pattern matching in event filtering.

    These tests ensure parity with Layer 1 (local file watching) and cover
    additional distributed scenarios.
    """

    @pytest.mark.asyncio
    async def test_directory_pattern_matches_file_in_dir(self, redis_client):
        """Watch /inbox/ -> event at /inbox/test.txt -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"dir-match-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/inbox/test.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_directory_pattern_no_match_different_dir(self, redis_client):
        """Watch /inbox/ -> event at /other/test.txt -> no fire."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"dir-nomatch-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            # Publish to different directory
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/other/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is None  # Should timeout, not match

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_directory_pattern_matches_subfolder(self, redis_client):
        """Watch /inbox/ -> event at /inbox/subdir/test.txt -> fires (recursive)."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"subdir-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/subdir/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/inbox/subdir/test.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_directory_pattern_matches_deep_nested(self, redis_client):
        """Watch /root/ -> event at /root/a/b/c/d/deep.txt -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"deep-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/root/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/root/a/b/c/d/deep.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/root/a/b/c/d/deep.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_glob_star_matches_extension(self, redis_client):
        """Watch /inbox/*.txt -> event at /inbox/test.txt -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"glob-ext-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/*.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/inbox/test.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_glob_star_no_match_wrong_extension(self, redis_client):
        """Watch /inbox/*.txt -> event at /inbox/test.pdf -> no fire."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"glob-noext-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/*.txt",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.pdf",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is None

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_glob_double_star_cross_folder(self, redis_client):
        """Watch **/*.txt -> event at /a/b/c/test.txt -> fires (cross-folder)."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"glob-cross-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="**/*.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/a/b/c/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/a/b/c/test.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_exact_path_match(self, redis_client):
        """Watch exact /inbox/test.txt -> event at same path -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"exact-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/test.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.path == "/inbox/test.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_exact_path_no_match_different_file(self, redis_client):
        """Watch exact /inbox/test.txt -> event at /inbox/other.txt -> no fire."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"exact-no-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/test.txt",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/other.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is None

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_question_mark_glob(self, redis_client):
        """Watch /inbox/tes?.txt -> event at /inbox/test.txt -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"glob-q-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/inbox/tes?.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None

        finally:
            await publisher.stop()
            await subscriber.stop()


# =============================================================================
# Event Type Tests
# =============================================================================


class TestEventTypes:
    """Tests for different event types being properly received."""

    @pytest.mark.asyncio
    async def test_file_write_event(self, redis_client):
        """Watch -> FILE_WRITE event -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-write-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/files/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/files/test.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "file_write"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_file_delete_event(self, redis_client):
        """Watch -> FILE_DELETE event -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-delete-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/files/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_DELETE,
                path="/files/deleted.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "file_delete"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_file_rename_event_matches_new_path(self, redis_client):
        """Watch -> FILE_RENAME event -> fires (matches new path)."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-rename-new-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/dest/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_RENAME,
                path="/dest/new_name.txt",
                old_path="/source/old_name.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "file_rename"
            assert received.path == "/dest/new_name.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_file_rename_event_matches_old_path(self, redis_client):
        """Watch -> FILE_RENAME event -> fires (matches old path)."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-rename-old-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/source/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_RENAME,
                path="/dest/new_name.txt",
                old_path="/source/old_name.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.old_path == "/source/old_name.txt"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_dir_create_event(self, redis_client):
        """Watch -> DIR_CREATE event -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-dircreate-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/parent/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.DIR_CREATE,
                path="/parent/new_folder",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "dir_create"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_dir_delete_event(self, redis_client):
        """Watch -> DIR_DELETE event -> fires."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"evt-dirdelete-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/parent/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.DIR_DELETE,
                path="/parent/deleted_folder",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "dir_delete"

        finally:
            await publisher.stop()
            await subscriber.stop()


# =============================================================================
# Lock Corner Cases Tests
# =============================================================================


class TestLockCornerCases:
    """Tests for lock manager corner cases and edge conditions."""

    @pytest.mark.asyncio
    async def test_lock_zero_timeout_immediate_fail(self, lock_manager):
        """Lock with timeout=0 -> immediate fail if already locked."""
        # First lock
        lock_id1 = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/zero-timeout.txt",
            timeout=5.0,
        )
        assert lock_id1 is not None

        try:
            # Second lock with zero timeout should fail immediately
            lock_id2 = await lock_manager.acquire(
                tenant_id="test-tenant",
                path="/zero-timeout.txt",
                timeout=0.0,
            )
            assert lock_id2 is None

        finally:
            await lock_manager.release(lock_id1, "test-tenant", "/zero-timeout.txt")

    @pytest.mark.asyncio
    async def test_extend_almost_expired_lock(self, lock_manager):
        """Extend lock that's about to expire -> success."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/almost-expired.txt",
            timeout=5.0,
            ttl=1.0,  # 1 second TTL
        )
        assert lock_id is not None

        try:
            # Wait until almost expired
            await asyncio.sleep(0.8)

            # Extend should still work
            extended = await lock_manager.extend(
                lock_id, "test-tenant", "/almost-expired.txt", ttl=30.0
            )
            assert extended is True

            # Verify still locked
            is_locked = await lock_manager.is_locked("test-tenant", "/almost-expired.txt")
            assert is_locked is True

        finally:
            await lock_manager.release(lock_id, "test-tenant", "/almost-expired.txt")

    @pytest.mark.asyncio
    async def test_extend_expired_lock_fails(self, lock_manager):
        """Extend lock that has already expired -> failure."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/extend-expired.txt",
            timeout=5.0,
            ttl=0.3,  # Very short TTL
        )
        assert lock_id is not None

        # Wait for expiration
        await asyncio.sleep(0.5)

        # Extend should fail (lock expired)
        extended = await lock_manager.extend(
            lock_id, "test-tenant", "/extend-expired.txt", ttl=30.0
        )
        assert extended is False

    @pytest.mark.asyncio
    async def test_double_release(self, lock_manager):
        """Release lock twice -> second returns False."""
        lock_id = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/double-release.txt",
            timeout=5.0,
        )
        assert lock_id is not None

        # First release succeeds
        released1 = await lock_manager.release(lock_id, "test-tenant", "/double-release.txt")
        assert released1 is True

        # Second release fails
        released2 = await lock_manager.release(lock_id, "test-tenant", "/double-release.txt")
        assert released2 is False

    @pytest.mark.asyncio
    async def test_lock_after_ttl_expiry(self, lock_manager):
        """Lock expires by TTL -> new lock on same path succeeds."""
        lock_id1 = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/ttl-expiry.txt",
            timeout=5.0,
            ttl=0.3,
        )
        assert lock_id1 is not None

        # Wait for TTL expiry
        await asyncio.sleep(0.5)

        # New lock should succeed
        lock_id2 = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/ttl-expiry.txt",
            timeout=1.0,
        )
        assert lock_id2 is not None
        assert lock_id2 != lock_id1

        await lock_manager.release(lock_id2, "test-tenant", "/ttl-expiry.txt")

    @pytest.mark.asyncio
    async def test_lock_different_tenants_same_path(self, lock_manager):
        """Different tenants can lock same path independently."""
        lock_a = await lock_manager.acquire(
            tenant_id="tenant-A",
            path="/shared-path.txt",
            timeout=5.0,
        )
        lock_b = await lock_manager.acquire(
            tenant_id="tenant-B",
            path="/shared-path.txt",
            timeout=5.0,
        )

        try:
            # Both should succeed
            assert lock_a is not None
            assert lock_b is not None
            assert lock_a != lock_b

        finally:
            await lock_manager.release(lock_a, "tenant-A", "/shared-path.txt")
            await lock_manager.release(lock_b, "tenant-B", "/shared-path.txt")

    @pytest.mark.asyncio
    async def test_release_wrong_tenant(self, lock_manager):
        """Release lock with wrong tenant -> failure."""
        lock_id = await lock_manager.acquire(
            tenant_id="correct-tenant",
            path="/wrong-tenant.txt",
            timeout=5.0,
        )
        assert lock_id is not None

        try:
            # Try to release with correct lock_id but wrong tenant
            released = await lock_manager.release(lock_id, "wrong-tenant", "/wrong-tenant.txt")
            assert released is False

            # Original lock should still exist
            is_locked = await lock_manager.is_locked("correct-tenant", "/wrong-tenant.txt")
            assert is_locked is True

        finally:
            await lock_manager.release(lock_id, "correct-tenant", "/wrong-tenant.txt")

    @pytest.mark.asyncio
    async def test_acquire_waits_for_release(self, lock_manager):
        """Second acquire waits and succeeds when first releases."""
        results = {"second_acquired": False, "second_lock_id": None}

        lock_id1 = await lock_manager.acquire(
            tenant_id="test-tenant",
            path="/wait-release.txt",
            timeout=5.0,
            ttl=30.0,
        )
        assert lock_id1 is not None

        async def second_acquire():
            results["second_lock_id"] = await lock_manager.acquire(
                tenant_id="test-tenant",
                path="/wait-release.txt",
                timeout=3.0,
            )
            results["second_acquired"] = results["second_lock_id"] is not None

        # Start second acquire in background
        acquire_task = asyncio.create_task(second_acquire())

        # Wait a bit, then release first lock
        await asyncio.sleep(0.3)
        await lock_manager.release(lock_id1, "test-tenant", "/wait-release.txt")

        # Wait for second acquire to complete
        await acquire_task

        try:
            assert results["second_acquired"] is True
            assert results["second_lock_id"] is not None
        finally:
            if results["second_lock_id"]:
                await lock_manager.release(
                    results["second_lock_id"], "test-tenant", "/wait-release.txt"
                )


# =============================================================================
# Distributed-Specific Tests
# =============================================================================


class TestDistributedSpecific:
    """Tests specific to distributed event system scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_pattern(self, redis_client):
        """Multiple subscribers with same pattern -> all receive event."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"multi-sub-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber1 = RedisEventBus(redis_client)
        subscriber2 = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber1.start()
        await subscriber2.start()

        try:
            results = {"sub1": None, "sub2": None}

            async def wait_sub1():
                results["sub1"] = await subscriber1.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/shared/",
                    timeout=2.0,
                )

            async def wait_sub2():
                results["sub2"] = await subscriber2.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/shared/",
                    timeout=2.0,
                )

            task1 = asyncio.create_task(wait_sub1())
            task2 = asyncio.create_task(wait_sub2())
            await asyncio.sleep(0.3)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/shared/file.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            await task1
            await task2

            # Both subscribers should receive
            assert results["sub1"] is not None
            assert results["sub2"] is not None
            assert results["sub1"].path == "/shared/file.txt"
            assert results["sub2"].path == "/shared/file.txt"

        finally:
            await publisher.stop()
            await subscriber1.stop()
            await subscriber2.stop()

    @pytest.mark.asyncio
    async def test_event_metadata_preserved(self, redis_client):
        """Event metadata (size, etag, agent_id) preserved through pub/sub."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"metadata-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/meta/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/meta/file.txt",
                tenant_id=tenant_id,
                size=12345,
                etag="abc123hash",
                agent_id="agent-007",
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.size == 12345
            assert received.etag == "abc123hash"
            assert received.agent_id == "agent-007"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_subscriber_joins_after_publish_misses_event(self, redis_client):
        """Subscriber joins after event published -> doesn't receive old event."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"late-sub-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)

        await publisher.start()

        try:
            # Publish event BEFORE subscriber joins
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/late/file.txt",
                tenant_id=tenant_id,
            )
            await publisher.publish(event)

            # Now start subscriber
            subscriber = RedisEventBus(redis_client)
            await subscriber.start()

            try:
                # Should timeout (event already published)
                result = await subscriber.wait_for_event(
                    tenant_id=tenant_id,
                    path_pattern="/late/",
                    timeout=0.5,
                )
                assert result is None

            finally:
                await subscriber.stop()

        finally:
            await publisher.stop()

    @pytest.mark.asyncio
    async def test_rapid_event_sequence(self, redis_client):
        """Rapid sequence of events -> subscriber receives them in order."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"rapid-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            received_events = []

            async def collect_events():
                for _ in range(3):
                    event = await subscriber.wait_for_event(
                        tenant_id=tenant_id,
                        path_pattern="/rapid/",
                        timeout=2.0,
                    )
                    if event:
                        received_events.append(event.path)

            collect_task = asyncio.create_task(collect_events())
            await asyncio.sleep(0.2)

            # Publish 3 events rapidly
            for i in range(3):
                event = FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path=f"/rapid/file{i}.txt",
                    tenant_id=tenant_id,
                )
                await publisher.publish(event)
                await asyncio.sleep(0.05)  # Small delay to ensure ordering

            await collect_task

            # Should receive all 3 in order
            assert len(received_events) == 3
            assert received_events == [
                "/rapid/file0.txt",
                "/rapid/file1.txt",
                "/rapid/file2.txt",
            ]

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_event_id_uniqueness(self, redis_client):
        """Each event has unique event_id for deduplication."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"unique-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            event_ids = []

            async def collect_event_ids():
                for _ in range(2):
                    event = await subscriber.wait_for_event(
                        tenant_id=tenant_id,
                        path_pattern="/unique/",
                        timeout=2.0,
                    )
                    if event:
                        event_ids.append(event.event_id)

            collect_task = asyncio.create_task(collect_event_ids())
            await asyncio.sleep(0.2)

            # Publish 2 events to same path
            for _ in range(2):
                event = FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path="/unique/file.txt",
                    tenant_id=tenant_id,
                )
                await publisher.publish(event)
                await asyncio.sleep(0.1)

            await collect_task

            # Each event should have unique ID
            assert len(event_ids) == 2
            assert event_ids[0] != event_ids[1]
            # Verify they're valid UUIDs
            for eid in event_ids:
                assert len(eid) == 36

        finally:
            await publisher.stop()
            await subscriber.stop()


# =============================================================================
# Additional Edge Cases Tests
# =============================================================================


@pytest.mark.skipif(
    not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
    reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
)
class TestAdditionalEdgeCases:
    """Additional edge case tests for completeness."""

    @pytest.mark.asyncio
    async def test_wait_for_event_nonexistent_path_pattern(self, event_bus: RedisEventBus):
        """Test wait_for_event on path that will never receive events."""
        import uuid

        tenant_id = f"nonexistent-{uuid.uuid4().hex[:8]}"

        # Wait for event on a path where nothing happens
        event = await event_bus.wait_for_event(
            tenant_id=tenant_id,
            path_pattern="/nonexistent/path/that/will/never/match/",
            timeout=0.5,
        )

        # Should timeout and return None
        assert event is None

    @pytest.mark.asyncio
    async def test_lock_manager_is_locked_never_locked_path(self, lock_manager: RedisLockManager):
        """Test is_locked() on a path that was never locked."""
        import uuid

        tenant_id = f"never-locked-{uuid.uuid4().hex[:8]}"

        # Check a path that was never locked
        is_locked = await lock_manager.is_locked(
            tenant_id=tenant_id,
            path="/never/locked/path.txt",
        )

        assert is_locked is False

    @pytest.mark.asyncio
    async def test_lock_manager_get_lock_info_never_locked(self, lock_manager: RedisLockManager):
        """Test get_lock_info() on a path that was never locked."""
        import uuid

        tenant_id = f"info-never-locked-{uuid.uuid4().hex[:8]}"

        # Get info on never-locked path
        info = await lock_manager.get_lock_info(
            tenant_id=tenant_id,
            path="/never/locked/info.txt",
        )

        # Should return None or empty info
        assert info is None or info.get("locked") is False


# =============================================================================
# Stress and Performance Tests
# =============================================================================


@pytest.mark.skipif(
    not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
    reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
)
class TestStressAndPerformance:
    """Stress tests for high-volume scenarios."""

    @pytest.mark.asyncio
    async def test_rapid_event_publishing(self, event_bus: RedisEventBus):
        """Test publishing many events rapidly doesn't cause issues."""
        import uuid

        from nexus.core.event_bus import FileEvent, FileEventType

        tenant_id = f"stress-publish-{uuid.uuid4().hex[:8]}"

        # Publish 50 events rapidly
        publish_count = 50
        for i in range(publish_count):
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path=f"/stress/file_{i}.txt",
                tenant_id=tenant_id,
            )
            await event_bus.publish(event)

        # No assertions needed - just verify no exceptions

    @pytest.mark.asyncio
    async def test_rapid_lock_acquire_release(self, lock_manager: RedisLockManager):
        """Test rapid lock acquire/release cycles."""
        import uuid

        tenant_id = f"stress-lock-{uuid.uuid4().hex[:8]}"

        # 20 rapid lock/unlock cycles
        for i in range(20):
            path = f"/stress/lock_{i}.txt"
            lock_id = await lock_manager.acquire(
                tenant_id=tenant_id,
                path=path,
                timeout=1.0,
                ttl=5.0,
            )
            assert lock_id is not None

            released = await lock_manager.release(
                lock_id=lock_id,
                tenant_id=tenant_id,
                path=path,
            )
            assert released is True

    @pytest.mark.asyncio
    async def test_event_ordering_under_load(self, event_bus: RedisEventBus):
        """Test that events maintain ordering under load."""
        import uuid

        from nexus.core.cache.dragonfly import DragonflyClient
        from nexus.core.event_bus import FileEvent, FileEventType, RedisEventBus

        tenant_id = f"order-load-{uuid.uuid4().hex[:8]}"

        # Create second bus for subscribing
        redis_url = os.environ.get("NEXUS_REDIS_URL", "redis://localhost:6379")
        subscribe_client = DragonflyClient(url=redis_url)
        await subscribe_client.connect()
        subscriber = RedisEventBus(subscribe_client)
        await subscriber.start()

        try:
            received_events = []

            async def collect_events():
                for _ in range(10):
                    event = await subscriber.wait_for_event(
                        tenant_id=tenant_id,
                        path_pattern="/order/",
                        timeout=3.0,
                    )
                    if event:
                        received_events.append(event)

            collect_task = asyncio.create_task(collect_events())
            await asyncio.sleep(0.2)

            # Publish 10 events with sequence numbers in path
            for i in range(10):
                event = FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path=f"/order/file_{i:03d}.txt",
                    tenant_id=tenant_id,
                )
                await event_bus.publish(event)

            await collect_task

            # Verify we got at least some events (timing-sensitive, may not get all)
            assert len(received_events) >= 1

            # Verify ordering of received events by extracting sequence from path
            sequences = []
            for e in received_events:
                # Extract sequence number from path like "/order/file_001.txt"
                try:
                    seq = int(e.path.split("_")[-1].replace(".txt", ""))
                    sequences.append(seq)
                except (ValueError, IndexError):
                    pass

            for i in range(len(sequences) - 1):
                assert sequences[i] <= sequences[i + 1], f"Events out of order: {sequences}"

        finally:
            await subscriber.stop()
            await subscribe_client.disconnect()

    @pytest.mark.asyncio
    async def test_concurrent_locks_different_paths(self, lock_manager: RedisLockManager):
        """Test acquiring locks on many different paths concurrently."""
        import uuid

        tenant_id = f"concurrent-locks-{uuid.uuid4().hex[:8]}"

        async def acquire_and_release(path: str):
            lock_id = await lock_manager.acquire(
                tenant_id=tenant_id,
                path=path,
                timeout=5.0,
                ttl=10.0,
            )
            if lock_id:
                await asyncio.sleep(0.1)  # Hold briefly
                await lock_manager.release(
                    lock_id=lock_id,
                    tenant_id=tenant_id,
                    path=path,
                )
                return True
            return False

        # Acquire 20 locks concurrently on different paths
        tasks = [acquire_and_release(f"/concurrent/{i}.txt") for i in range(20)]
        results = await asyncio.gather(*tasks)

        # All should succeed (different paths)
        assert all(results)


# =============================================================================
# Error Recovery Tests (Layer 2)
# =============================================================================


@pytest.mark.skipif(
    not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
    reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
)
class TestErrorRecoveryLayer2:
    """Error recovery tests for Layer 2 distributed system."""

    @pytest.mark.asyncio
    async def test_publish_to_stopped_bus_raises(self, event_bus: RedisEventBus):
        """Test that publishing to stopped bus raises appropriate error."""
        from nexus.core.event_bus import FileEvent, FileEventType

        # Stop the bus first
        await event_bus.stop()

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            tenant_id="test-tenant",
        )

        # Should raise RuntimeError
        with pytest.raises(RuntimeError):
            await event_bus.publish(event)

        # Restart for cleanup
        await event_bus.start()

    @pytest.mark.asyncio
    async def test_wait_for_event_on_stopped_bus_raises(self, event_bus: RedisEventBus):
        """Test that wait_for_event on stopped bus raises appropriate error."""
        # Stop the bus first
        await event_bus.stop()

        # Should raise RuntimeError
        with pytest.raises(RuntimeError):
            await event_bus.wait_for_event(
                tenant_id="test-tenant",
                path_pattern="/test/",
                timeout=1.0,
            )

        # Restart for cleanup
        await event_bus.start()

    @pytest.mark.asyncio
    async def test_health_check_returns_status(self, event_bus: RedisEventBus):
        """Test health_check returns proper status."""
        # Should return True when Redis is available
        is_healthy = await event_bus.health_check()
        assert is_healthy is True

    @pytest.mark.asyncio
    async def test_bus_can_restart_after_stop(self, event_bus: RedisEventBus):
        """Test that bus can restart after being stopped."""
        from nexus.core.event_bus import FileEvent, FileEventType

        # Stop the bus
        await event_bus.stop()

        # Restart the bus
        await event_bus.start()

        # Should be able to publish again
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/restart.txt",
            tenant_id="test-tenant",
        )
        # Should not raise
        await event_bus.publish(event)
