"""Event log kernel protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for persistent audit-trail event storage.
This is SEPARATE from ``EventBusProtocol`` (real-time pub/sub in
``nexus.core.event_bus``).  EventLog is for durable, queryable history.

No existing implementation — this is a new protocol.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from collections.abc import AsyncIterator
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
    """Kernel contract for persistent event storage (audit trail).

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

    async def subscribe(
        self,
        *,
        zone_id: str | None = None,
        pattern: str = "*",
    ) -> AsyncIterator[KernelEvent]: ...
