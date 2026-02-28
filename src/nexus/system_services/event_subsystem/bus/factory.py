"""EventBus factory — create event bus instances by backend name."""

import logging
from typing import Any

from nexus.system_services.event_subsystem.bus.base import EventBusBase
from nexus.system_services.event_subsystem.bus.protocol import PubSubClientProtocol
from nexus.system_services.event_subsystem.bus.redis import RedisEventBus

logger = logging.getLogger(__name__)


def create_event_bus(
    backend: str = "redis",
    redis_client: PubSubClientProtocol | None = None,
    nats_url: str | None = None,
    **kwargs: Any,
) -> EventBusBase:
    """Factory function to create an event bus instance.

    Args:
        backend: Backend type ("redis" or "nats")
        redis_client: DragonflyClient for Redis backend
        nats_url: NATS server URL for NATS backend
        **kwargs: Additional backend-specific arguments (record_store, node_id, etc.)

    Returns:
        EventBusBase implementation

    Raises:
        ValueError: If backend is not supported or required arguments are missing
    """
    if backend == "nats":
        if nats_url is None:
            raise ValueError("nats_url is required for NATS backend")
        from nexus.system_services.event_subsystem.bus.nats import NatsEventBus

        return NatsEventBus(nats_url=nats_url, **kwargs)

    if backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for Redis backend")
        return RedisEventBus(redis_client, **kwargs)

    raise ValueError(f"Unsupported event bus backend: {backend}")
