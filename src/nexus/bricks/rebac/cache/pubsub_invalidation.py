"""Pub/Sub cross-zone cache invalidation hints.

Best-effort invalidation for cross-zone scenarios. Unlike DT_STREAM
(which provides ordered, reliable intra-zone invalidation), Pub/Sub
provides fire-and-forget hints to other zones.

Channels: rebac:invalidation:{zone_id}:{layer}

Messages are hints, not commands — a missed message means slightly
stale cache that will self-correct on TTL expiry.

Related: Issue #3192
"""

import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class PubSubInvalidation:
    """Cross-zone invalidation via Redis/Dragonfly Pub/Sub.

    Publishes invalidation hints to zone-specific channels.
    Subscribers in other zones receive hints and invalidate local caches.

    This is best-effort: if a subscriber is down, it misses the hint
    and relies on TTL-based expiry for eventual consistency.
    """

    def __init__(
        self,
        redis_client: Any = None,
        channel_prefix: str = "rebac:invalidation",
    ):
        """Initialize Pub/Sub invalidation.

        Args:
            redis_client: Redis/Dragonfly client instance (async)
            channel_prefix: Prefix for pub/sub channels
        """
        self._client = redis_client
        self._channel_prefix = channel_prefix
        self._subscribers: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._enabled = redis_client is not None

        # Metrics
        self._published = 0
        self._received = 0
        self._publish_errors = 0

    def publish_invalidation(
        self,
        zone_id: str,
        layer: str,
        payload: dict[str, Any],
    ) -> bool:
        """Publish an invalidation hint to a zone-specific channel.

        Args:
            zone_id: Target zone for the invalidation
            layer: Cache layer (e.g., "boundary", "visibility", "l1")
            payload: Invalidation details

        Returns:
            True if published successfully, False otherwise
        """
        if not self._enabled:
            return False

        channel = f"{self._channel_prefix}:{zone_id}:{layer}"
        message = json.dumps(payload)

        try:
            self._client.publish(channel, message)
            self._published += 1
            logger.debug("[PUBSUB] Published to %s: %s", channel, payload)
            return True
        except Exception:
            self._publish_errors += 1
            logger.warning(
                "[PUBSUB] Failed to publish to %s",
                channel,
                exc_info=True,
            )
            return False

    def subscribe(
        self,
        zone_id: str,
        layer: str,
        callback: Callable[[dict[str, Any]], None],
    ) -> str:
        """Subscribe to invalidation hints for a zone+layer.

        Args:
            zone_id: Zone to listen for
            layer: Cache layer to listen for
            callback: Function called with invalidation payload

        Returns:
            Subscription ID for unsubscribe
        """
        channel = f"{self._channel_prefix}:{zone_id}:{layer}"
        sub_id = f"{zone_id}:{layer}"
        self._subscribers[sub_id] = callback
        logger.info("[PUBSUB] Subscribed to %s", channel)
        return sub_id

    def unsubscribe(self, sub_id: str) -> None:
        """Unsubscribe from invalidation hints."""
        self._subscribers.pop(sub_id, None)

    def handle_message(self, channel: str, message: str) -> None:
        """Handle an incoming pub/sub message.

        Called by the Redis subscriber when a message arrives.

        Args:
            channel: Channel the message was received on
            message: JSON-encoded invalidation payload
        """
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("[PUBSUB] Invalid message on %s: %s", channel, message[:100])
            return

        self._received += 1

        # Extract zone and layer from channel
        parts = channel.split(":")
        if len(parts) >= 4:
            zone_id = parts[2]
            layer = parts[3]
            sub_id = f"{zone_id}:{layer}"

            callback = self._subscribers.get(sub_id)
            if callback:
                try:
                    callback(payload)
                except Exception:
                    logger.warning(
                        "[PUBSUB] Subscriber %s failed",
                        sub_id,
                        exc_info=True,
                    )

    def get_stats(self) -> dict[str, Any]:
        """Get pub/sub statistics."""
        return {
            "enabled": self._enabled,
            "published": self._published,
            "received": self._received,
            "publish_errors": self._publish_errors,
            "subscriber_count": len(self._subscribers),
        }
