"""Integration tests for distributed event system (RedisEventBus).

These tests verify the Redis Pub/Sub integration for distributed file events.

Requirements:
- Redis or Dragonfly running at localhost:6379 (or NEXUS_REDIS_URL)
- Use pytest marker `@pytest.mark.redis` to skip if Redis unavailable

Related: Issue #1106 Block 2
"""

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from nexus.services.event_bus.redis import RedisEventBus

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
    from nexus.cache.dragonfly import DragonflyClient

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
    from nexus.services.event_bus.redis import RedisEventBus

    bus = RedisEventBus(redis_client)
    await bus.start()

    yield bus

    await bus.stop()


# =============================================================================
# Event Bus Integration Tests
# =============================================================================


class TestRedisEventBusIntegration:
    """Integration tests for RedisEventBus with real Redis."""

    @pytest.mark.asyncio
    async def test_publish_event(self, event_bus):
        """Test publishing an event to Redis."""
        from nexus.services.event_bus.types import FileEvent, FileEventType

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="test-zone",
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
            zone_id="test-zone",
            path_pattern="/nonexistent/",
            timeout=0.5,
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_publish_and_receive_event(self, redis_client):
        """Test publishing and receiving an event."""
        import uuid

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Use unique zone to avoid cross-test contamination in parallel runs
        zone_id = f"pubsub-test-{uuid.uuid4().hex[:8]}"

        # Create two separate bus instances (simulating different nodes)
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                zone_id=zone_id,
                size=1024,
            )

            # Start waiting in background
            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
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
            assert received.zone_id == zone_id
            assert received.size == 1024

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_event_filtering_by_path(self, redis_client):
        """Test that events are filtered by path pattern."""
        import uuid

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Use unique zone to avoid cross-test contamination in parallel runs
        zone_id = f"filter-test-{uuid.uuid4().hex[:8]}"

        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            # Subscriber watches /inbox/
            async def wait_for_inbox_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_inbox_event())
            await asyncio.sleep(0.2)

            # Publish event to /other/ (should not match)
            other_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/other/file.txt",
                zone_id=zone_id,
            )
            await publisher.publish(other_event)

            # Publish event to /inbox/ (should match)
            inbox_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/file.txt",
                zone_id=zone_id,
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
    async def test_multi_zone_isolation(self, redis_client):
        """Test that events are isolated per zone."""
        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            # Subscriber watches zone-A
            async def wait_for_zone_a_event():
                return await subscriber.wait_for_event(
                    zone_id="zone-A",
                    path_pattern="/inbox/",
                    timeout=1.0,
                )

            wait_task = asyncio.create_task(wait_for_zone_a_event())
            await asyncio.sleep(0.2)

            # Publish event to zone-B (should not be received)
            zone_b_event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/file.txt",
                zone_id="zone-B",
            )
            await publisher.publish(zone_b_event)

            # Wait should timeout (no matching zone)
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"dir-match-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"dir-nomatch-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            # Publish to different directory
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/other/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"subdir-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/subdir/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"deep-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/root/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/root/a/b/c/d/deep.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"glob-ext-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/*.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"glob-noext-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/*.txt",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.pdf",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"glob-cross-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="**/*.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/a/b/c/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"exact-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/test.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"exact-no-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/test.txt",
                    timeout=0.5,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/other.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"glob-q-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/inbox/tes?.txt",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-write-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/files/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/files/test.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-delete-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/files/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_DELETE,
                path="/files/deleted.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-rename-new-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/dest/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_RENAME,
                path="/dest/new_name.txt",
                old_path="/source/old_name.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-rename-old-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/source/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_RENAME,
                path="/dest/new_name.txt",
                old_path="/source/old_name.txt",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-dircreate-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/parent/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.DIR_CREATE,
                path="/parent/new_folder",
                zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"evt-dirdelete-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/parent/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.DIR_DELETE,
                path="/parent/deleted_folder",
                zone_id=zone_id,
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.type == "dir_delete"

        finally:
            await publisher.stop()
            await subscriber.stop()


# =============================================================================
# Distributed-Specific Tests
# =============================================================================


class TestDistributedSpecific:
    """Tests specific to distributed event system scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_subscribers_same_pattern(self, redis_client):
        """Multiple subscribers with same pattern -> all receive event."""
        import uuid

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"multi-sub-{uuid.uuid4().hex[:8]}"
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
                    zone_id=zone_id,
                    path_pattern="/shared/",
                    timeout=2.0,
                )

            async def wait_sub2():
                results["sub2"] = await subscriber2.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/shared/",
                    timeout=2.0,
                )

            task1 = asyncio.create_task(wait_sub1())
            task2 = asyncio.create_task(wait_sub2())
            await asyncio.sleep(0.3)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/shared/file.txt",
                zone_id=zone_id,
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
        """Event metadata (size, content_id, agent_id) preserved through pub/sub."""
        import uuid

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"metadata-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:

            async def wait_for_event():
                return await subscriber.wait_for_event(
                    zone_id=zone_id,
                    path_pattern="/meta/",
                    timeout=2.0,
                )

            wait_task = asyncio.create_task(wait_for_event())
            await asyncio.sleep(0.2)

            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/meta/file.txt",
                zone_id=zone_id,
                size=12345,
                content_id="abc123hash",
                agent_id="agent-007",
            )
            await publisher.publish(event)

            received = await wait_task
            assert received is not None
            assert received.size == 12345
            assert received.content_id == "abc123hash"
            assert received.agent_id == "agent-007"

        finally:
            await publisher.stop()
            await subscriber.stop()

    @pytest.mark.asyncio
    async def test_subscriber_joins_after_publish_misses_event(self, redis_client):
        """Subscriber joins after event published -> doesn't receive old event."""
        import uuid

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"late-sub-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)

        await publisher.start()

        try:
            # Publish event BEFORE subscriber joins
            event = FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/late/file.txt",
                zone_id=zone_id,
            )
            await publisher.publish(event)

            # Now start subscriber
            subscriber = RedisEventBus(redis_client)
            await subscriber.start()

            try:
                # Should timeout (event already published)
                result = await subscriber.wait_for_event(
                    zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"rapid-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            received_events = []

            async def collect_events():
                for _ in range(3):
                    event = await subscriber.wait_for_event(
                        zone_id=zone_id,
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
                    zone_id=zone_id,
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

        from nexus.services.event_bus.redis import RedisEventBus
        from nexus.services.event_bus.types import FileEvent, FileEventType

        zone_id = f"unique-{uuid.uuid4().hex[:8]}"
        publisher = RedisEventBus(redis_client)
        subscriber = RedisEventBus(redis_client)

        await publisher.start()
        await subscriber.start()

        try:
            event_ids = []

            async def collect_event_ids():
                for _ in range(2):
                    event = await subscriber.wait_for_event(
                        zone_id=zone_id,
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
                    zone_id=zone_id,
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
# Error Recovery Tests (Layer 2)
# =============================================================================


@pytest.mark.skipif(
    not os.environ.get("NEXUS_REDIS_URL") and not os.environ.get("REDIS_ENABLED"),
    reason="Redis not available (set NEXUS_REDIS_URL or REDIS_ENABLED=1)",
)
class TestErrorRecoveryLayer2:
    """Error recovery tests for Layer 2 distributed system."""

    @pytest.mark.asyncio
    async def test_publish_to_stopped_bus_raises(self, event_bus: "RedisEventBus"):
        """Test that publishing to stopped bus raises appropriate error."""
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Stop the bus first
        await event_bus.stop()

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="test-zone",
        )

        # Should raise RuntimeError
        with pytest.raises(RuntimeError):
            await event_bus.publish(event)

        # Restart for cleanup
        await event_bus.start()

    @pytest.mark.asyncio
    async def test_wait_for_event_on_stopped_bus_raises(self, event_bus: "RedisEventBus"):
        """Test that wait_for_event on stopped bus raises appropriate error."""
        # Stop the bus first
        await event_bus.stop()

        # Should raise RuntimeError
        with pytest.raises(RuntimeError):
            await event_bus.wait_for_event(
                zone_id="test-zone",
                path_pattern="/test/",
                timeout=1.0,
            )

        # Restart for cleanup
        await event_bus.start()

    @pytest.mark.asyncio
    async def test_health_check_returns_status(self, event_bus: "RedisEventBus"):
        """Test health_check returns proper status."""
        # Should return True when Redis is available
        is_healthy = await event_bus.health_check()
        assert is_healthy is True

    @pytest.mark.asyncio
    async def test_bus_can_restart_after_stop(self, event_bus: "RedisEventBus"):
        """Test that bus can restart after being stopped."""
        from nexus.services.event_bus.types import FileEvent, FileEventType

        # Stop the bus
        await event_bus.stop()

        # Restart the bus
        await event_bus.start()

        # Should be able to publish again
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/restart.txt",
            zone_id="test-zone",
        )
        # Should not raise
        await event_bus.publish(event)
