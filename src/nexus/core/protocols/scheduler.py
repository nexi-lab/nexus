"""Scheduler kernel protocol (Nexus Lego Architecture, Issue #1383).

Defines the contract for agent work-request scheduling.
No existing production implementation — ``InMemoryScheduler`` is provided
as a test stub.

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md Part 2
    - Issue #1383: Define 6 kernel protocol interfaces
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """A work request submitted to the scheduler.

    Attributes:
        agent_id: Target agent identifier.
        zone_id: Zone/organization ID for multi-zone isolation.
        priority: Scheduling priority (higher = more urgent).  Default 0.
        submitted_at: ISO-8601 timestamp of submission.
        payload: Arbitrary request-specific data.
    """

    agent_id: str
    zone_id: str | None
    priority: int = 0
    submitted_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SchedulerProtocol(Protocol):
    """Kernel contract for agent work-request scheduling.

    All methods are async.  No existing production implementation.
    """

    async def submit(self, request: AgentRequest) -> None: ...

    async def next(self) -> AgentRequest | None: ...

    async def pending_count(self, *, zone_id: str | None = None) -> int: ...

    async def cancel(self, agent_id: str) -> int: ...


# ---------------------------------------------------------------------------
# InMemoryScheduler — test / development stub
# ---------------------------------------------------------------------------


class InMemoryScheduler:
    """Simple FIFO scheduler backed by a list.

    Intended for testing and development only — not production-grade.
    """

    def __init__(self) -> None:
        self._pending: list[AgentRequest] = []

    async def submit(self, request: AgentRequest) -> None:
        self._pending.append(request)

    async def next(self) -> AgentRequest | None:
        if not self._pending:
            return None
        return self._pending.pop(0)

    async def pending_count(self, *, zone_id: str | None = None) -> int:
        if zone_id is None:
            return len(self._pending)
        return sum(1 for r in self._pending if r.zone_id == zone_id)

    async def cancel(self, agent_id: str) -> int:
        before = len(self._pending)
        self._pending = [r for r in self._pending if r.agent_id != agent_id]
        return before - len(self._pending)
