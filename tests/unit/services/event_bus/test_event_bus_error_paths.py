"""Error path tests for EventBus implementations."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

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
def sample_event():
    """Create a sample FileEvent for testing."""
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/test.txt",
        zone_id=ROOT_ZONE_ID,
    )


class TestSerializationErrors:
    """Test event serialization failure handling."""

    @pytest.mark.asyncio
    async def test_redis_publish_serialization_error(self, mock_redis_client):
        """ValueError raised for non-serializable event fields."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Create event and mock to_json to raise error
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with (
            patch.object(FileEvent, "to_json", side_effect=TypeError("circular reference")),
            pytest.raises(ValueError, match="Event serialization failed"),
        ):
            await bus.publish(event)

        await bus.stop()

    @pytest.mark.asyncio
    async def test_redis_publish_invalid_json(self, mock_redis_client):
        """ValueError raised for JSON encoding errors."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with (
            patch.object(FileEvent, "to_json", side_effect=ValueError("NaN not supported")),
            pytest.raises(ValueError, match="Event serialization failed"),
        ):
            await bus.publish(event)

        await bus.stop()


class TestConnectionErrors:
    """Test backend connection failure handling."""

    @pytest.mark.asyncio
    async def test_redis_connection_lost_during_publish(self, mock_redis_client):
        """Exception propagates with context when Redis disconnects."""
        mock_redis_client.client.publish = AsyncMock(
            side_effect=ConnectionError("Redis connection lost")
        )

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with pytest.raises(ConnectionError, match="Redis connection lost"):
            await bus.publish(event)

        await bus.stop()

    @pytest.mark.asyncio
    async def test_redis_publish_timeout(self, mock_redis_client):
        """Timeout error propagates correctly."""
        mock_redis_client.client.publish = AsyncMock(
            side_effect=TimeoutError("Operation timed out")
        )

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with pytest.raises(asyncio.TimeoutError):
            await bus.publish(event)

        await bus.stop()

    @pytest.mark.asyncio
    async def test_redis_health_check_failure(self, mock_redis_client):
        """Health check returns False on connection failure."""
        mock_redis_client.health_check = AsyncMock(side_effect=Exception("Connection failed"))

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Should return False, not raise
        result = await bus.health_check()
        assert result is False

        await bus.stop()


class TestInvalidMessages:
    """Test corrupted message handling."""

    @pytest.mark.asyncio
    async def test_wait_for_event_invalid_json(self, mock_redis_client):
        """Gracefully handles corrupted JSON messages."""
        # Mock pubsub to return invalid JSON
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.aclose = AsyncMock()

        # First call returns invalid JSON, rest return None (timeout)
        call_count = 0

        async def mock_get_message(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"type": "message", "data": b"invalid json {"}
            return None

        mock_pubsub.get_message = mock_get_message

        mock_redis_client.client.pubsub = Mock(return_value=mock_pubsub)

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Should timeout gracefully, not crash
        result = await bus.wait_for_event("root", "/test.txt", timeout=0.1)
        assert result is None

        await bus.stop()

    @pytest.mark.asyncio
    async def test_subscribe_handles_malformed_events(self, mock_redis_client):
        """Subscription continues after encountering malformed events."""
        mock_pubsub = AsyncMock()
        mock_pubsub.subscribe = AsyncMock()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.aclose = AsyncMock()

        # Mix of invalid and valid messages
        valid_event = FileEvent(
            type=FileEventType.FILE_WRITE, path="/valid.txt", zone_id=ROOT_ZONE_ID
        )

        mock_pubsub.get_message = AsyncMock(
            side_effect=[
                {"type": "message", "data": b"invalid"},  # Malformed
                {"type": "message", "data": valid_event.to_json()},  # Valid
                asyncio.CancelledError(),  # Stop iteration
            ]
        )

        mock_redis_client.client.pubsub = Mock(return_value=mock_pubsub)

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Should skip invalid and yield valid
        events = []
        try:
            async for event in bus.subscribe("root"):
                events.append(event)
        except asyncio.CancelledError:
            pass

        assert len(events) == 1
        assert events[0].path == "/valid.txt"

        await bus.stop()


class TestLifecycleErrors:
    """Test lifecycle edge cases and errors."""

    @pytest.mark.asyncio
    async def test_publish_before_start_raises_error(self, mock_redis_client):
        """Publishing before start() raises RuntimeError."""
        bus = RedisEventBus(mock_redis_client)
        # Don't call start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with pytest.raises(RuntimeError, match="not started"):
            await bus.publish(event)

    @pytest.mark.asyncio
    async def test_subscribe_before_start_raises_error(self, mock_redis_client):
        """Subscribing before start() raises RuntimeError."""
        bus = RedisEventBus(mock_redis_client)
        # Don't call start()

        with pytest.raises(RuntimeError, match="not started"):
            async for _ in bus.subscribe("root"):
                pass

    @pytest.mark.asyncio
    async def test_wait_for_event_before_start_raises_error(self, mock_redis_client):
        """wait_for_event before start() raises RuntimeError."""
        bus = RedisEventBus(mock_redis_client)
        # Don't call start()

        with pytest.raises(RuntimeError, match="not started"):
            await bus.wait_for_event("root", "/test.txt")

    @pytest.mark.asyncio
    async def test_double_start_is_idempotent(self, mock_redis_client):
        """Calling start() twice is safe (idempotent)."""
        bus = RedisEventBus(mock_redis_client)

        await bus.start()
        await bus.start()  # Should not raise or cause issues

        # Verify we can still publish
        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)
        await bus.publish(event)

        await bus.stop()

    @pytest.mark.asyncio
    async def test_double_stop_is_idempotent(self, mock_redis_client):
        """Calling stop() twice is safe (idempotent)."""
        bus = RedisEventBus(mock_redis_client)

        await bus.start()
        await bus.stop()
        await bus.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_operations_after_stop_raise_error(self, mock_redis_client):
        """Operations after stop() raise RuntimeError."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()
        await bus.stop()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=ROOT_ZONE_ID)

        with pytest.raises(RuntimeError, match="not started"):
            await bus.publish(event)
