"""Unit tests for NatsEventBus (mocked NATS).

Tests cover:
- Connection lifecycle (start/stop)
- Subject construction
- Publish with JetStream ack, headers, dedup ID
- Subscribe (auto-ack wrapper)
- Durable subscribe (pull consumer, ack/nack/in_progress)
- Wait for event (ephemeral consumer)
- Health check
- Reconnection callbacks
- Error handling (malformed messages, NATS errors)

Related: Issue #1331
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.core.event_bus import AckableEvent, FileEvent, FileEventType

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_nats_connect():
    """Patch nats.connect to return a mock NATS client."""
    with patch("nexus.core.event_bus_nats.nats.connect", new_callable=AsyncMock) as mock_connect:
        nc = AsyncMock()
        nc.is_connected = True
        nc.drain = AsyncMock()

        js = AsyncMock()
        js.add_stream = AsyncMock()
        # jetstream() is synchronous in nats-py
        nc.jetstream = MagicMock(return_value=js)

        mock_connect.return_value = nc
        yield mock_connect, nc, js


@pytest.fixture
def make_bus():
    """Create a NatsEventBus with default test settings."""

    def _make(**kwargs):
        from nexus.core.event_bus_nats import NatsEventBus

        defaults = {"nats_url": "nats://test:4222"}
        defaults.update(kwargs)
        return NatsEventBus(**defaults)

    return _make


@pytest.fixture
def sample_event():
    """A sample FileEvent for testing."""
    return FileEvent(
        type=FileEventType.FILE_WRITE,
        path="/inbox/test.txt",
        zone_id="zone1",
        event_id="evt-123",
    )


# ============================================================================
# Start / Stop Tests
# ============================================================================


class TestNatsEventBusStartStop:
    """Tests for connection lifecycle."""

    @pytest.mark.asyncio
    async def test_start_connects_and_creates_stream(self, mock_nats_connect, make_bus):
        mock_connect, nc, js = mock_nats_connect
        bus = make_bus()

        await bus.start()

        assert bus._started is True
        mock_connect.assert_called_once()
        js.add_stream.assert_called_once()

        # Verify stream config
        call_args = js.add_stream.call_args
        config = call_args[0][0]
        assert config.name == "NEXUS_EVENTS"
        assert "nexus.events.>" in config.subjects

    @pytest.mark.asyncio
    async def test_start_idempotent(self, mock_nats_connect, make_bus):
        mock_connect, nc, js = mock_nats_connect
        bus = make_bus()

        await bus.start()
        await bus.start()

        # Should only connect once
        mock_connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_drains_connection(self, mock_nats_connect, make_bus):
        _, nc, _ = mock_nats_connect
        bus = make_bus()

        await bus.start()
        await bus.stop()

        assert bus._started is False
        nc.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, mock_nats_connect, make_bus):
        _, nc, _ = mock_nats_connect
        bus = make_bus()

        await bus.start()
        await bus.stop()
        await bus.stop()

        # Drain called only once
        nc.drain.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_before_start_is_noop(self, make_bus):
        bus = make_bus()
        await bus.stop()
        assert bus._started is False


# ============================================================================
# Publish Tests
# ============================================================================


class TestNatsEventBusPublish:
    """Tests for event publishing."""

    @pytest.mark.asyncio
    async def test_publish_builds_correct_subject(self, mock_nats_connect, make_bus, sample_event):
        _, _, js = mock_nats_connect

        @dataclass
        class MockAck:
            seq: int = 42

        js.publish = AsyncMock(return_value=MockAck())
        bus = make_bus()
        await bus.start()

        seq = await bus.publish(sample_event)

        assert seq == 42
        call_args = js.publish.call_args
        assert call_args[0][0] == "nexus.events.zone1.file_write"

    @pytest.mark.asyncio
    async def test_publish_sends_json_payload(self, mock_nats_connect, make_bus, sample_event):
        _, _, js = mock_nats_connect

        @dataclass
        class MockAck:
            seq: int = 1

        js.publish = AsyncMock(return_value=MockAck())
        bus = make_bus()
        await bus.start()

        await bus.publish(sample_event)

        call_args = js.publish.call_args
        payload = call_args[0][1]
        parsed = json.loads(payload.decode())
        assert parsed["type"] == "file_write"
        assert parsed["path"] == "/inbox/test.txt"

    @pytest.mark.asyncio
    async def test_publish_includes_dedup_header(self, mock_nats_connect, make_bus, sample_event):
        _, _, js = mock_nats_connect

        @dataclass
        class MockAck:
            seq: int = 1

        js.publish = AsyncMock(return_value=MockAck())
        bus = make_bus()
        await bus.start()

        await bus.publish(sample_event)

        call_args = js.publish.call_args
        headers = call_args[1]["headers"]
        assert headers["Nats-Msg-Id"] == "evt-123"
        assert headers["zone_id"] == "zone1"

    @pytest.mark.asyncio
    async def test_publish_default_zone(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect

        @dataclass
        class MockAck:
            seq: int = 1

        js.publish = AsyncMock(return_value=MockAck())
        bus = make_bus()
        await bus.start()

        event = FileEvent(type=FileEventType.FILE_WRITE, path="/test.txt", zone_id=None)
        await bus.publish(event)

        call_args = js.publish.call_args
        assert call_args[0][0] == "nexus.events.default.file_write"

    @pytest.mark.asyncio
    async def test_publish_requires_start(self, make_bus, sample_event):
        bus = make_bus()
        with pytest.raises(RuntimeError, match="not started"):
            await bus.publish(sample_event)

    @pytest.mark.asyncio
    async def test_publish_propagates_nats_error(self, mock_nats_connect, make_bus, sample_event):
        _, _, js = mock_nats_connect
        js.publish = AsyncMock(side_effect=Exception("NATS unavailable"))
        bus = make_bus()
        await bus.start()

        with pytest.raises(Exception, match="NATS unavailable"):
            await bus.publish(sample_event)


# ============================================================================
# Subscribe Tests (auto-ack wrapper)
# ============================================================================


class TestNatsEventBusSubscribe:
    """Tests for the backward-compat subscribe() wrapper."""

    @pytest.mark.asyncio
    async def test_subscribe_yields_events(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        # Mock the pull subscription
        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        event1 = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file1.txt",
            zone_id="z1",
        )
        event2 = FileEvent(
            type=FileEventType.FILE_DELETE,
            path="/test/file2.txt",
            zone_id="z1",
        )

        msg1 = MagicMock()
        msg1.data = event1.to_json().encode()
        msg1.subject = "nexus.events.z1.file_write"
        msg1.ack = AsyncMock()
        msg1.nak = AsyncMock()
        msg1.in_progress = AsyncMock()

        msg2 = MagicMock()
        msg2.data = event2.to_json().encode()
        msg2.subject = "nexus.events.z1.file_delete"
        msg2.ack = AsyncMock()
        msg2.nak = AsyncMock()
        msg2.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg1, msg2]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        received = []
        count = 0
        async for event in bus.subscribe("z1"):
            received.append(event)
            count += 1
            if count >= 2:
                break

        assert len(received) == 2
        assert received[0].path == "/test/file1.txt"
        assert received[1].path == "/test/file2.txt"
        # Auto-ack should have been called
        msg1.ack.assert_called_once()
        msg2.ack.assert_called_once()


# ============================================================================
# Durable Subscribe Tests
# ============================================================================


class TestNatsEventBusSubscribeDurable:
    """Tests for durable pull consumer subscription."""

    @pytest.mark.asyncio
    async def test_durable_creates_consumer(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        from nats.errors import TimeoutError as NatsTimeoutError

        async def mock_fetch(batch=10, timeout=5):
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        # Just verify the subscription is created
        gen = bus.subscribe_durable("z1", "test-consumer", deliver_policy="all")
        # Need to actually iterate to trigger the pull_subscribe call
        task = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass

        js.pull_subscribe.assert_called_once()
        call_args = js.pull_subscribe.call_args
        assert call_args[1]["durable"] == "test-consumer"

    @pytest.mark.asyncio
    async def test_durable_yields_ackable_events(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="z1",
        )

        msg = MagicMock()
        msg.data = event.to_json().encode()
        msg.subject = "nexus.events.z1.file_write"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msg.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        received = []
        async for ackable in bus.subscribe_durable("z1", "consumer-1"):
            assert isinstance(ackable, AckableEvent)
            assert ackable.event.path == "/test/file.txt"
            received.append(ackable)
            break

        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_durable_ack_calls_msg_ack(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="z1",
        )

        msg = MagicMock()
        msg.data = event.to_json().encode()
        msg.subject = "nexus.events.z1.file_write"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msg.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        async for ackable in bus.subscribe_durable("z1", "consumer-1"):
            await ackable.ack()
            break

        msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_durable_nack_calls_msg_nak(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="z1",
        )

        msg = MagicMock()
        msg.data = event.to_json().encode()
        msg.subject = "nexus.events.z1.file_write"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msg.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        async for ackable in bus.subscribe_durable("z1", "consumer-1"):
            await ackable.nack(delay=5.0)
            break

        msg.nak.assert_called_once_with(delay=5.0)

    @pytest.mark.asyncio
    async def test_durable_in_progress(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/file.txt",
            zone_id="z1",
        )

        msg = MagicMock()
        msg.data = event.to_json().encode()
        msg.subject = "nexus.events.z1.file_write"
        msg.ack = AsyncMock()
        msg.nak = AsyncMock()
        msg.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [msg]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        async for ackable in bus.subscribe_durable("z1", "consumer-1"):
            await ackable.in_progress()
            break

        msg.in_progress.assert_called_once()

    @pytest.mark.asyncio
    async def test_durable_skips_malformed_messages(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        js.pull_subscribe = AsyncMock(return_value=mock_sub)

        good_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/test/good.txt",
            zone_id="z1",
        )

        bad_msg = MagicMock()
        bad_msg.data = b"not valid json{{{"
        bad_msg.subject = "nexus.events.z1.file_write"
        bad_msg.ack = AsyncMock()

        good_msg = MagicMock()
        good_msg.data = good_event.to_json().encode()
        good_msg.subject = "nexus.events.z1.file_write"
        good_msg.ack = AsyncMock()
        good_msg.nak = AsyncMock()
        good_msg.in_progress = AsyncMock()

        from nats.errors import TimeoutError as NatsTimeoutError

        call_count = [0]

        async def mock_fetch(batch=10, timeout=5):
            call_count[0] += 1
            if call_count[0] == 1:
                return [bad_msg, good_msg]
            raise NatsTimeoutError

        mock_sub.fetch = mock_fetch
        mock_sub.unsubscribe = AsyncMock()

        received = []
        async for ackable in bus.subscribe_durable("z1", "consumer-1"):
            received.append(ackable)
            break

        assert len(received) == 1
        assert received[0].event.path == "/test/good.txt"
        # Bad message should have been acked to prevent redelivery
        bad_msg.ack.assert_called_once()

    @pytest.mark.asyncio
    async def test_durable_requires_start(self, make_bus):
        bus = make_bus()
        with pytest.raises(RuntimeError, match="not started"):
            async for _ in bus.subscribe_durable("z1", "consumer-1"):
                pass


# ============================================================================
# Wait For Event Tests
# ============================================================================


class TestNatsEventBusWaitForEvent:
    """Tests for wait_for_event with ephemeral consumer."""

    @pytest.mark.asyncio
    async def test_wait_for_event_returns_matching(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="z1",
        )

        mock_sub = AsyncMock()
        mock_msg = MagicMock()
        mock_msg.data = event.to_json().encode()
        mock_sub.next_msg = AsyncMock(return_value=mock_msg)
        mock_sub.unsubscribe = AsyncMock()
        js.subscribe = AsyncMock(return_value=mock_sub)

        result = await bus.wait_for_event("z1", "/inbox/", timeout=5.0)

        assert result is not None
        assert result.path == "/inbox/test.txt"

    @pytest.mark.asyncio
    async def test_wait_for_event_timeout(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_sub = AsyncMock()
        mock_sub.next_msg = AsyncMock(side_effect=TimeoutError)
        mock_sub.unsubscribe = AsyncMock()
        js.subscribe = AsyncMock(return_value=mock_sub)

        result = await bus.wait_for_event("z1", "/inbox/", timeout=0.1)

        assert result is None

    @pytest.mark.asyncio
    async def test_wait_for_event_ignores_non_matching(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        non_matching = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/other/test.txt",
            zone_id="z1",
        )
        matching = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="z1",
        )

        mock_sub = AsyncMock()
        msg1 = MagicMock()
        msg1.data = non_matching.to_json().encode()
        msg2 = MagicMock()
        msg2.data = matching.to_json().encode()
        mock_sub.next_msg = AsyncMock(side_effect=[msg1, msg2])
        mock_sub.unsubscribe = AsyncMock()
        js.subscribe = AsyncMock(return_value=mock_sub)

        result = await bus.wait_for_event("z1", "/inbox/", timeout=5.0)

        assert result is not None
        assert result.path == "/inbox/test.txt"

    @pytest.mark.asyncio
    async def test_wait_for_event_respects_since_revision(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        old_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="z1",
            revision=5,
        )
        new_event = FileEvent(
            type=FileEventType.FILE_WRITE,
            path="/inbox/test.txt",
            zone_id="z1",
            revision=10,
        )

        mock_sub = AsyncMock()
        msg1 = MagicMock()
        msg1.data = old_event.to_json().encode()
        msg2 = MagicMock()
        msg2.data = new_event.to_json().encode()
        mock_sub.next_msg = AsyncMock(side_effect=[msg1, msg2])
        mock_sub.unsubscribe = AsyncMock()
        js.subscribe = AsyncMock(return_value=mock_sub)

        result = await bus.wait_for_event("z1", "/inbox/", timeout=5.0, since_revision=7)

        assert result is not None
        assert result.revision == 10

    @pytest.mark.asyncio
    async def test_wait_for_event_requires_start(self, make_bus):
        bus = make_bus()
        with pytest.raises(RuntimeError, match="not started"):
            await bus.wait_for_event("z1", "/inbox/")


# ============================================================================
# Health Check Tests
# ============================================================================


class TestNatsEventBusHealthCheck:
    """Tests for health check."""

    @pytest.mark.asyncio
    async def test_health_check_when_connected(self, mock_nats_connect, make_bus):
        _, nc, js = mock_nats_connect
        js.find_stream_name_by_subject = AsyncMock(return_value="NEXUS_EVENTS")
        bus = make_bus()
        await bus.start()

        result = await bus.health_check()

        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_when_not_started(self, make_bus):
        bus = make_bus()
        result = await bus.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_when_disconnected(self, mock_nats_connect, make_bus):
        _, nc, _ = mock_nats_connect
        bus = make_bus()
        await bus.start()

        nc.is_connected = False
        result = await bus.health_check()

        assert result is False


# ============================================================================
# Reconnection Callback Tests
# ============================================================================


class TestNatsEventBusReconnection:
    """Tests for disconnect/reconnect callbacks."""

    @pytest.mark.asyncio
    async def test_on_disconnect_logs(self, make_bus):
        bus = make_bus()
        # Should not raise
        await bus._on_disconnect()

    @pytest.mark.asyncio
    async def test_on_reconnect_logs(self, make_bus):
        bus = make_bus()
        await bus._on_reconnect()

    @pytest.mark.asyncio
    async def test_on_error_logs(self, make_bus):
        bus = make_bus()
        await bus._on_error(Exception("test error"))


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestNatsEventBusErrors:
    """Tests for error scenarios."""

    @pytest.mark.asyncio
    async def test_start_raises_on_connection_failure(self, make_bus):
        from nats.errors import NoServersError

        with patch(
            "nexus.core.event_bus_nats.nats.connect",
            new_callable=AsyncMock,
            side_effect=NoServersError,
        ):
            bus = make_bus()
            with pytest.raises(NoServersError):
                await bus.start()

    @pytest.mark.asyncio
    async def test_subject_construction(self, make_bus):
        bus = make_bus()
        assert bus._subject("zone1", "file_write") == "nexus.events.zone1.file_write"
        assert bus._zone_wildcard("zone1") == "nexus.events.zone1.>"

    @pytest.mark.asyncio
    async def test_get_stats_includes_stream_info(self, mock_nats_connect, make_bus):
        _, _, js = mock_nats_connect
        bus = make_bus()
        await bus.start()

        mock_state = MagicMock()
        mock_state.messages = 100
        mock_state.bytes = 5000
        mock_state.first_seq = 1
        mock_state.last_seq = 100
        mock_state.consumer_count = 3

        mock_info = MagicMock()
        mock_info.state = mock_state

        js.stream_info = AsyncMock(return_value=mock_info)

        stats = await bus.get_stats()

        assert stats["backend"] == "nats_jetstream"
        assert stats["status"] == "running"
        assert stats["stream"]["messages"] == 100
        assert stats["stream"]["consumer_count"] == 3
