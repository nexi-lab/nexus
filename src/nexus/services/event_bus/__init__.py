"""EventBus implementations — pub/sub event distribution."""

from nexus.services.event_bus.base import EventBusBase
from nexus.services.event_bus.in_memory import InMemoryEventBus
from nexus.services.event_bus.protocol import AckableEvent, EventBusProtocol
from nexus.services.event_bus.redis import RedisEventBus


def __getattr__(name: str) -> type:
    if name == "NatsEventBus":
        from nexus.services.event_bus.nats import NatsEventBus

        return NatsEventBus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AckableEvent",
    "EventBusBase",
    "EventBusProtocol",
    "InMemoryEventBus",
    "NatsEventBus",
    "RedisEventBus",
]
