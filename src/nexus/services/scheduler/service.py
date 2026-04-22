"""Scheduler service - orchestrates priority computation, queue, and credits.

The SchedulerService is the main entry point for task scheduling. It:
1. Validates submissions
2. Computes priority (4-layer model)
3. Reserves credits for boosts
4. Checks admission (rate limit + fair-share)
5. Applies overlap policy (ALLOW/SKIP/CANCEL_PREVIOUS)
6. Enqueues tasks in PostgreSQL
7. Provides status, cancellation, and aging sweep
8. Implements SchedulerProtocol (8-method interface, Issue #1274)
9. Astraea-style classification, HRRN dequeue, and fair-share

Related: Issue #1212, #1274, #2749
"""

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.services.scheduler.constants import (
    STARVATION_PROMOTION_THRESHOLD_SECS,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    OverlapPolicy,
    PriorityClass,
    PriorityTier,
)
from nexus.services.scheduler.exceptions import TaskAlreadyRunning
from nexus.services.scheduler.models import ScheduledTask, TaskSubmission
from nexus.services.scheduler.policies.admission import AdmissionPolicy
from nexus.services.scheduler.policies.classifier import (
    classify_agent_request,
    classify_request,
    parse_request_enums,
)
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.policies.rate_limiter import TokenBucketLimiter
from nexus.services.scheduler.priority import (
    compute_boost_tiers,
    compute_effective_tier,
    validate_submission,
)
from nexus.services.scheduler.queue import TaskQueue

if TYPE_CHECKING:
    from nexus.contracts.protocols.scheduler import AgentRequest, CreditsReservationProtocol
    from nexus.services.scheduler.events import AgentStateEmitter, AgentStateEvent

logger = logging.getLogger(__name__)


def _task_to_status_dict(task: ScheduledTask) -> dict[str, Any]:
    """Convert a ScheduledTask to a status response dict (DRY helper, Issue #2748)."""
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


class SchedulerService:
    """High-level scheduler service implementing SchedulerProtocol.

    Orchestrates priority computation, queue operations, credits
    integration, Astraea classification, HRRN dequeue, admission
    policy (rate limit + fair-share), and overlap policies for the
    hybrid priority system.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue | None = None,
        db_pool: Any = None,
        credits_service: "CreditsReservationProtocol | None" = None,
        state_emitter: "AgentStateEmitter | None" = None,
        fair_share: FairShareCounter | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
        admission: AdmissionPolicy | None = None,
        use_hrrn: bool = True,
    ) -> None:
        self._queue = queue or TaskQueue()
        self._pool = db_pool
        self._credits = credits_service
        self._use_hrrn = use_hrrn
        self._state_emitter = state_emitter

        # Build admission policy from components or use provided one
        fs = fair_share or FairShareCounter()
        rl = rate_limiter or TokenBucketLimiter()
        self._admission = admission or AdmissionPolicy(fair_share=fs, rate_limiter=rl)

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

    async def start(self) -> None:
        """BackgroundService: no-op — lifecycle managed by initialize(pool)/shutdown().

        The scheduler has two-phase init: factory creates with db_pool=None,
        lifespan calls initialize(pool) after bootstrap.  start() exists
        solely for BackgroundService protocol conformance and is called
        during bootstrap before initialize() — this ordering is expected.
        """
        if not self._initialized:
            logger.debug("SchedulerService.start() awaiting initialize(pool) — two-phase init")

    async def stop(self) -> None:
        """BackgroundService: close the asyncpg pool and mark as uninitialized."""
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._initialized = False
            logger.info("SchedulerService shutdown (pool closed)")

    # Legacy alias
    shutdown = stop

    # =========================================================================
    # SchedulerProtocol — 8 methods
    # =========================================================================

    async def submit(self, request: "AgentRequest") -> str:
        """Submit an AgentRequest, auto-classify, and enqueue.

        1. Converts AgentRequest to internal TaskSubmission
        2. Validates the submission
        3. Auto-classifies priority_class if not set
        4. Checks admission (rate limit + fair-share)
        5. Reserves credits for boost if needed
        6. Computes effective tier
        7. Applies overlap policy (ALLOW/SKIP/CANCEL_PREVIOUS)
        8. Enqueues in PostgreSQL

        Returns:
            Task ID string.

        Raises:
            RateLimitExceeded: If the agent exceeds its submission rate.
            CapacityExceeded: If the agent is at its concurrent task limit.
            TaskAlreadyRunning: If overlap policy is SKIP and a matching task is running.
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

        # Parse overlap policy (defaults to SKIP)
        overlap_policy = OverlapPolicy(request.overlap_policy)

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

        # Check admission (rate limit + fair-share)
        self._admission.check(submission.executor_id)

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

        # Common enqueue kwargs
        enqueue_kwargs: dict[str, Any] = {
            "agent_id": submission.agent_id,
            "executor_id": submission.executor_id,
            "task_type": submission.task_type,
            "payload": submission.payload,
            "priority_tier": submission.priority.value,
            "effective_tier": effective_tier,
            "deadline": submission.deadline,
            "boost_amount": submission.boost_amount,
            "boost_tiers": boost_tiers,
            "boost_reservation_id": boost_reservation_id,
            "request_state": req_state.value,
            "priority_class": priority_class.value,
            "estimated_service_time": submission.estimated_service_time,
        }

        # Apply overlap policy
        if submission.idempotency_key is None or overlap_policy == OverlapPolicy.ALLOW:
            # No idempotency key or ALLOW policy — use standard UPSERT
            async with self.pool.acquire() as conn:
                task_id = await self._queue.enqueue(
                    conn,
                    idempotency_key=submission.idempotency_key,
                    **enqueue_kwargs,
                )
        elif overlap_policy == OverlapPolicy.SKIP:
            # Atomic CTE: skip if a running task with same key exists
            async with self.pool.acquire() as conn:
                skip_result = await self._queue.enqueue_skip(
                    conn,
                    idempotency_key=submission.idempotency_key,
                    **enqueue_kwargs,
                )
            if skip_result is None:
                # Release the boost reservation since we're not enqueuing
                if boost_reservation_id and self._credits:
                    await self._credits.release_reservation(boost_reservation_id)
                raise TaskAlreadyRunning(
                    f"Task with idempotency_key '{submission.idempotency_key}' "
                    f"is already running (policy=SKIP)"
                )
            task_id = skip_result
        elif overlap_policy == OverlapPolicy.CANCEL_PREVIOUS:
            # Transaction-wrapped: cancel old task, enqueue new one
            task_id = await self._submit_cancel_previous(
                submission=submission,
                enqueue_kwargs=enqueue_kwargs,
            )
        else:
            raise ValueError(f"Unknown overlap policy: {overlap_policy}")

        return str(task_id)

    async def _submit_cancel_previous(
        self,
        *,
        submission: TaskSubmission,
        enqueue_kwargs: dict[str, Any],
    ) -> str:
        """Handle CANCEL_PREVIOUS overlap policy in a single transaction.

        Atomically cancels any running task with the same idempotency_key
        and enqueues the new task. Credit release happens after commit.
        """
        assert submission.idempotency_key is not None  # noqa: S101

        old_reservation_id: str | None = None

        async with self.pool.acquire() as conn, conn.transaction():
            # Cancel the running task with the same key (if any)
            _cancelled_id, old_reservation_id = await self._queue.cancel_running_by_idempotency_key(
                conn, submission.idempotency_key
            )

            # Enqueue the new task (standard UPSERT for queued/completed)
            task_id = await self._queue.enqueue(
                conn,
                idempotency_key=submission.idempotency_key,
                **enqueue_kwargs,
            )

        # Release the old task's credit reservation after commit
        if old_reservation_id and self._credits:
            await self._credits.release_reservation(old_reservation_id)

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
        return _task_to_status_dict(task)

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
        return _task_to_status_dict(task)

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        """Mark a task as completed or failed, and update fair-share counter."""
        status = TASK_STATUS_FAILED if error else TASK_STATUS_COMPLETED
        async with self.pool.acquire() as conn:
            # Look up the task to get agent_id for fair-share
            task = await self._queue.get_task(conn, task_id)
            await self._queue.complete(conn, task_id, status=status, error=error)

        if task is not None:
            self._admission.fair_share.record_complete(task.agent_id)

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
                for agent_id, snap in self._admission.fair_share.all_snapshots().items()
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
            self._admission.fair_share.record_start(task.agent_id)

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
        self._admission.fair_share.sync_from_db(running_counts)
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
