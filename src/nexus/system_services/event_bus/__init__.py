"""EventBus implementations — pub/sub event distribution."""

from nexus.system_services.event_bus.base import EventBusBase
from nexus.system_services.event_bus.protocol import AckableEvent, EventBusProtocol
from nexus.system_services.event_bus.redis import RedisEventBus


def __getattr__(name: str) -> type:
    if name == "NatsEventBus":
        from nexus.system_services.event_bus.nats import NatsEventBus

        return NatsEventBus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["EventBusProtocol", "AckableEvent", "EventBusBase", "RedisEventBus", "NatsEventBus"]
