"""Comprehensive lifecycle edge case tests for EventBus implementations."""

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
def sample_event():
    """Create a sample FileEvent."""
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/test.txt",
        zone_id=ROOT_ZONE_ID,
    )


# Parametrized test matrix: method × backend × state
@pytest.mark.parametrize(
    "method,args_factory",
    [
        ("publish", lambda e: (e,)),
        ("wait_for_event", lambda e: ("root", "/test.txt")),
        ("subscribe", lambda e: ("root",)),
        ("health_check", lambda e: ()),
    ],
)
class TestMethodsBeforeStart:
    """Test all methods raise RuntimeError if called before start()."""

    @pytest.mark.asyncio
    async def test_method_before_start_raises(
        self, mock_redis_client, sample_event, method, args_factory
    ):
        """Method raises RuntimeError before start()."""
        bus = RedisEventBus(mock_redis_client)
        # Don't call start()

        args = args_factory(sample_event)

        if method == "subscribe":
            # Subscribe is an async generator
            with pytest.raises(RuntimeError, match="not started"):
                async for _ in getattr(bus, method)(*args):
                    pass
        elif method == "health_check":
            # health_check should return False, not raise
            result = await getattr(bus, method)(*args)
            assert result is False
        else:
            # Other methods should raise
            with pytest.raises(RuntimeError, match="not started"):
                await getattr(bus, method)(*args)


@pytest.mark.parametrize(
    "method,args_factory",
    [
        ("publish", lambda e: (e,)),
        ("wait_for_event", lambda e: ("root", "/test.txt", 0.1)),  # Short timeout
    ],
)
class TestMethodsAfterStop:
    """Test methods raise RuntimeError after stop()."""

    @pytest.mark.asyncio
    async def test_method_after_stop_raises(
        self, mock_redis_client, sample_event, method, args_factory
    ):
        """Method raises RuntimeError after stop()."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()
        await bus.stop()

        args = args_factory(sample_event)

        with pytest.raises(RuntimeError, match="not started"):
            await getattr(bus, method)(*args)


class TestStartStopIdempotency:
    """Test start() and stop() idempotency."""

    @pytest.mark.asyncio
    async def test_start_twice_is_safe(self, mock_redis_client):
        """Calling start() multiple times is idempotent."""
        bus = RedisEventBus(mock_redis_client)

        await bus.start()
        await bus.start()
        await bus.start()

        # Should still work
        assert bus._started is True

        await bus.stop()

    @pytest.mark.asyncio
    async def test_stop_twice_is_safe(self, mock_redis_client):
        """Calling stop() multiple times is idempotent."""
        bus = RedisEventBus(mock_redis_client)

        await bus.start()
        await bus.stop()
        await bus.stop()
        await bus.stop()

        # Should be stopped
        assert bus._started is False

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self, mock_redis_client):
        """Calling stop() before start() is safe."""
        bus = RedisEventBus(mock_redis_client)

        # Should not raise
        await bus.stop()

        assert bus._started is False


class TestConcurrentStartStop:
    """Test concurrent start/stop calls."""

    @pytest.mark.asyncio
    async def test_concurrent_starts(self, mock_redis_client):
        """Concurrent start() calls are handled safely."""
        import asyncio

        bus = RedisEventBus(mock_redis_client)

        # Start multiple times concurrently
        await asyncio.gather(
            bus.start(),
            bus.start(),
            bus.start(),
        )

        assert bus._started is True

        await bus.stop()

    @pytest.mark.asyncio
    async def test_concurrent_stops(self, mock_redis_client):
        """Concurrent stop() calls are handled safely."""
        import asyncio

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        # Stop multiple times concurrently
        await asyncio.gather(
            bus.stop(),
            bus.stop(),
            bus.stop(),
        )

        assert bus._started is False

    @pytest.mark.asyncio
    async def test_interleaved_start_stop(self, mock_redis_client):
        """Rapid start/stop cycles work correctly."""
        bus = RedisEventBus(mock_redis_client)

        for _ in range(5):
            await bus.start()
            assert bus._started is True

            await bus.stop()
            assert bus._started is False


class TestStateTransitions:
    """Test valid state transitions."""

    @pytest.mark.asyncio
    async def test_lifecycle_sequence(self, mock_redis_client, sample_event):
        """Test full lifecycle: create → start → use → stop."""
        bus = RedisEventBus(mock_redis_client)

        # State: Created (not started)
        assert bus._started is False

        # Transition: start
        await bus.start()
        assert bus._started is True

        # State: Started (can use)
        await bus.publish(sample_event)  # Should work

        # Transition: stop
        await bus.stop()
        assert bus._started is False

        # State: Stopped (cannot use)
        with pytest.raises(RuntimeError, match="not started"):
            await bus.publish(sample_event)

    @pytest.mark.asyncio
    async def test_restart_after_stop(self, mock_redis_client, sample_event):
        """Can restart after stopping."""
        bus = RedisEventBus(mock_redis_client)

        # First lifecycle
        await bus.start()
        await bus.publish(sample_event)
        await bus.stop()

        # Restart
        await bus.start()
        await bus.publish(sample_event)  # Should work again
        await bus.stop()


class TestHealthCheck:
    """Test health_check behavior in different states."""

    @pytest.mark.asyncio
    async def test_health_check_before_start(self, mock_redis_client):
        """Health check returns False before start."""
        bus = RedisEventBus(mock_redis_client)

        result = await bus.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_after_start(self, mock_redis_client):
        """Health check returns True after start (if backend healthy)."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.health_check()
        assert result is True

        await bus.stop()

    @pytest.mark.asyncio
    async def test_health_check_after_stop(self, mock_redis_client):
        """Health check returns False after stop."""
        bus = RedisEventBus(mock_redis_client)
        await bus.start()
        await bus.stop()

        result = await bus.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_with_backend_failure(self, mock_redis_client):
        """Health check returns False if backend fails."""
        mock_redis_client.health_check = AsyncMock(return_value=False)

        bus = RedisEventBus(mock_redis_client)
        await bus.start()

        result = await bus.health_check()
        assert result is False

        await bus.stop()


class TestNodeIdGeneration:
    """Test node ID generation and persistence."""

    @pytest.mark.asyncio
    async def test_node_id_auto_generated(self, mock_redis_client):
        """Node ID is auto-generated if not provided."""
        bus = RedisEventBus(mock_redis_client)

        assert bus._node_id is not None
        assert isinstance(bus._node_id, str)
        assert len(bus._node_id) > 0

    @pytest.mark.asyncio
    async def test_node_id_custom_provided(self, mock_redis_client):
        """Custom node ID is used if provided."""
        bus = RedisEventBus(mock_redis_client, node_id="test-node-123")

        assert bus._node_id == "test-node-123"

    @pytest.mark.asyncio
    async def test_node_id_persists_across_lifecycle(self, mock_redis_client):
        """Node ID remains constant across start/stop cycles."""
        bus = RedisEventBus(mock_redis_client)
        original_node_id = bus._node_id

        await bus.start()
        assert bus._node_id == original_node_id

        await bus.stop()
        assert bus._node_id == original_node_id

        await bus.start()
        assert bus._node_id == original_node_id

        await bus.stop()
