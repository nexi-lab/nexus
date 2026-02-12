"""Event log service protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for persistent audit-trail event storage.
This is SEPARATE from ``EventBusProtocol`` (real-time pub/sub in CacheStore).
EventLog is append-only durable history; EventBus is ephemeral pub/sub.

No existing implementation — this is a new protocol.

Storage Affinity: **RecordStore** — append-only BRIN audit log (PostgreSQL).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class EventId:
    """Opaque identifier returned when an event is appended.

    Attributes:
        id: Unique event identifier (e.g. UUID).
        sequence: Monotonically-increasing sequence number for ordering.
    """

    id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class KernelEvent:
    """A kernel-level audit event.

    Attributes:
        type: Event type string (e.g. "file_write", "agent_connected").
        source: Component that emitted the event (e.g. "vfs_router").
        zone_id: Zone/organization ID for multi-zone isolation.
        timestamp: ISO-8601 timestamp string.
        event_id: Unique event identifier.
        payload: Arbitrary event-specific data.
    """

    type: str
    source: str
    zone_id: str | None
    timestamp: str
    event_id: str
    payload: dict[str, Any]


@runtime_checkable
class EventLogProtocol(Protocol):
    """Service contract for persistent event storage (audit trail).

    All methods are async.  No existing implementation — this protocol
    defines the target interface for future EventLog bricks.
    """

    async def append(self, event: KernelEvent) -> EventId: ...

    async def read(
        self,
        *,
        since_sequence: int = 0,
        limit: int = 100,
        zone_id: str | None = None,
    ) -> list[KernelEvent]: ...
