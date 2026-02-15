"""Event log service — durable event persistence for the event delivery pipeline.

NOT a kernel pillar ABC. EventLog is the durability backend for EventBus,
analogous to journald in Linux (user-space log persistence, not a syscall).

The Four Pillars (MetastoreABC, RecordStoreABC, ObjectStoreABC, CacheStoreABC)
remain the only kernel storage abstractions. EventLog is a service-layer concern.

Architecture:
    EventBus.publish(event)
        ├─ event_log.append(event)   # WAL-first durability (if available)
        └─ redis.publish(event)      # Dragonfly fan-out to subscribers

    EventDeliveryWorker (Issue #1241):
        operation_log WHERE delivered = FALSE
        → build FileEvent → dispatch → mark delivered = TRUE

Tracked by: #1397, #1241
"""

from nexus.services.event_log.factory import create_event_log
from nexus.services.event_log.protocol import EventLogConfig, EventLogProtocol

__all__ = [
    "EventLogConfig",
    "EventLogProtocol",
    "create_event_log",
]
