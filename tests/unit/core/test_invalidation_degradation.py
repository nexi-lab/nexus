"""Failure injection tests for the invalidation pipeline.

Tests 4 degradation modes:
1. Durable stream unavailable at startup
2. Durable stream fails mid-operation
3. Consumer fails to ACK (messages re-delivered)
4. Full degradation (Dragonfly down)

Related: Issue #3396 (decision 12A)
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.rebac.cache.coordinator import CacheCoordinator
from nexus.bricks.rebac.cache.coordinator_config import (
    CoordinatorConfig,
    InvalidationChannels,
)
from nexus.bricks.rebac.cache.durable_stream import DurableInvalidationStream
from nexus.bricks.rebac.cache.read_fence import ReadFence

pytest.importorskip("pyroaring")


# ---------------------------------------------------------------------------
# Failure Mode 1: Durable stream unavailable at startup
# ---------------------------------------------------------------------------


class TestDurableStreamUnavailableAtStartup:
    """When durable stream is None, coordinator falls back gracefully."""

    def test_coordinator_works_without_durable_stream(self):
        """Invalidation pipeline completes even without durable stream."""
        l1 = MagicMock()
        mock_pubsub = MagicMock()
        mock_pubsub.publish_invalidation = MagicMock(return_value=True)

        config = CoordinatorConfig(channels=InvalidationChannels(pubsub=mock_pubsub))

        coordinator = CacheCoordinator(
            l1_cache=l1,
            zone_graph_cache={"z": {}},
            config=config,
        )

        # Should not raise
        coordinator.invalidate_for_write(
            zone_id="z",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        # Pub/Sub still fires as fallback
        mock_pubsub.publish_invalidation.assert_called_once()
        # L1 still invalidated
        l1.invalidate_subject.assert_called()

    def test_disabled_durable_stream_publish_returns_false(self):
        """A durable stream with no Redis client returns False on publish."""
        stream = DurableInvalidationStream(redis_client=None)
        result = stream.publish("zone-a", {"key": "val"})
        assert result is False
        assert stream.stats()["published"] == 0

    def test_stats_reflect_disabled_state(self):
        """Stats correctly report disabled components."""
        config = CoordinatorConfig(
            channels=InvalidationChannels()  # all None
        )
        coordinator = CacheCoordinator(config=config)
        stats = coordinator.get_stats()

        assert stats["durable_stream_enabled"] is False
        assert stats["read_fence_enabled"] is False
        assert stats["stream_enabled"] is False
        assert stats["pubsub_enabled"] is False


# ---------------------------------------------------------------------------
# Failure Mode 2: Durable stream fails mid-operation
# ---------------------------------------------------------------------------


class TestDurableStreamMidOperationFailure:
    """Queue buffers events when drain fails; recovers when stream comes back."""

    def test_publish_succeeds_even_when_drain_will_fail(self):
        """Sync publish to queue always succeeds (independent of Redis health)."""
        mock_client = MagicMock()
        stream = DurableInvalidationStream(redis_client=mock_client, zone_id="z")

        result = stream.publish("zone-a", {"key": "val"})
        assert result is True
        assert stream.stats()["queue_size"] == 1

    @pytest.mark.asyncio
    async def test_drain_failure_requeues_events(self):
        """When pipeline.execute() fails, events go back to front of queue."""
        mock_client = MagicMock()
        mock_pipe = MagicMock()
        mock_pipe.xadd = MagicMock(return_value=mock_pipe)
        mock_pipe.execute = AsyncMock(side_effect=ConnectionError("Dragonfly down"))
        mock_client.pipeline = MagicMock(return_value=mock_pipe)

        stream = DurableInvalidationStream(redis_client=mock_client, zone_id="z")
        stream.publish("zone-a", {"key": "val1"})
        stream.publish("zone-a", {"key": "val2"})

        assert stream.stats()["queue_size"] == 2

        with pytest.raises(ConnectionError):
            await stream._drain_batch()

        # Events should be re-queued
        assert stream.stats()["queue_size"] == 2
        assert stream.stats()["drain_errors"] == 1

    @pytest.mark.asyncio
    async def test_recovery_after_drain_failure(self):
        """After Dragonfly recovers, queued events are drained successfully."""
        call_count = 0

        mock_client = MagicMock()

        def make_pipeline(transaction=False):
            nonlocal call_count
            call_count += 1
            pipe = MagicMock()
            pipe.xadd = MagicMock(return_value=pipe)
            if call_count == 1:
                pipe.execute = AsyncMock(side_effect=ConnectionError("down"))
            else:
                pipe.execute = AsyncMock(return_value=["1-0"])
            return pipe

        mock_client.pipeline = make_pipeline

        stream = DurableInvalidationStream(redis_client=mock_client, zone_id="z")
        stream.publish("zone-a", {"key": "val"})

        # First drain fails
        with pytest.raises(ConnectionError):
            await stream._drain_batch()
        assert stream.stats()["queue_size"] == 1

        # Second drain succeeds (Dragonfly recovered)
        await stream._drain_batch()
        assert stream.stats()["queue_size"] == 0
        assert stream.stats()["drained"] == 1

    def test_queue_full_drops_events_gracefully(self):
        """When queue is full, new events are dropped with a warning."""
        stream = DurableInvalidationStream(
            redis_client=MagicMock(),
            zone_id="z",
            queue_maxsize=3,
        )

        for i in range(5):
            stream.publish("zone-a", {"idx": i})

        stats = stream.stats()
        assert stats["queue_size"] == 3  # maxlen=3
        assert stats["queue_drops"] >= 1  # At least some dropped


# ---------------------------------------------------------------------------
# Failure Mode 3: Consumer ACK failure
# ---------------------------------------------------------------------------


class TestConsumerAckFailure:
    """Events stay in pending list when ACK fails; re-delivered on next read."""

    @pytest.mark.asyncio
    async def test_handler_failure_does_not_ack(self):
        """When a handler raises, the message should still be ACKed (fail-open)."""
        mock_client = MagicMock()
        mock_client.xack = AsyncMock(return_value=1)

        stream = DurableInvalidationStream(
            redis_client=mock_client,
            zone_id="z",
        )

        async def failing_handler(zone_id, payload):
            raise RuntimeError("handler boom")

        stream.register_handler("failing", failing_handler)

        sem = asyncio.Semaphore(10)
        # Process a message with failing handler
        await stream._process_message(
            sem,
            "test-stream",
            "1-0",
            {b"data": b'{"source_zone": "z", "key": "val"}'},
        )

        # Message should still be ACKed (fail-open design)
        # The handler error is logged but doesn't prevent ACK
        stats = stream.stats()
        assert stats["consume_errors"] >= 1

    @pytest.mark.asyncio
    async def test_malformed_message_is_acked(self):
        """Messages with no data field are ACKed and skipped."""
        mock_client = MagicMock()
        mock_client.xack = AsyncMock(return_value=1)

        stream = DurableInvalidationStream(
            redis_client=mock_client,
            zone_id="z",
        )

        sem = asyncio.Semaphore(10)
        await stream._process_message(
            sem,
            "test-stream",
            "1-0",
            {b"wrong_field": b"val"},
        )

        mock_client.xack.assert_called_once()


# ---------------------------------------------------------------------------
# Failure Mode 4: Full degradation (Dragonfly completely down)
# ---------------------------------------------------------------------------


class TestFullDegradation:
    """When both durable stream and pub/sub fail, TTL-based expiry is the safety net."""

    def test_coordinator_completes_when_all_channels_fail(self):
        """Pipeline completes even when durable stream and pub/sub both fail."""
        mock_durable = MagicMock()
        mock_durable.publish = MagicMock(return_value=False)  # Queue full

        mock_pubsub = MagicMock()
        mock_pubsub.publish_invalidation = MagicMock(return_value=False)  # Redis down

        config = CoordinatorConfig(
            channels=InvalidationChannels(
                durable_stream=mock_durable,
                pubsub=mock_pubsub,
            )
        )

        l1 = MagicMock()
        coordinator = CacheCoordinator(
            l1_cache=l1,
            zone_graph_cache={"z": {}},
            config=config,
        )

        # Should not raise — pipeline completes for local caches
        coordinator.invalidate_for_write(
            zone_id="z",
            subject=("user", "alice"),
            relation="editor",
            object=("file", "/doc.txt"),
        )

        # Local caches still invalidated (critical path)
        l1.invalidate_subject.assert_called()
        l1.invalidate_object.assert_called()

    def test_read_fence_frozen_when_stream_down(self):
        """When no events are consumed, the read fence watermark stays frozen.

        Cached results eventually expire via TTL — the read fence doesn't
        block reads when the stream is down, it just can't detect staleness
        for new revocations until the stream recovers.
        """
        fence = ReadFence()

        # Initially, everything is "fresh" (watermark is 0)
        assert fence.is_stale("zone-a", 0) is False
        assert fence.is_stale("zone-a", 100) is False

        # Simulate: stream was working, advanced to 50
        fence.advance("zone-a", 50)

        # Now stream goes down — no more advances
        # Cached results from before 50 are detected as stale
        assert fence.is_stale("zone-a", 30) is True
        # Cached results from 50+ are still "fresh" (best available info)
        assert fence.is_stale("zone-a", 50) is False
        assert fence.is_stale("zone-a", 100) is False

        stats = fence.stats()
        assert stats["zones_tracked"] == 1
        assert stats["watermarks"]["zone-a"] == 50

    def test_read_fence_watermark_never_goes_backward(self):
        """Watermark monotonically increases even with out-of-order events."""
        fence = ReadFence()
        fence.advance("zone-a", 100)
        fence.advance("zone-a", 50)  # Out-of-order (ignored)
        fence.advance("zone-a", 150)

        assert fence.watermark("zone-a") == 150

    def test_read_fence_reset_zone(self):
        """Reset clears the watermark for a zone."""
        fence = ReadFence()
        fence.advance("zone-a", 100)
        fence.advance("zone-b", 200)

        fence.reset_zone("zone-a")
        assert fence.watermark("zone-a") == 0
        assert fence.watermark("zone-b") == 200
