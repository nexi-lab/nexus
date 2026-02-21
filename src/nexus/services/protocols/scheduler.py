"""Scheduler service protocol (Issue #1383, #1274, #2360).

Defines the contract for agent work-request scheduling.
``InMemoryScheduler`` is provided as a lightweight fallback for
deployments without PostgreSQL (edge/lite profiles).

Also defines ``CreditsReservationProtocol`` — the narrow interface
the scheduler uses for credit-based priority boosting, decoupling
the system service tier from the pay brick (LEGO §3.3).

``classify_agent_request()`` is the single source of truth for
AgentRequest → PriorityClass conversion, shared by both
SchedulerService and InMemoryScheduler (DRY).

Storage Affinity: **CacheStore** — ephemeral work queue (Dragonfly sorted set).

References:
    - docs/architecture/KERNEL-ARCHITECTURE.md §3
    - docs/architecture/data-storage-matrix.md (Four Pillars)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §2.4, §4.2
    - Issue #1383: Define 6 kernel protocol interfaces
    - Issue #1274: Astraea-style state-aware scheduler
    - Issue #2360: Promote scheduler to always-started system service
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Maximum completed tasks retained by InMemoryScheduler to prevent
# unbounded memory growth in long-running edge deployments.
_MAX_COMPLETED = 10_000


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
    idempotency_key: str | None = None


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
# classify_agent_request — shared classification helper (DRY)
# ---------------------------------------------------------------------------


def classify_agent_request(request: AgentRequest) -> str:
    """Classify an AgentRequest into a PriorityClass string.

    Single source of truth for AgentRequest → PriorityClass conversion.
    Delegates to ``classify_request()`` from the scheduler policies module,
    handling AgentRequest field parsing (tier, request_state) in one place.

    Used by both ``InMemoryScheduler.classify()`` and
    ``SchedulerService.classify()`` to avoid duplicated logic.
    """
    from nexus.services.scheduler.constants import PriorityTier, RequestState
    from nexus.services.scheduler.policies.classifier import classify_request

    try:
        tier = PriorityTier(request.priority)
    except ValueError:
        tier = PriorityTier.NORMAL

    try:
        req_state = RequestState(request.request_state)
    except ValueError:
        req_state = RequestState.PENDING

    return classify_request(tier, req_state)


# ---------------------------------------------------------------------------
# CreditsReservationProtocol — narrow interface for pay brick decoupling
# ---------------------------------------------------------------------------


@runtime_checkable
class CreditsReservationProtocol(Protocol):
    """Narrow credit-reservation interface used by the scheduler.

    Decouples SchedulerService (system service) from CreditsService (pay brick)
    per LEGO §3.3: "A brick MUST NOT import from other bricks."

    Only the two methods the scheduler actually uses are exposed:
    ``reserve()`` for priority-boost escrow and ``release_reservation()``
    for cancellation refunds.
    """

    async def reserve(
        self,
        agent_id: str,
        amount: Decimal,
        timeout_seconds: int = 300,
        *,
        zone_id: str = "",
    ) -> str:
        """Reserve credits for a priority boost.

        Returns:
            Reservation ID string.
        """
        ...

    async def release_reservation(self, reservation_id: str) -> None:
        """Void a pending reservation (full refund)."""
        ...


class NullCreditsReservation:
    """No-op credits stub for deployments without the pay brick.

    Structurally satisfies ``CreditsReservationProtocol``.
    All operations succeed silently with placeholder values.
    """

    async def reserve(
        self,
        agent_id: str,  # noqa: ARG002
        amount: Decimal,  # noqa: ARG002
        timeout_seconds: int = 300,  # noqa: ARG002
        *,
        zone_id: str = "",  # noqa: ARG002
    ) -> str:
        return "null-reservation"

    async def release_reservation(self, reservation_id: str) -> None:  # noqa: ARG002
        pass


# ---------------------------------------------------------------------------
# InMemoryScheduler — lightweight fallback for non-PostgreSQL deployments
# ---------------------------------------------------------------------------


class InMemoryScheduler:
    """Priority-aware scheduler backed by a sorted list.

    Used as a lightweight fallback for deployments without PostgreSQL
    (edge/lite profiles) and for testing.  Tasks are NOT persisted —
    they will be lost on restart.

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
            return {"id": task_id, "status": "queued", "agent_id": req.agent_id}
        return None

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "completed"
        self._completed[task_id] = {
            "id": task_id,
            "status": status,
            "error": error,
        }
        self._task_map.pop(task_id, None)

        # Evict oldest entries to prevent unbounded memory growth
        if len(self._completed) > _MAX_COMPLETED:
            oldest = next(iter(self._completed))
            del self._completed[oldest]

    async def classify(self, request: AgentRequest) -> str:
        return classify_agent_request(request)

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]:
        count = await self.pending_count(zone_id=zone_id)
        return {
            "pending_count": count,
            "completed_count": len(self._completed),
            "queue_by_class": [],
            "fair_share": {},
            "use_hrrn": False,
        }

    # -----------------------------------------------------------------------
    # Lifecycle — no-ops for InMemoryScheduler
    # -----------------------------------------------------------------------

    async def initialize(self, dsn: str = "", **kwargs: Any) -> None:  # noqa: ARG002
        """No-op — in-memory scheduler requires no external resources."""

    async def shutdown(self) -> None:
        """Clear all queues on shutdown."""
        self._pending.clear()
        self._completed.clear()
        self._task_map.clear()

    async def sync_fair_share(self) -> None:
        """No-op — in-memory scheduler has no persistent fair-share state."""

    async def run_aging_sweep(self) -> int:
        """No-op — in-memory scheduler has no aging mechanism."""
        return 0

    async def run_starvation_promotion(self, threshold_seconds: float = 300.0) -> int:  # noqa: ARG002
        """No-op — in-memory scheduler has no starvation promotion."""
        return 0
