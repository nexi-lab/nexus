"""Service-layer event types (re-exported from core).

This module re-exports FileEvent and FileEventType from nexus.core.file_events
for backward compatibility. Services should import from here, but the canonical
definitions live in the kernel tier (core/).

Per NEXUS-LEGO-ARCHITECTURE:
- Kernel tier (core/) defines FileEvent/FileEventType as kernel data types
- Services tier (services/event_subsystem/) provides EventBus and EventLog implementations
- Both tiers can use the same data types (upward import is allowed)

Service-layer protocols and implementations (EventBusProtocol, EventBusBase,
RedisEventBus, NatsEventBus) live in nexus.services.event_subsystem.bus.
"""

from nexus.core.file_events import FileEvent, FileEventType

__all__ = ["FileEvent", "FileEventType"]
