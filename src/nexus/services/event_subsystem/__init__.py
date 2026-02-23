"""Unified event subsystem — EventBus (pub/sub) + EventLog (persistence).

Public API:
- FileEvent, FileEventType (types)
- EventBusProtocol, RedisEventBus, NatsEventBus (bus)
- EventLogProtocol, WALEventLog (log)
- ReactiveSubscriptionManager (subscriptions)
"""

from nexus.core.file_events import FileEvent, FileEventType
from nexus.services.event_subsystem.bus import (
    EventBusBase,
    EventBusProtocol,
    NatsEventBus,
    RedisEventBus,
)
from nexus.services.event_subsystem.log import (
    EventLogProtocol,
    EventReplayService,
    WALEventLog,
)
from nexus.services.event_subsystem.subscriptions import ReactiveSubscriptionManager

__all__ = [
    "FileEvent",
    "FileEventType",
    "EventBusProtocol",
    "EventBusBase",
    "RedisEventBus",
    "NatsEventBus",
    "EventLogProtocol",
    "WALEventLog",
    "EventReplayService",
    "ReactiveSubscriptionManager",
]
