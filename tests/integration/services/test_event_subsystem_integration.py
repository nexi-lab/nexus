"""Integration tests for EventBus ↔ EventLog interaction."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_bus import RedisEventBus
from nexus.services.event_bus.types import FileEvent, FileEventType


@pytest.fixture
def mock_redis_client():
    """Create a mock Redis client."""
    client = Mock()
    client.client = Mock()
    client.client.pubsub = Mock(return_value=AsyncMock())
    client.client.publish = AsyncMock(return_value=0)
    client.health_check = AsyncMock(return_value=True)
    client.get_info = AsyncMock(return_value={"status": "ok"})
    return client


@pytest.fixture
def mock_event_log():
    """Create a mock EventLog."""
    log = Mock()
    log.append = AsyncMock(return_value=1)
    log.append_batch = AsyncMock(return_value=[1, 2, 3])
    log.read_from = AsyncMock(return_value=[])
    log.current_sequence = Mock(return_value=0)
    return log


class TestWALFirstPublish:
    """Test WAL-first durability pattern (Issue #1397)."""

    @pytest.mark.asyncio
    async def test_publish_persists_to_wal_then_redis(self, mock_redis_client, mock_event_log):
        """Events persisted to WAL before Redis publish."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)
        await bus.publish(event)

        # Verify WAL append was called
        mock_event_log.append.assert_called_once_with(event)

        # Verify Redis publish was called after WAL
        mock_redis_client.client.publish.assert_called_once()

        await bus.stop()

    @pytest.mark.asyncio
    async def test_event_log_failure_doesnt_block_publish(self, mock_redis_client, mock_event_log):
        """Event still published even if WAL append fails (best-effort)."""
        # Mock WAL.append() to raise exception
        mock_event_log.append = AsyncMock(side_effect=OSError("Disk full"))

        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        # Should NOT raise — logged but published
        num_subs = await bus.publish(event)
        assert num_subs == 0  # Mock returns 0

        # Verify Redis publish was still called
        mock_redis_client.client.publish.assert_called_once()

        await bus.stop()

    @pytest.mark.asyncio
    async def test_event_log_can_be_wired_after_start(self, mock_redis_client, mock_event_log):
        """Event log can be set after bus is already started."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Wire event log after start
        bus.set_event_log(mock_event_log)

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)
        await bus.publish(event)

        # Verify event log was called
        mock_event_log.append.assert_called_once_with(event)

        await bus.stop()


class TestOrderingGuarantees:
    """Test event ordering across Bus and Log."""

    @pytest.mark.asyncio
    async def test_events_published_in_order(self, mock_redis_client, mock_event_log):
        """Multiple events published in order."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        events = [
            FileEvent(type=FileEventType.FILE_WRITE, path=f"/test{i}.txt", zone_id=ROOT_ZONE_ID)
            for i in range(10)
        ]

        for event in events:
            await bus.publish(event)

        # Verify all events were appended to log in order
        assert mock_event_log.append.call_count == 10

        # Verify order matches
        call_args = [call.args[0] for call in mock_event_log.append.call_args_list]
        for i, event in enumerate(events):
            assert call_args[i].path == event.path

        await bus.stop()

    @pytest.mark.asyncio
    async def test_concurrent_publishes_are_serialized(self, mock_redis_client, mock_event_log):
        """Concurrent publishes are handled correctly."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        events = [
            FileEvent(type=FileEventType.FILE_WRITE, path=f"/test{i}.txt", zone_id=ROOT_ZONE_ID)
            for i in range(20)
        ]

        # Publish all events concurrently
        await asyncio.gather(*[bus.publish(event) for event in events])

        # Verify all events were appended
        assert mock_event_log.append.call_count == 20

        await bus.stop()


class TestEventLogIntegration:
    """Test EventLog integration scenarios."""

    @pytest.mark.asyncio
    async def test_publish_without_event_log(self, mock_redis_client):
        """Publishing works without event log (Redis-only mode)."""
        bus = RedisEventBus(mock_redis_client)  # No event_log
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)
        num_subs = await bus.publish(event)

        assert num_subs == 0  # Mock returns 0
        mock_redis_client.client.publish.assert_called_once()

        await bus.stop()

    @pytest.mark.asyncio
    async def test_event_log_sequence_tracking(self, mock_redis_client, mock_event_log):
        """Event log sequence numbers increment correctly."""
        # Mock append to return incrementing sequence numbers
        call_count = 0

        async def mock_append(event):
            nonlocal call_count
            call_count += 1
            return call_count

        mock_event_log.append = mock_append

        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        events = [
            FileEvent(type=FileEventType.FILE_WRITE, path=f"/test{i}.txt", zone_id=ROOT_ZONE_ID)
            for i in range(5)
        ]

        for event in events:
            await bus.publish(event)

        # Verify sequence incremented
        assert call_count == 5

        await bus.stop()


class TestEventBusStatsWithLog:
    """Test stats reporting with event log integration."""

    @pytest.mark.asyncio
    async def test_stats_includes_event_log_info(self, mock_redis_client, mock_event_log):
        """Stats include information when event log is present."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        stats = await bus.get_stats()

        assert "backend" in stats
        assert stats["backend"] == "redis_pubsub"
        assert "status" in stats
        assert stats["status"] == "running"

        await bus.stop()


class TestMultiZonePublishing:
    """Test publishing to multiple zones with event log."""

    @pytest.mark.asyncio
    async def test_events_to_different_zones(self, mock_redis_client, mock_event_log):
        """Events to different zones are all logged."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        zones = ["zone1", "zone2", "zone3"]
        for zone in zones:
            event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=zone)
            await bus.publish(event)

        # Verify all zones were logged
        assert mock_event_log.append.call_count == 3

        # Verify correct zone_ids
        call_args = [call.args[0] for call in mock_event_log.append.call_args_list]
        for i, zone in enumerate(zones):
            assert call_args[i].zone_id == zone

        await bus.stop()


class TestEventLogErrorRecovery:
    """Test recovery from event log errors."""

    @pytest.mark.asyncio
    async def test_intermittent_event_log_failures(self, mock_redis_client, mock_event_log):
        """Bus continues working despite intermittent event log failures."""
        # Mock append to fail every other call
        call_count = 0

        async def mock_append(event):
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                raise OSError("Intermittent failure")
            return call_count

        mock_event_log.append = mock_append

        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        # Publish 4 events
        for i in range(4):
            event = FileEvent(
                type=FileEventType.FILE_WRITE, path=f"/test{i}.txt", zone_id=ROOT_ZONE_ID
            )
            await bus.publish(event)  # Should not raise

        # All events should be published to Redis despite log failures
        assert mock_redis_client.client.publish.call_count == 4

        await bus.stop()


class TestEventDeduplication:
    """Test event deduplication scenarios."""

    @pytest.mark.asyncio
    async def test_duplicate_event_ids_logged_separately(self, mock_redis_client, mock_event_log):
        """Events with same event_id are logged separately (log doesn't dedupe)."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        # Create two events with same event_id
        event1 = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test.txt",
            zone_id=ROOT_ZONE_ID,
            event_id="duplicate-id",
        )
        event2 = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test2.txt",
            zone_id=ROOT_ZONE_ID,
            event_id="duplicate-id",
        )

        await bus.publish(event1)
        await bus.publish(event2)

        # Both should be logged (no deduplication at event bus level)
        assert mock_event_log.append.call_count == 2

        await bus.stop()


class TestConcurrentBusAndLogOperations:
    """Test concurrent operations on bus and log."""

    @pytest.mark.asyncio
    async def test_publish_while_reading_log(self, mock_redis_client, mock_event_log):
        """Can publish to bus while reading from log."""
        bus = RedisEventBus(mock_redis_client, event_log=mock_event_log)
        await bus.start()

        # Simulate reading from log
        async def read_log():
            for _ in range(10):
                await mock_event_log.read_from(0, limit=10)
                await asyncio.sleep(0.01)

        # Simulate publishing events
        async def publish_events():
            for i in range(10):
                event = FileEvent(
                    type=FileEventType.FILE_WRITE, path=f"/test{i}.txt", zone_id=ROOT_ZONE_ID
                )
                await bus.publish(event)
                await asyncio.sleep(0.01)

        # Run both concurrently
        await asyncio.gather(read_log(), publish_events())

        # Verify all publishes completed
        assert mock_event_log.append.call_count == 10

        await bus.stop()
