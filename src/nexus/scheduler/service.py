"""Scheduler service - orchestrates priority computation, queue, and credits.

The SchedulerService is the main entry point for task scheduling. It:
1. Validates submissions
2. Computes priority (4-layer model)
3. Reserves credits for boosts
4. Enqueues tasks in PostgreSQL
5. Provides status, cancellation, and aging sweep

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.scheduler.constants import (
    TASK_STATUS_QUEUED,
)
from nexus.scheduler.models import ScheduledTask, TaskSubmission
from nexus.scheduler.priority import (
    compute_boost_tiers,
    compute_effective_tier,
    validate_submission,
)
from nexus.scheduler.queue import TaskQueue

if TYPE_CHECKING:
    from nexus.pay.credits import CreditsService


class SchedulerService:
    """High-level scheduler service.

    Orchestrates priority computation, queue operations, and
    credits integration for the hybrid priority system.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue | None = None,
        db_pool: Any = None,
        credits_service: CreditsService | None = None,
    ) -> None:
        self._queue = queue or TaskQueue()
        self._pool = db_pool
        self._credits = credits_service

    async def _get_conn(self) -> Any:
        """Acquire a connection from the pool."""
        return await self._pool.acquire()

    async def submit_task(self, submission: TaskSubmission) -> ScheduledTask:
        """Submit a task for scheduling.

        1. Validates the submission
        2. If boost > 0, reserves credits
        3. Computes effective tier
        4. Enqueues in PostgreSQL
        5. Returns the scheduled task

        Args:
            submission: Task submission with priority signals.

        Returns:
            ScheduledTask with computed priority and queue position.

        Raises:
            ValueError: If submission is invalid.
            InsufficientCreditsError: If boost can't be afforded.
        """
        validate_submission(submission)

        now = datetime.now(UTC)

        # Reserve credits for boost if needed
        boost_tiers = compute_boost_tiers(submission.boost_amount)
        boost_reservation_id: str | None = None

        if submission.boost_amount > 0 and self._credits is not None:
            boost_reservation_id = await self._credits.reserve(
                agent_id=submission.agent_id,
                amount=submission.boost_amount,
                timeout_seconds=3600,  # 1 hour timeout for boost reservations
            )

        # Compute effective tier
        effective_tier = compute_effective_tier(submission, enqueued_at=now, now=now)

        # Enqueue
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
        )

    async def get_status(self, task_id: str) -> ScheduledTask | None:
        """Get task status by ID.

        Args:
            task_id: Task identifier.

        Returns:
            ScheduledTask if found, None otherwise.
        """
        async with self._pool.acquire() as conn:
            return await self._queue.get_task(conn, task_id)

    async def cancel_task(self, task_id: str, agent_id: str = "") -> bool:  # noqa: ARG002
        """Cancel a queued task.

        If the task has a boost reservation, it is released.
        Only tasks with status 'queued' can be cancelled.

        Args:
            task_id: Task to cancel.
            agent_id: Agent requesting cancellation (for authorization).

        Returns:
            True if cancelled, False otherwise.
        """
        async with self._pool.acquire() as conn:
            # Look up task for boost reservation
            task = await self._queue.get_task(conn, task_id)

            # Attempt cancellation
            cancelled = await self._queue.cancel(conn, task_id)

            # Release boost reservation if cancelled
            if cancelled and task and task.boost_reservation_id and self._credits:
                await self._credits.release_reservation(task.boost_reservation_id)

            return cancelled

    async def dequeue_next(self) -> ScheduledTask | None:
        """Dequeue the highest-priority task for execution.

        Returns:
            ScheduledTask if available, None if queue is empty.
        """
        async with self._pool.acquire() as conn:
            return await self._queue.dequeue(conn)

    async def run_aging_sweep(self) -> int:
        """Run one aging sweep cycle.

        Recalculates effective_tier for all queued tasks
        based on their wait time.

        Returns:
            Number of tasks updated.
        """
        now = datetime.now(UTC)
        async with self._pool.acquire() as conn:
            return await self._queue.aging_sweep(conn, now)
