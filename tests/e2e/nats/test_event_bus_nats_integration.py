"""Integration tests for NatsEventBus with a real NATS server.

Requires: nats-server running with JetStream enabled.
Start with:
  docker run -d --name nats-test -p 4222:4222 -p 8222:8222 \
    nats:2.10-alpine --jetstream -m 8222

Set NEXUS_NATS_URL to override the default nats://localhost:4222.

Related: Issue #1331
"""

from __future__ import annotations

import asyncio
import os

import pytest

from nexus.core.event_bus import FileEvent, FileEventType

NATS_URL = os.environ.get("NEXUS_NATS_URL", "nats://localhost:4222")


def _is_nats_available() -> bool:
    """Check if NATS server is reachable."""
    import socket

    try:
        # Parse host:port from nats://host:port
        url = NATS_URL.replace("nats://", "")
        host, port_str = url.split(":")
        port = int(port_str)
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return True
    except (OSError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _is_nats_available(), reason="NATS not available"),
    pytest.mark.xdist_group("nats"),  # All NATS tests share one stream; run sequentially
]


@pytest.fixture
async def nats_bus():
    """Create, start, and clean up a NatsEventBus for testing."""
    from nexus.core.event_bus_nats import NatsEventBus

    bus = NatsEventBus(nats_url=NATS_URL)
    await bus.start()

    yield bus

    # Clean up: delete the stream to avoid test pollution
    if bus._js:
        try:
            await bus._js.delete_stream(bus.STREAM_NAME)
        except Exception:
            pass
    await bus.stop()


class TestNatsEventBusIntegration:
    """Integration tests with a real NATS server."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe_roundtrip(self, nats_bus):
        """Test that a published event can be received via subscribe."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="integration-zone",
        )

        # Publish
        seq = await nats_bus.publish(event)
        assert isinstance(seq, int)
        assert seq > 0

        # Subscribe and receive
        received = []
        async for ackable in nats_bus.subscribe_durable(
            "integration-zone", "test-roundtrip", deliver_policy="all"
        ):
            received.append(ackable)
            await ackable.ack()
            break

        assert len(received) == 1
        assert received[0].event.path == "/inbox/test.txt"
        assert received[0].event.type == "file_write"

    @pytest.mark.asyncio
    async def test_ack_prevents_redelivery(self, nats_bus):
        """Test that acked messages are not redelivered."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/ack-test.txt",
            zone_id="ack-zone",
        )

        await nats_bus.publish(event)

        # First consumer acks
        async for ackable in nats_bus.subscribe_durable(
            "ack-zone", "ack-consumer", deliver_policy="all"
        ):
            await ackable.ack()
            break

        # Second read from same consumer — should not get same message
        from nats.errors import TimeoutError as NatsTimeoutError

        got_message = False
        try:
            sub = await nats_bus._js.pull_subscribe(
                "nexus.events.ack-zone.>",
                durable="ack-consumer",
            )
            msgs = await sub.fetch(batch=1, timeout=1)
            if msgs:
                got_message = True
            await sub.unsubscribe()
        except NatsTimeoutError:
            pass

        assert not got_message, "Acked message should not be redelivered"

    @pytest.mark.asyncio
    async def test_publish_deduplication(self, nats_bus):
        """Test that duplicate event_ids are deduplicated."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/dedup.txt",
            zone_id="dedup-zone",
            event_id="same-id-12345",
        )

        seq1 = await nats_bus.publish(event)
        seq2 = await nats_bus.publish(event)

        # Both publishes succeed, but second should be deduplicated
        # (same sequence number = duplicate detected)
        assert seq1 == seq2

    @pytest.mark.asyncio
    async def test_multiple_zones_independent(self, nats_bus):
        """Test that events in different zones are independent."""
        event_a = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/a.txt",
            zone_id="zone-a",
        )
        event_b = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/b.txt",
            zone_id="zone-b",
        )

        await nats_bus.publish(event_a)
        await nats_bus.publish(event_b)

        # Consumer for zone-a should only get event_a
        received_a = []
        async for ackable in nats_bus.subscribe_durable(
            "zone-a", "zone-a-consumer", deliver_policy="all"
        ):
            received_a.append(ackable.event)
            await ackable.ack()
            break

        assert len(received_a) == 1
        assert received_a[0].path == "/inbox/a.txt"

    @pytest.mark.asyncio
    async def test_health_check(self, nats_bus):
        """Test health check against live NATS."""
        result = await nats_bus.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_stats(self, nats_bus):
        """Test get_stats against live NATS."""
        # Publish one event so stream has data
        await nats_bus.publish(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path="/inbox/stats-test.txt",
                zone_id="stats-zone",
            )
        )

        stats = await nats_bus.get_stats()

        assert stats["backend"] == "nats_jetstream"
        assert stats["status"] == "running"
        assert "stream" in stats
        assert stats["stream"]["messages"] >= 1

    @pytest.mark.asyncio
    async def test_wait_for_event(self, nats_bus):
        """Test wait_for_event with live NATS."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/wait-test.txt",
            zone_id="wait-zone",
        )

        # Publish after a short delay
        async def delayed_publish():
            await asyncio.sleep(0.2)
            await nats_bus.publish(event)

        task = asyncio.create_task(delayed_publish())

        result = await nats_bus.wait_for_event("wait-zone", "/inbox/", timeout=5.0)

        await task

        assert result is not None
        assert result.path == "/inbox/wait-test.txt"

    @pytest.mark.asyncio
    async def test_consumer_group_independent_delivery(self, nats_bus):
        """Test that different consumer names get independent delivery."""
        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/group-test.txt",
            zone_id="group-zone",
        )

        await nats_bus.publish(event)

        # Consumer A
        received_a = []
        async for ackable in nats_bus.subscribe_durable(
            "group-zone", "consumer-a", deliver_policy="all"
        ):
            received_a.append(ackable.event)
            await ackable.ack()
            break

        # Consumer B — same event, independent consumer
        received_b = []
        async for ackable in nats_bus.subscribe_durable(
            "group-zone", "consumer-b", deliver_policy="all"
        ):
            received_b.append(ackable.event)
            await ackable.ack()
            break

        assert len(received_a) == 1
        assert len(received_b) == 1
        assert received_a[0].event_id == received_b[0].event_id
