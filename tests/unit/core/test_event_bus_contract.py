"""Parametrized contract tests for event bus backends.

Ensures that both Redis and NATS backends satisfy the same EventBusBase
contract. Each test is run once per backend.

Related: Issue #1331
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.event_bus import (
    EventBusBase,
    EventBusProtocol,
    FileEvent,
    FileEventType,
    RedisEventBus,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_redis_client():
    """Create a mock DragonflyClient for Redis backend."""
    client = MagicMock()
    client.client = MagicMock()
    client.health_check = AsyncMock(return_value=True)
    client.get_info = AsyncMock(return_value={"status": "ok"})

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.aclose = AsyncMock()
    pubsub.get_message = AsyncMock(return_value=None)
    client.client.pubsub.return_value = pubsub
    client.client.publish = AsyncMock(return_value=1)

    return client


@pytest.fixture
def mock_nats_connect():
    """Patch nats.connect and return mock objects."""
    with patch("nexus.core.event_bus_nats.nats.connect", new_callable=AsyncMock) as mock_connect:
        nc = AsyncMock()
        nc.is_connected = True
        nc.drain = AsyncMock()

        js = AsyncMock()
        js.add_stream = AsyncMock()
        js.find_stream_name_by_subject = AsyncMock(return_value="NEXUS_EVENTS")

        @dataclass
        class MockAck:
            seq: int = 1

        js.publish = AsyncMock(return_value=MockAck())
        # jetstream() is synchronous in nats-py
        nc.jetstream = MagicMock(return_value=js)

        mock_connect.return_value = nc
        yield mock_connect, nc, js


@pytest.fixture(params=["redis", "nats"])
async def event_bus(request, mock_redis_client, mock_nats_connect):
    """Create and start an event bus for each backend."""
    if request.param == "redis":
        bus = RedisEventBus(mock_redis_client)
        await bus.start()
        yield bus
        await bus.stop()
    else:
        from nexus.core.event_bus_nats import NatsEventBus

        bus = NatsEventBus(nats_url="nats://mock:4222")
        await bus.start()
        yield bus
        await bus.stop()


@pytest.fixture
def sample_event():
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/inbox/test.txt",
        zone_id="zone1",
        event_id="evt-contract-1",
    )


# ============================================================================
# Contract Tests
# ============================================================================


class TestEventBusContract:
    """Tests that both backends satisfy the same contract."""

    @pytest.mark.asyncio
    async def test_implements_protocol(self, event_bus):
        """Both backends implement EventBusProtocol."""
        assert isinstance(event_bus, EventBusProtocol)
        assert isinstance(event_bus, EventBusBase)

    @pytest.mark.asyncio
    async def test_start_stop_lifecycle(self, event_bus):
        """start() and stop() are idempotent."""
        # Already started by fixture — start again should be fine
        await event_bus.start()
        assert event_bus._started is True

        await event_bus.stop()
        assert event_bus._started is False

        # Double stop is fine
        await event_bus.stop()

    @pytest.mark.asyncio
    async def test_publish_returns_int(self, event_bus, sample_event):
        """publish() returns an integer (subscriber count or sequence)."""
        result = await event_bus.publish(sample_event)
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_health_check(self, event_bus):
        """health_check() returns a boolean."""
        result = await event_bus.health_check()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_get_stats(self, event_bus):
        """get_stats() returns a dict with backend and status keys."""
        stats = await event_bus.get_stats()
        assert isinstance(stats, dict)
        assert "backend" in stats
        assert "status" in stats

    @pytest.mark.asyncio
    async def test_has_subscribe(self, event_bus):
        """subscribe() method exists and is callable."""
        assert hasattr(event_bus, "subscribe")
        assert callable(event_bus.subscribe)

    @pytest.mark.asyncio
    async def test_has_subscribe_durable(self, event_bus):
        """subscribe_durable() method exists and is callable."""
        assert hasattr(event_bus, "subscribe_durable")
        assert callable(event_bus.subscribe_durable)

    @pytest.mark.asyncio
    async def test_has_wait_for_event(self, event_bus):
        """wait_for_event() method exists and is callable."""
        assert hasattr(event_bus, "wait_for_event")
        assert callable(event_bus.wait_for_event)

    @pytest.mark.asyncio
    async def test_has_startup_sync(self, event_bus):
        """startup_sync() method exists (inherited from EventBusBase)."""
        assert hasattr(event_bus, "startup_sync")
        assert callable(event_bus.startup_sync)

    @pytest.mark.asyncio
    async def test_has_node_id(self, event_bus):
        """Each backend gets a node_id from EventBusBase."""
        assert hasattr(event_bus, "_node_id")
        assert isinstance(event_bus._node_id, str)
        assert len(event_bus._node_id) > 0
