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

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.system_services.scheduler.constants import (
    STARVATION_PROMOTION_THRESHOLD_SECS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    PriorityClass,
    PriorityTier,
)
from nexus.system_services.scheduler.models import ScheduledTask, TaskSubmission
from nexus.system_services.scheduler.policies.classifier import (
    classify_agent_request,
    classify_request,
    parse_request_enums,
)
from nexus.system_services.scheduler.policies.fair_share import FairShareCounter
from nexus.system_services.scheduler.priority import (
    compute_boost_tiers,
    compute_effective_tier,
    validate_submission,
)
from nexus.system_services.scheduler.queue import TaskQueue

if TYPE_CHECKING:
    from nexus.contracts.protocols.scheduler import AgentRequest, CreditsReservationProtocol
    from nexus.system_services.scheduler.events import AgentStateEmitter, AgentStateEvent

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
        credits_service: "CreditsReservationProtocol | None" = None,
        state_emitter: "AgentStateEmitter | None" = None,
        fair_share: FairShareCounter | None = None,
        use_hrrn: bool = True,
    ) -> None:
        self._queue = queue or TaskQueue()
        self._pool = db_pool
        self._credits = credits_service
        self._fair_share = fair_share or FairShareCounter()
        self._use_hrrn = use_hrrn
        self._state_emitter = state_emitter

        # Two-phase init tracking (Issue #2195):
        # Factory creates with db_pool=None, lifespan calls initialize(pool).
        self._initialized = db_pool is not None

        # Register for agent state events if emitter is provided
        if state_emitter is not None:
            state_emitter.add_handler(self._on_agent_state_change)

    # =========================================================================
    # Two-phase initialization (Issue #2195)
    # =========================================================================

    @property
    def pool(self) -> Any:
        """Return the asyncpg pool, raising if not yet initialized."""
        if self._pool is None:
            raise RuntimeError(
                "SchedulerService.pool accessed before initialize(). "
                "Call `await scheduler.initialize(db_pool)` first."
            )
        return self._pool

    async def initialize(self, db_pool: Any) -> None:
        """Complete async initialization with the asyncpg pool.

        Called by lifespan after creating the asyncpg pool.
        """
        self._pool = db_pool
        self._initialized = True
        await self.sync_fair_share()
        logger.info("SchedulerService initialized (pool connected, fair-share synced)")

    async def shutdown(self) -> None:
        """Close the asyncpg pool and mark as uninitialized."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False
            logger.info("SchedulerService shutdown (pool closed)")

    # =========================================================================
    # SchedulerProtocol — 8 methods
    # =========================================================================

    async def submit(self, request: "AgentRequest") -> str:
        """Submit an AgentRequest, auto-classify, and enqueue.

        1. Converts AgentRequest to internal TaskSubmission
        2. Validates the submission
        3. Auto-classifies priority_class if not set
        4. Checks fair-share admission
        5. Reserves credits for boost if needed
        6. Computes effective tier
        7. Enqueues in PostgreSQL

        Returns:
            Task ID string.
        """
        # Map AgentRequest fields to internal types (shared parser — DRY)
        tier, req_state = parse_request_enums(request)

        # Auto-classify priority_class
        priority_class_str = request.priority_class
        if not priority_class_str or priority_class_str == "batch":
            priority_class_str = classify_request(tier, req_state)
        priority_class = PriorityClass(priority_class_str)

        # Parse deadline
        deadline = None
        if request.deadline:
            deadline = datetime.fromisoformat(request.deadline)

        submission = TaskSubmission(
            agent_id=request.agent_id,
            executor_id=request.executor_id or request.agent_id,
            task_type=request.task_type or "default",
            payload=request.payload,
            priority=tier,
            deadline=deadline,
            boost_amount=Decimal(request.boost_amount),
            request_state=req_state,
            priority_class=priority_class,
            estimated_service_time=request.estimated_service_time,
            idempotency_key=request.idempotency_key,
        )

        validate_submission(submission)

        now = datetime.now(UTC)

        # Check fair-share admission
        if not self._fair_share.admit(submission.executor_id):
            snap = self._fair_share.snapshot(submission.executor_id)
            raise ValueError(
                f"Agent {submission.executor_id} is at capacity "
                f"({snap.running_count}/{snap.max_concurrent})"
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
        async with self.pool.acquire() as conn:
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
                request_state=req_state.value,
                priority_class=priority_class.value,
                estimated_service_time=submission.estimated_service_time,
            )

        return str(task_id)

    async def next(self, *, executor_id: str | None = None) -> "AgentRequest | None":
        """Dequeue the next task and return as AgentRequest."""
        task = await self.dequeue_next(executor_id=executor_id)
        if task is None:
            return None

        from nexus.contracts.protocols.scheduler import AgentRequest

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
        async with self.pool.acquire() as conn:
            task = await self._queue.get_task(conn, task_id)
        if task is None:
            return None
        return {
            "id": task.id,
            "status": task.status,
            "agent_id": task.agent_id,
            "executor_id": task.executor_id,
            "task_type": task.task_type,
            "priority_tier": PriorityTier(task.priority_tier).name.lower(),
            "effective_tier": task.effective_tier,
            "priority_class": task.priority_class,
            "request_state": task.request_state,
            "enqueued_at": task.enqueued_at.isoformat() if task.enqueued_at else "",
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "deadline": task.deadline.isoformat() if task.deadline else None,
            "boost_amount": str(task.boost_amount),
            "error_message": task.error_message,
        }

    async def get_status_scoped(
        self,
        task_id: str,
        *,
        agent_id: str,
    ) -> dict[str, Any] | None:
        """Get task status by ID, scoped to agent (owner check).

        Returns None if task doesn't exist or doesn't belong to agent_id.
        """
        async with self.pool.acquire() as conn:
            task = await self._queue.get_task_scoped(conn, task_id, agent_id)
        if task is None:
            return None
        return {
            "id": task.id,
            "status": task.status,
            "agent_id": task.agent_id,
            "executor_id": task.executor_id,
            "task_type": task.task_type,
            "priority_tier": PriorityTier(task.priority_tier).name.lower(),
            "effective_tier": task.effective_tier,
            "priority_class": task.priority_class,
            "request_state": task.request_state,
            "enqueued_at": task.enqueued_at.isoformat() if task.enqueued_at else "",
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "deadline": task.deadline.isoformat() if task.deadline else None,
            "boost_amount": str(task.boost_amount),
            "error_message": task.error_message,
        }

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        """Mark a task as completed or failed, and update fair-share counter."""
        status = TASK_STATUS_FAILED if error else TASK_STATUS_COMPLETED
        async with self.pool.acquire() as conn:
            # Look up the task to get agent_id for fair-share
            task = await self._queue.get_task(conn, task_id)
            await self._queue.complete(conn, task_id, status=status, error=error)

        if task is not None:
            self._fair_share.record_complete(task.agent_id)

    async def classify(self, request: "AgentRequest") -> str:
        """Classify an AgentRequest into a PriorityClass."""
        return classify_agent_request(request)

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]:
        """Get scheduler metrics including queue stats and fair-share."""
        async with self.pool.acquire() as conn:
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
        """Count pending tasks via a direct COUNT(*) query.

        More efficient than metrics() which aggregates by priority_class.
        """
        async with self.pool.acquire() as conn:
            return await self._queue.count_pending(conn, zone_id=zone_id)

    async def cancel(self, agent_id: str) -> int:
        """Cancel all queued tasks for an agent. Returns count cancelled."""
        async with self.pool.acquire() as conn:
            return await self._queue.cancel_by_agent(conn, agent_id)

    # =========================================================================
    # Single-task operations
    # =========================================================================

    async def cancel_by_id(self, task_id: str) -> bool:
        """Cancel a single queued task with credit release."""
        async with self.pool.acquire() as conn:
            task = await self._queue.get_task(conn, task_id)
            cancelled = await self._queue.cancel(conn, task_id)

            if cancelled and task and task.boost_reservation_id and self._credits:
                await self._credits.release_reservation(task.boost_reservation_id)

            return cancelled

    async def cancel_by_id_scoped(self, task_id: str, *, agent_id: str) -> bool:
        """Cancel a single queued task, scoped to agent (owner check).

        Returns False if task doesn't exist, doesn't belong to agent, or
        is not in 'queued' status.
        """
        async with self.pool.acquire() as conn:
            task = await self._queue.get_task_scoped(conn, task_id, agent_id)
            if task is None:
                return False
            cancelled = await self._queue.cancel_scoped(conn, task_id, agent_id)

            if cancelled and task.boost_reservation_id and self._credits:
                await self._credits.release_reservation(task.boost_reservation_id)

            return cancelled

    # =========================================================================
    # Queue operations
    # =========================================================================

    async def dequeue_next(self, *, executor_id: str | None = None) -> ScheduledTask | None:
        """Dequeue the highest-priority task for execution.

        Uses HRRN dequeue when enabled, falls back to classic ordering.
        Updates fair-share counter on successful dequeue.

        Args:
            executor_id: If provided, only dequeue tasks assigned to this executor.
        """
        async with self.pool.acquire() as conn:
            if self._use_hrrn:
                task = await self._queue.dequeue_hrrn(conn, executor_id=executor_id)
            else:
                task = await self._queue.dequeue(conn, executor_id=executor_id)

        if task is not None:
            self._fair_share.record_start(task.agent_id)

        return task

    async def run_aging_sweep(self) -> int:
        """Run one aging sweep cycle."""
        now = datetime.now(UTC)
        async with self.pool.acquire() as conn:
            return await self._queue.aging_sweep(conn, now)

    # =========================================================================
    # Astraea internal methods (Issue #1274)
    # =========================================================================

    async def _on_agent_state_change(self, event: "AgentStateEvent") -> None:
        """Handle agent state transitions — update executor_state in DB."""
        logger.info(
            "Agent state change: %s %s -> %s",
            event.agent_id,
            event.previous_state,
            event.new_state,
        )
        async with self.pool.acquire() as conn:
            await self._queue.update_executor_state(conn, event.agent_id, event.new_state)

    async def sync_fair_share(self) -> None:
        """Initialize fair-share counters from database on startup."""
        async with self.pool.acquire() as conn:
            running_counts = await self._queue.count_running_by_agent(conn)
        self._fair_share.sync_from_db(running_counts)
        logger.info("Fair-share synced from DB: %d agents", len(running_counts))

    async def run_starvation_promotion(
        self,
        threshold_seconds: float = STARVATION_PROMOTION_THRESHOLD_SECS,
    ) -> int:
        """Promote starved BACKGROUND tasks to BATCH."""
        async with self.pool.acquire() as conn:
            count = await self._queue.promote_starved(conn, threshold_seconds)
        if count > 0:
            logger.info("Starvation promotion: %d tasks promoted", count)
        return count
