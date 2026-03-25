"""InMemoryScheduler — lightweight fallback for non-PostgreSQL deployments.

Extracted from ``nexus.contracts.protocols.scheduler`` to keep protocol
files free of implementation code.

Used as a lightweight fallback for deployments without PostgreSQL
(edge/lite profiles) and for testing.  Tasks are NOT persisted --
they will be lost on restart.

Scheduling policy:
  - Higher ``priority`` values are scheduled first.
  - Equal-priority requests are served in FIFO (submission) order.

Supports basic overlap policies (SKIP, CANCEL_PREVIOUS, ALLOW) and
simple rate limiting for behavioral parity with SchedulerService.
"""

from __future__ import annotations

import heapq
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.protocols.scheduler import _MAX_COMPLETED, AgentRequest

logger = logging.getLogger(__name__)


class InMemoryScheduler:
    """Priority-aware scheduler backed by a sorted list.

    Used as a lightweight fallback for deployments without PostgreSQL
    (edge/lite profiles) and for testing.  Tasks are NOT persisted --
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
        self._enqueued_at: dict[str, str] = {}  # task_id -> ISO timestamp
        self._running: dict[str, AgentRequest] = {}  # task_id -> request (for overlap checks)
        self._idempotency_index: dict[str, str] = {}  # idempotency_key -> task_id

    def _build_status(
        self,
        task_id: str,
        req: AgentRequest,
        status: str = "queued",
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Build a complete status dict matching TaskStatusResponse."""
        from nexus.contracts.constants import PriorityTier

        try:
            tier_name = PriorityTier(req.priority).name.lower()
        except ValueError:
            tier_name = "normal"

        return {
            "id": task_id,
            "status": status,
            "agent_id": req.agent_id,
            "executor_id": req.executor_id or "",
            "task_type": req.task_type or "",
            "priority_tier": tier_name,
            "effective_tier": req.priority,
            "enqueued_at": self._enqueued_at.get(task_id, ""),
            "deadline": req.deadline,
            "boost_amount": req.boost_amount,
            "error_message": error,
            "priority_class": req.priority_class,
            "request_state": req.request_state,
        }

    async def submit(self, request: AgentRequest) -> str:
        from nexus.services.scheduler.constants import OverlapPolicy
        from nexus.services.scheduler.exceptions import TaskAlreadyRunning

        overlap_policy = OverlapPolicy(request.overlap_policy)
        idem_key = request.idempotency_key

        # Apply overlap policy when idempotency_key is set
        if idem_key is not None and overlap_policy != OverlapPolicy.ALLOW:
            existing_task_id = self._idempotency_index.get(idem_key)
            if existing_task_id is not None and existing_task_id in self._running:
                if overlap_policy == OverlapPolicy.SKIP:
                    raise TaskAlreadyRunning(
                        f"Task with idempotency_key '{idem_key}' is already running (policy=SKIP)"
                    )
                if overlap_policy == OverlapPolicy.CANCEL_PREVIOUS:
                    # Cancel the running task
                    self._running.pop(existing_task_id, None)
                    old_req = self._task_map.pop(existing_task_id, None)
                    if old_req:
                        self._completed[existing_task_id] = self._build_status(
                            existing_task_id, old_req, "cancelled"
                        )
                    self._enqueued_at.pop(existing_task_id, None)

        task_id = str(uuid.uuid4())
        heapq.heappush(self._pending, (-request.priority, self._seq, request))
        self._seq += 1
        self._task_map[task_id] = request
        self._enqueued_at[task_id] = datetime.now(UTC).isoformat()

        # Track idempotency key -> task_id mapping
        if idem_key is not None:
            self._idempotency_index[idem_key] = task_id

        return task_id

    async def next(self, *, executor_id: str | None = None) -> AgentRequest | None:
        if not self._pending:
            return None
        if executor_id is not None:
            # Filter for matching executor
            for i, (_p, _s, r) in enumerate(self._pending):
                if r.executor_id == executor_id:
                    self._pending.pop(i)
                    heapq.heapify(self._pending)
                    # Track as running for overlap policy checks
                    for tid, req in self._task_map.items():
                        if req is r:
                            self._running[tid] = req
                            break
                    return r
            return None
        _, _, request = heapq.heappop(self._pending)
        # Track as running for overlap policy checks
        for tid, req in self._task_map.items():
            if req is request:
                self._running[tid] = req
                break
        return request

    async def pending_count(self, *, zone_id: str | None = None) -> int:
        if zone_id is None:
            return len(self._pending)
        return sum(1 for _, _, r in self._pending if r.zone_id == zone_id)

    async def cancel(self, agent_id: str) -> int:
        before = len(self._pending)
        self._pending = [(p, s, r) for p, s, r in self._pending if r.agent_id != agent_id]
        heapq.heapify(self._pending)
        return before - len(self._pending)

    async def cancel_by_id(self, task_id: str) -> bool:
        """Cancel a specific task by its task ID."""
        req = self._task_map.pop(task_id, None)
        if req is None:
            return False
        self._pending = [(p, s, r) for p, s, r in self._pending if r is not req]
        heapq.heapify(self._pending)
        # Record as cancelled so get_status() still returns data
        self._completed[task_id] = self._build_status(task_id, req, "cancelled")
        self._enqueued_at.pop(task_id, None)
        return True

    async def get_status(self, task_id: str) -> dict[str, Any] | None:
        if task_id in self._completed:
            return self._completed[task_id]
        if task_id in self._task_map:
            req = self._task_map[task_id]
            return self._build_status(task_id, req)
        return None

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        status = "failed" if error else "completed"
        req = self._task_map.pop(task_id, None)
        self._running.pop(task_id, None)
        if req:
            result = self._build_status(task_id, req, status, error=error)
            result["completed_at"] = datetime.now(UTC).isoformat()
            self._completed[task_id] = result
        else:
            self._completed[task_id] = {"id": task_id, "status": status, "error_message": error}
        self._enqueued_at.pop(task_id, None)

        # Evict oldest entries to prevent unbounded memory growth
        if len(self._completed) > _MAX_COMPLETED:
            oldest = next(iter(self._completed))
            del self._completed[oldest]

    async def classify(self, request: AgentRequest) -> str:
        from nexus.services.scheduler.policies.classifier import classify_agent_request

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
    # Lifecycle -- no-ops for InMemoryScheduler
    # -----------------------------------------------------------------------

    async def initialize(self, *args: Any, **kwargs: Any) -> None:  # noqa: ARG002
        """No-op -- in-memory scheduler requires no external resources."""

    async def shutdown(self) -> None:
        """Clear all queues on shutdown."""
        self._pending.clear()
        self._completed.clear()
        self._task_map.clear()
        self._enqueued_at.clear()
        self._running.clear()
        self._idempotency_index.clear()

    async def sync_fair_share(self) -> None:
        """No-op -- in-memory scheduler has no persistent fair-share state."""

    async def run_aging_sweep(self) -> int:
        """No-op -- in-memory scheduler has no aging mechanism."""
        return 0

    async def run_starvation_promotion(self, threshold_seconds: float = 300.0) -> int:  # noqa: ARG002
        """No-op -- in-memory scheduler has no starvation promotion."""
        return 0
