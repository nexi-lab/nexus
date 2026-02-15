"""Scheduler service protocol (Issue #1383).

Defines the contract for agent work-request scheduling.
No existing production implementation — ``InMemoryScheduler`` is provided
as a test stub.

Storage Affinity: **CacheStore** — ephemeral work queue (Dragonfly sorted set).

References:
    - docs/design/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
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
    """Service contract for agent work-request scheduling.

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
    """Priority-aware scheduler backed by a sorted list.

    Intended for testing and development only — not production-grade.

    Scheduling policy:
      - Higher ``priority`` values are scheduled first.
      - Equal-priority requests are served in FIFO (submission) order.

    This uses a monotonic counter (``_seq``) as a tiebreaker so that
    equal-priority requests are dequeued in insertion order.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[int, int, AgentRequest]] = []  # (-priority, seq, request)
        self._seq: int = 0

    async def submit(self, request: AgentRequest) -> None:
        import heapq

        heapq.heappush(self._pending, (-request.priority, self._seq, request))
        self._seq += 1

    async def next(self) -> AgentRequest | None:
        import heapq

        if not self._pending:
            return None
        _, _, request = heapq.heappop(self._pending)
        return request

    async def pending_count(self, *, zone_id: str | None = None) -> int:
        if zone_id is None:
            return len(self._pending)
        return sum(1 for _, _, r in self._pending if r.zone_id == zone_id)

    async def cancel(self, agent_id: str) -> int:
        import heapq

        before = len(self._pending)
        self._pending = [(p, s, r) for p, s, r in self._pending if r.agent_id != agent_id]
        heapq.heapify(self._pending)  # Re-establish heap invariant after filtering
        return before - len(self._pending)
