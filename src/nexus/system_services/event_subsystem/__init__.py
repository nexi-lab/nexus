"""Unified event subsystem — EventBus (pub/sub) + EventLog (delivery/replay).

Public API:
- FileEvent, FileEventType (types)
- EventBusProtocol, RedisEventBus, NatsEventBus (bus)
- EventReplayService (log)
- ReactiveSubscriptionManager (subscriptions)
"""

from nexus.core.file_events import FileEvent, FileEventType
from nexus.system_services.event_subsystem.bus import (
    EventBusBase,
    EventBusProtocol,
    RedisEventBus,
)
from nexus.system_services.event_subsystem.log import (
    EventReplayService,
)
from nexus.system_services.event_subsystem.subscriptions import ReactiveSubscriptionManager


def __getattr__(name: str) -> type:
    if name == "NatsEventBus":
        from nexus.system_services.event_subsystem.bus.nats import NatsEventBus

        return NatsEventBus
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FileEvent",
    "FileEventType",
    "EventBusProtocol",
    "EventBusBase",
    "RedisEventBus",
    "NatsEventBus",
    "EventReplayService",
    "ReactiveSubscriptionManager",
]
