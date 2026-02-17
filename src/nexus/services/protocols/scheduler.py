"""Scheduler service protocol (Issue #1383, #1274).

Defines the contract for agent work-request scheduling.
``InMemoryScheduler`` is provided as a test stub.

Storage Affinity: **CacheStore** — ephemeral work queue (Dragonfly sorted set).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #1274: Astraea-style state-aware scheduler
"""

import uuid
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
        executor_id: Target executor agent (Astraea).
        task_type: Task type identifier for classification (Astraea).
        request_state: Current execution state for classification (Astraea).
        priority_class: Scheduling class: interactive/batch/background (Astraea).
        deadline: Optional ISO-8601 deadline (Astraea).
        boost_amount: Credits for priority boost as decimal string (Astraea).
        estimated_service_time: Estimated service time in seconds (Astraea/HRRN).
    """

    agent_id: str
    zone_id: str | None
    priority: int = 0
    submitted_at: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    # Astraea extensions (all optional for backward compat)
    executor_id: str | None = None
    task_type: str = ""
    request_state: str = "pending"
    priority_class: str = "batch"
    deadline: str | None = None
    boost_amount: str = "0"
    estimated_service_time: float = 30.0

@runtime_checkable
class SchedulerProtocol(Protocol):
    """Service contract for agent work-request scheduling.

    All methods are async. 8-method interface (Issue #1274).
    """

    async def submit(self, request: AgentRequest) -> str: ...

    async def next(self, *, executor_id: str | None = None) -> AgentRequest | None: ...

    async def pending_count(self, *, zone_id: str | None = None) -> int: ...

    async def cancel(self, agent_id: str) -> int: ...

    async def get_status(self, task_id: str) -> dict[str, Any] | None: ...

    async def complete(self, task_id: str, *, error: str | None = None) -> None: ...

    async def classify(self, request: AgentRequest) -> str: ...

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]: ...

# ---------------------------------------------------------------------------
# InMemoryScheduler — test / development stub
# ---------------------------------------------------------------------------

class InMemoryScheduler:
    """Priority-aware scheduler backed by a sorted list.

    Intended for testing and development only — not production-grade.

    Scheduling policy:
      - Higher ``priority`` values are scheduled first.
      - Equal-priority requests are served in FIFO (submission) order.
    """

    def __init__(self) -> None:
        self._pending: list[tuple[int, int, AgentRequest]] = []  # (-priority, seq, request)
        self._seq: int = 0
        self._completed: dict[str, dict[str, Any]] = {}
        self._task_map: dict[str, AgentRequest] = {}

    async def submit(self, request: AgentRequest) -> str:
        import heapq

        task_id = str(uuid.uuid4())
        heapq.heappush(self._pending, (-request.priority, self._seq, request))
        self._seq += 1
        self._task_map[task_id] = request
        return task_id

    async def next(self, *, executor_id: str | None = None) -> AgentRequest | None:
        import heapq

        if not self._pending:
            return None
        if executor_id is not None:
            # Filter for matching executor
            for i, (_p, _s, r) in enumerate(self._pending):
                if r.executor_id == executor_id:
                    self._pending.pop(i)
                    heapq.heapify(self._pending)
                    return r
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
        heapq.heapify(self._pending)
        return before - len(self._pending)

    async def get_status(self, task_id: str) -> dict[str, Any] | None:
        if task_id in self._completed:
            return self._completed[task_id]
        if task_id in self._task_map:
            req = self._task_map[task_id]
            return {"task_id": task_id, "status": "queued", "agent_id": req.agent_id}
        return None

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "completed"
        self._completed[task_id] = {
            "task_id": task_id,
            "status": status,
            "error": error,
        }
        self._task_map.pop(task_id, None)

    async def classify(self, request: AgentRequest) -> str:
        from nexus.scheduler.constants import (
            TIER_TO_CLASS,
            PriorityClass,
            PriorityTier,
            RequestState,
        )

        # Map integer priority to PriorityTier, default NORMAL
        try:
            tier = PriorityTier(request.priority)
        except ValueError:
            tier = PriorityTier.NORMAL
        base_class = TIER_TO_CLASS.get(tier, PriorityClass.BATCH)

        # IO promotion: BACKGROUND → BATCH if IO_WAIT
        if base_class == PriorityClass.BACKGROUND and request.request_state == RequestState.IO_WAIT:
            return PriorityClass.BATCH
        return base_class

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]:
        count = await self.pending_count(zone_id=zone_id)
        return {"pending_count": count, "completed_count": len(self._completed)}
