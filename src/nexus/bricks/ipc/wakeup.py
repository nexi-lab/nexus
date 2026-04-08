"""IPC EventPublisher implementation backed by CacheStore pub/sub.

Bridges the IPC brick's ``EventPublisher`` protocol to a ``CacheStoreABC``
backend (Dragonfly/Redis), enabling cross-node IPC event notifications
without requiring a separate EventBus service.

Note: Uses duck-typing (Any) for CacheStore to respect the LEGO brick
import boundary (bricks must not import from nexus.core directly).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class CacheStoreEventPublisher:
    """Bridges CacheStoreABC pub/sub to the IPC EventPublisher protocol.

    MessageSender uses EventPublisher.publish(channel, data) to notify
    recipients of new inbox messages.  This adapter serializes the event
    dict to JSON and publishes it via CacheStore (Dragonfly/Redis) pub/sub,
    enabling cross-node EventBus notifications without requiring a separate
    EventBus service for IPC.

    Satisfies the ``EventPublisher`` protocol from ``protocols.py``.
    """

    def __init__(self, cache_store: Any) -> None:
        self._cs = cache_store

    async def publish(self, channel: str, data: dict) -> None:
        """Publish an IPC event to a CacheStore pub/sub channel."""
        import json

        try:
            await self._cs.publish(channel, json.dumps(data).encode())
        except Exception:
            logger.debug(
                "CacheStore EventPublisher failed for channel %s (best-effort)",
                channel,
            )
