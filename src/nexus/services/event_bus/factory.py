"""EventBus factory — self-contained lib that resolves its own dependencies.

EventBus is a lib, not a service. Callers create it with ``create_event_bus()``
which resolves backend type, connection URLs, and optional stores internally.
No external dependency injection needed.
"""

import logging
from typing import Any

from nexus.services.event_bus.base import EventBusBase

logger = logging.getLogger(__name__)


def _resolve_optional_stores(**overrides: Any) -> dict[str, Any]:
    """Try to resolve optional stores (record_store, settings_store).

    These are nice-to-haves for SSOT sync and checkpoint persistence.
    EventBus works without them.
    """
    kwargs: dict[str, Any] = {}

    # record_store — for startup_sync (PG SSOT)
    if "record_store" in overrides:
        kwargs["record_store"] = overrides["record_store"]

    # settings_store — for checkpoint persistence
    if "settings_store" in overrides:
        kwargs["settings_store"] = overrides["settings_store"]

    # node_id override
    if "node_id" in overrides:
        kwargs["node_id"] = overrides["node_id"]

    return kwargs


def create_event_bus(
    backend: str | None = None,
    **overrides: Any,
) -> EventBusBase:
    """Create an event bus instance. Resolves dependencies internally.

    Backend auto-detected from DistributedConfig (default: "redis").
    Connection URLs resolved from config defaults and env helpers.
    Optional stores (record_store, settings_store) resolved if available.

    Args:
        backend: Override backend type ("redis" or "nats"). Auto-detected if None.
        **overrides: Optional overrides (nats_url, url, record_store, settings_store, node_id).

    Returns:
        EventBusBase implementation.

    Raises:
        ValueError: If no backend URL is available.
    """
    from nexus.contracts.constants import DEFAULT_NATS_URL
    from nexus.core.config import DistributedConfig

    _backend = backend or DistributedConfig().event_bus_backend
    _optional = _resolve_optional_stores(**overrides)

    if _backend == "nats":
        from nexus.services.event_bus.nats import NatsEventBus

        nats_url = overrides.get("nats_url") or DEFAULT_NATS_URL
        logger.info("EventBus: creating NatsEventBus (url=%s)", nats_url)
        return NatsEventBus(nats_url=nats_url, **_optional)

    # Redis/Dragonfly
    from nexus.lib.env import get_dragonfly_url, get_redis_url

    url = overrides.get("url") or get_redis_url() or get_dragonfly_url()
    if url:
        from nexus.cache.dragonfly import DragonflyClient
        from nexus.services.event_bus.redis import RedisEventBus

        client = DragonflyClient(url=url)
        logger.info("EventBus: creating RedisEventBus (url=%s)", url)
        return RedisEventBus(client, **_optional)

    raise ValueError(
        "No event bus backend available. "
        "Set NATS_URL or REDIS_URL/DRAGONFLY_URL, or configure event_bus_backend='nats'."
    )
