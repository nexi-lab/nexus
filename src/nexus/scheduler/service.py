"""Scheduler service - orchestrates priority computation, queue, and credits.

The SchedulerService is the main entry point for task scheduling. It:
1. Validates submissions
2. Computes priority (4-layer model)
3. Reserves credits for boosts
4. Enqueues tasks in PostgreSQL
5. Provides status, cancellation, and aging sweep
6. Implements SchedulerProtocol (8-method interface, Issue #1274)
7. Astraea-style classification, HRRN dequeue, and fair-share

Related: Issue #1212, #1274
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.scheduler.constants import (
    STARVATION_PROMOTION_THRESHOLD_SECS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    PriorityClass,
    PriorityTier,
    RequestState,
)
from nexus.scheduler.models import ScheduledTask, TaskSubmission
from nexus.scheduler.policies.classifier import classify_request
from nexus.scheduler.policies.fair_share import FairShareCounter
from nexus.scheduler.priority import (
    compute_boost_tiers,
    compute_effective_tier,
    validate_submission,
)
from nexus.scheduler.queue import TaskQueue

if TYPE_CHECKING:
    from nexus.pay.credits import CreditsService
    from nexus.scheduler.events import AgentStateEmitter, AgentStateEvent
    from nexus.services.protocols.scheduler import AgentRequest

logger = logging.getLogger(__name__)


class SchedulerService:
    """High-level scheduler service implementing SchedulerProtocol.

    Orchestrates priority computation, queue operations, credits
    integration, Astraea classification, HRRN dequeue, and fair-share
    admission for the hybrid priority system.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue | None = None,
        db_pool: Any = None,
        credits_service: CreditsService | None = None,
        state_emitter: AgentStateEmitter | None = None,
        fair_share: FairShareCounter | None = None,
        use_hrrn: bool = True,
    ) -> None:
        self._queue = queue or TaskQueue()
        self._pool = db_pool
        self._credits = credits_service
        self._fair_share = fair_share or FairShareCounter()
        self._use_hrrn = use_hrrn

        # Register for agent state events if emitter is provided
        if state_emitter is not None:
            state_emitter.add_handler(self._on_agent_state_change)

    # =========================================================================
    # SchedulerProtocol — 8 methods
    # =========================================================================

    async def submit(self, request: AgentRequest) -> str:
        """Submit an AgentRequest, auto-classify, and enqueue.

        Returns:
            Task ID string.
        """
        # Map AgentRequest fields to TaskSubmission
        try:
            tier = PriorityTier(request.priority)
        except ValueError:
            tier = PriorityTier.NORMAL

        try:
            req_state = RequestState(request.request_state)
        except ValueError:
            req_state = RequestState.PENDING

        priority_class_str = request.priority_class
        if not priority_class_str or priority_class_str == "batch":
            # Auto-classify
            priority_class_str = classify_request(tier, req_state)

        submission = TaskSubmission(
            agent_id=request.agent_id,
            executor_id=request.executor_id or request.agent_id,
            task_type=request.task_type or "default",
            payload=request.payload,
            priority=tier,
            deadline=None,
            boost_amount=Decimal(request.boost_amount),
            request_state=req_state,
            priority_class=PriorityClass(priority_class_str),
            estimated_service_time=request.estimated_service_time,
        )

        task = await self.submit_task(submission)
        return task.id

    async def next(self, *, executor_id: str | None = None) -> AgentRequest | None:  # noqa: ARG002
        """Dequeue the next task and return as AgentRequest."""
        task = await self.dequeue_next()
        if task is None:
            return None

        from nexus.services.protocols.scheduler import AgentRequest

        return AgentRequest(
            agent_id=task.agent_id,
            zone_id=task.zone_id,
            priority=task.priority_tier.value,
            submitted_at=task.enqueued_at.isoformat() if task.enqueued_at else "",
            payload={**task.payload, "_task_id": task.id},
            executor_id=task.executor_id,
            task_type=task.task_type,
            request_state=task.request_state,
            priority_class=task.priority_class,
            estimated_service_time=task.estimated_service_time,
        )

    async def get_status(self, task_id: str) -> dict[str, Any] | None:
        """Get task status by ID as a dict."""
        async with self._pool.acquire() as conn:
            task = await self._queue.get_task(conn, task_id)
        if task is None:
            return None
        return {
            "task_id": task.id,
            "status": task.status,
            "agent_id": task.agent_id,
            "executor_id": task.executor_id,
            "task_type": task.task_type,
            "priority_tier": task.priority_tier.value,
            "effective_tier": task.effective_tier,
            "priority_class": task.priority_class,
            "request_state": task.request_state,
            "enqueued_at": task.enqueued_at.isoformat() if task.enqueued_at else "",
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "error_message": task.error_message,
        }

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        """Mark a task as completed or failed, and update fair-share counter."""
        status = TASK_STATUS_FAILED if error else TASK_STATUS_COMPLETED
        async with self._pool.acquire() as conn:
            # Look up the task to get agent_id for fair-share
            task = await self._queue.get_task(conn, task_id)
            await self._queue.complete(conn, task_id, status=status, error=error)

        if task is not None:
            self._fair_share.record_complete(task.agent_id)

    async def classify(self, request: AgentRequest) -> str:
        """Classify an AgentRequest into a PriorityClass."""
        try:
            tier = PriorityTier(request.priority)
        except ValueError:
            tier = PriorityTier.NORMAL

        try:
            req_state = RequestState(request.request_state)
        except ValueError:
            req_state = RequestState.PENDING

        return classify_request(tier, req_state)

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]:
        """Get scheduler metrics including queue stats and fair-share."""
        async with self._pool.acquire() as conn:
            queue_metrics = await self._queue.get_queue_metrics(conn, zone_id=zone_id)

        return {
            "queue_by_class": queue_metrics,
            "fair_share": {
                agent_id: {
                    "running_count": snap.running_count,
                    "max_concurrent": snap.max_concurrent,
                    "available_slots": snap.available_slots,
                }
                for agent_id, snap in self._fair_share.all_snapshots().items()
            },
            "use_hrrn": self._use_hrrn,
        }

    async def pending_count(self, *, zone_id: str | None = None) -> int:
        """Count pending tasks. Placeholder for protocol conformance."""
        m = await self.metrics(zone_id=zone_id)
        return sum(row.get("cnt", 0) for row in m.get("queue_by_class", []))

    async def cancel(self, agent_id: str) -> int:
        """Cancel all queued tasks for an agent. Returns count cancelled."""
        async with self._pool.acquire() as conn:
            return await self._queue.cancel_by_agent(conn, agent_id)

    # =========================================================================
    # Original service methods (backward compatible)
    # =========================================================================

    async def submit_task(self, submission: TaskSubmission) -> ScheduledTask:
        """Submit a task for scheduling.

        1. Validates the submission
        2. If boost > 0, reserves credits
        3. Auto-classifies priority_class if not provided
        4. Checks fair-share admission
        5. Computes effective tier
        6. Enqueues in PostgreSQL
        7. Returns the scheduled task
        """
        validate_submission(submission)

        now = datetime.now(UTC)

        # Auto-classify priority_class if not provided
        priority_class = submission.priority_class
        if priority_class is None:
            priority_class = PriorityClass(
                classify_request(submission.priority, submission.request_state)
            )

        # Check fair-share admission
        executor_id = submission.executor_id
        if not self._fair_share.admit(executor_id):
            raise ValueError(
                f"Agent {executor_id} is at capacity "
                f"({self._fair_share.snapshot(executor_id).running_count}/"
                f"{self._fair_share.snapshot(executor_id).max_concurrent})"
            )

        # Reserve credits for boost if needed
        boost_tiers = compute_boost_tiers(submission.boost_amount)
        boost_reservation_id: str | None = None

        if submission.boost_amount > 0 and self._credits is not None:
            boost_reservation_id = await self._credits.reserve(
                agent_id=submission.agent_id,
                amount=submission.boost_amount,
                timeout_seconds=3600,
            )

        # Compute effective tier
        effective_tier = compute_effective_tier(submission, enqueued_at=now, now=now)

        # Enqueue with Astraea fields
        async with self._pool.acquire() as conn:
            task_id = await self._queue.enqueue(
                conn,
                agent_id=submission.agent_id,
                executor_id=submission.executor_id,
                task_type=submission.task_type,
                payload=submission.payload,
                priority_tier=submission.priority.value,
                effective_tier=effective_tier,
                deadline=submission.deadline,
                boost_amount=submission.boost_amount,
                boost_tiers=boost_tiers,
                boost_reservation_id=boost_reservation_id,
                idempotency_key=submission.idempotency_key,
                request_state=submission.request_state.value,
                priority_class=priority_class.value,
                estimated_service_time=submission.estimated_service_time,
            )

        return ScheduledTask(
            id=task_id,
            agent_id=submission.agent_id,
            executor_id=submission.executor_id,
            task_type=submission.task_type,
            payload=submission.payload,
            priority_tier=submission.priority,
            effective_tier=effective_tier,
            enqueued_at=now,
            status=TASK_STATUS_QUEUED,
            deadline=submission.deadline,
            boost_amount=submission.boost_amount,
            boost_tiers=boost_tiers,
            boost_reservation_id=boost_reservation_id,
            idempotency_key=submission.idempotency_key,
            request_state=submission.request_state.value,
            priority_class=priority_class.value,
            estimated_service_time=submission.estimated_service_time,
        )

    async def get_task_status(self, task_id: str) -> ScheduledTask | None:
        """Get task as ScheduledTask (for backward compat with router)."""
        async with self._pool.acquire() as conn:
            return await self._queue.get_task(conn, task_id)

    async def cancel_task(self, task_id: str, agent_id: str = "") -> bool:  # noqa: ARG002
        """Cancel a queued task with credit release."""
        async with self._pool.acquire() as conn:
            task = await self._queue.get_task(conn, task_id)
            cancelled = await self._queue.cancel(conn, task_id)

            if cancelled and task and task.boost_reservation_id and self._credits:
                await self._credits.release_reservation(task.boost_reservation_id)

            return cancelled

    async def dequeue_next(self) -> ScheduledTask | None:
        """Dequeue the highest-priority task for execution.

        Uses HRRN dequeue when enabled, falls back to classic ordering.
        Updates fair-share counter on successful dequeue.
        """
        async with self._pool.acquire() as conn:
            if self._use_hrrn:
                task = await self._queue.dequeue_hrrn(conn)
            else:
                task = await self._queue.dequeue(conn)

        if task is not None:
            self._fair_share.record_start(task.agent_id)

        return task

    async def run_aging_sweep(self) -> int:
        """Run one aging sweep cycle."""
        now = datetime.now(UTC)
        async with self._pool.acquire() as conn:
            return await self._queue.aging_sweep(conn, now)

    # =========================================================================
    # Astraea internal methods (Issue #1274)
    # =========================================================================

    async def _on_agent_state_change(self, event: AgentStateEvent) -> None:
        """Handle agent state transitions — update executor_state in DB."""
        logger.info(
            "Agent state change: %s %s -> %s",
            event.agent_id,
            event.previous_state,
            event.new_state,
        )
        async with self._pool.acquire() as conn:
            await self._queue.update_executor_state(conn, event.agent_id, event.new_state)

    async def sync_fair_share(self) -> None:
        """Initialize fair-share counters from database on startup."""
        async with self._pool.acquire() as conn:
            running_counts = await self._queue.count_running_by_agent(conn)
        self._fair_share.sync_from_db(running_counts)
        logger.info("Fair-share synced from DB: %d agents", len(running_counts))

    async def run_starvation_promotion(
        self,
        threshold_seconds: float = STARVATION_PROMOTION_THRESHOLD_SECS,
    ) -> int:
        """Promote starved BACKGROUND tasks to BATCH."""
        async with self._pool.acquire() as conn:
            count = await self._queue.promote_starved(conn, threshold_seconds)
        if count > 0:
            logger.info("Starvation promotion: %d tasks promoted", count)
        return count
