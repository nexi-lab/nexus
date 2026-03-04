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
    NatsEventBus,
    RedisEventBus,
)
from nexus.system_services.event_subsystem.log import (
    EventReplayService,
)
from nexus.system_services.event_subsystem.subscriptions import ReactiveSubscriptionManager

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
