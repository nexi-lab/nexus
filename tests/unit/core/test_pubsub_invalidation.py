"""Foundation tests for PubSubInvalidation.

Establishes behavioral baseline before the durable channel refactor.
Covers: publish/receive round-trip, ChannelCodec integration, subscriber
failure isolation, disabled-mode behavior, and stats accuracy.

Related: Issue #3396 (decision 9A)
"""

from unittest.mock import MagicMock

from nexus.bricks.rebac.cache.channel_codec import decode_channel, encode_channel
from nexus.bricks.rebac.cache.pubsub_invalidation import PubSubInvalidation


class TestPubSubPublish:
    """Publishing invalidation hints."""

    def test_publish_calls_redis_with_encoded_channel(self):
        mock_client = MagicMock()
        ps = PubSubInvalidation(redis_client=mock_client)

        ps.publish_invalidation("zone-a", "boundary", {"key": "val"})

        mock_client.publish.assert_called_once()
        call_args = mock_client.publish.call_args
        channel = call_args[0][0]
        # Should use ChannelCodec pipe delimiter, not colons
        assert "|" in channel
        assert "zone-a" in channel
        assert "boundary" in channel

    def test_publish_returns_true_on_success(self):
        mock_client = MagicMock()
        ps = PubSubInvalidation(redis_client=mock_client)
        assert ps.publish_invalidation("z", "l", {"k": "v"}) is True

    def test_publish_returns_false_when_disabled(self):
        ps = PubSubInvalidation(redis_client=None)
        assert ps.publish_invalidation("z", "l", {"k": "v"}) is False

    def test_publish_returns_false_on_error(self):
        mock_client = MagicMock()
        mock_client.publish.side_effect = ConnectionError("down")
        ps = PubSubInvalidation(redis_client=mock_client)

        result = ps.publish_invalidation("z", "l", {"k": "v"})
        assert result is False

    def test_publish_increments_error_counter_on_failure(self):
        mock_client = MagicMock()
        mock_client.publish.side_effect = ConnectionError("down")
        ps = PubSubInvalidation(redis_client=mock_client)

        ps.publish_invalidation("z", "l", {"k": "v"})

        stats = ps.get_stats()
        assert stats["publish_errors"] == 1
        assert stats["published"] == 0

    def test_publish_increments_published_counter(self):
        mock_client = MagicMock()
        ps = PubSubInvalidation(redis_client=mock_client)

        ps.publish_invalidation("z", "l", {"k": "v"})
        ps.publish_invalidation("z", "l", {"k": "v2"})

        assert ps.get_stats()["published"] == 2


class TestPubSubSubscribe:
    """Subscribing and receiving invalidation hints."""

    def test_subscribe_returns_sub_id(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        sub_id = ps.subscribe("zone-a", "boundary", lambda p: None)
        assert sub_id == "zone-a:boundary"

    def test_handle_message_dispatches_to_subscriber(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        received = []
        ps.subscribe("zone-a", "boundary", received.append)

        channel = encode_channel("rebac:invalidation", "zone-a", "boundary")
        ps.handle_message(channel, '{"key": "val"}')

        assert len(received) == 1
        assert received[0] == {"key": "val"}

    def test_handle_message_increments_received_counter(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        ps.subscribe("z", "l", lambda p: None)

        channel = encode_channel("rebac:invalidation", "z", "l")
        ps.handle_message(channel, '{"k": "v"}')

        assert ps.get_stats()["received"] == 1

    def test_handle_message_ignores_invalid_json(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        ps.subscribe("z", "l", lambda p: None)

        channel = encode_channel("rebac:invalidation", "z", "l")
        ps.handle_message(channel, "not-json{{{")

        assert ps.get_stats()["received"] == 0

    def test_handle_message_ignores_unknown_channel(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        received = []
        ps.subscribe("zone-a", "boundary", received.append)

        # Message for different zone
        channel = encode_channel("rebac:invalidation", "zone-b", "boundary")
        ps.handle_message(channel, '{"k": "v"}')

        assert len(received) == 0

    def test_subscriber_failure_does_not_crash(self):
        ps = PubSubInvalidation(redis_client=MagicMock())

        def bad_callback(payload):
            raise RuntimeError("boom")

        ps.subscribe("z", "l", bad_callback)

        channel = encode_channel("rebac:invalidation", "z", "l")
        # Should not raise
        ps.handle_message(channel, '{"k": "v"}')

    def test_unsubscribe_stops_delivery(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        received = []
        sub_id = ps.subscribe("z", "l", received.append)

        channel = encode_channel("rebac:invalidation", "z", "l")
        ps.handle_message(channel, '{"k": "v1"}')
        assert len(received) == 1

        ps.unsubscribe(sub_id)
        ps.handle_message(channel, '{"k": "v2"}')
        assert len(received) == 1


class TestPubSubStats:
    """Stats and disabled mode."""

    def test_disabled_stats(self):
        ps = PubSubInvalidation(redis_client=None)
        stats = ps.get_stats()
        assert stats["enabled"] is False
        assert stats["published"] == 0
        assert stats["subscriber_count"] == 0

    def test_enabled_stats(self):
        ps = PubSubInvalidation(redis_client=MagicMock())
        ps.subscribe("z", "l", lambda p: None)
        stats = ps.get_stats()
        assert stats["enabled"] is True
        assert stats["subscriber_count"] == 1


class TestChannelCodec:
    """ChannelCodec encode/decode round-trips."""

    def test_basic_round_trip(self):
        encoded = encode_channel("prefix", "zone-a", "boundary")
        decoded = decode_channel(encoded)
        assert decoded == ("prefix", "zone-a", "boundary")

    def test_zone_id_with_colons(self):
        """Zone IDs containing colons should be preserved."""
        encoded = encode_channel("pfx", "us-east-1:partition-2", "all")
        decoded = decode_channel(encoded)
        assert decoded == ("pfx", "us-east-1:partition-2", "all")

    def test_invalid_channel_returns_none(self):
        assert decode_channel("no-delimiters") is None
        assert decode_channel("too|many|pipe|delimiters") is None
        assert decode_channel("") is None

    def test_empty_components(self):
        encoded = encode_channel("", "", "")
        decoded = decode_channel(encoded)
        assert decoded == ("", "", "")
