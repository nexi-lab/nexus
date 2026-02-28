"""System-service event types (re-exported from core).

Re-exports FileEvent and FileEventType from nexus.core.file_events.
The canonical definitions live in the kernel tier (core/).

Per NEXUS-LEGO-ARCHITECTURE §2.4:
- Kernel tier (core/) defines FileEvent/FileEventType as kernel data types
- System services tier provides EventBus and EventLog implementations
- Both tiers can use the same data types (upward import is allowed)

Protocols and implementations live in nexus.system_services.event_subsystem.bus.
"""

import json

from nexus.core.file_events import FileEvent, FileEventType

__all__ = ["FileEvent", "FileEventType", "serialize_event"]


def serialize_event(event: FileEvent) -> bytes:
    """Serialize a FileEvent to UTF-8 JSON bytes.

    Shared helper replacing 5+ duplicate ``json.dumps(event.to_dict()).encode()``
    patterns across exporters and delivery workers.
    """
    return json.dumps(event.to_dict()).encode("utf-8")
