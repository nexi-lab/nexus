"""SQLAlchemy async ORM task queue with priority ordering.

Uses `with_for_update(skip_locked=True)` for concurrent, safe dequeue.
Tasks are ordered by (effective_tier ASC, enqueued_at ASC) for
strict priority ordering with FIFO within each tier.

Related: Issue #1212
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import SmallInteger, case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from nexus.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    MAX_WAIT_SECONDS,
    TASK_STATUS_COMPLETED,
    PriorityTier,
)
from nexus.scheduler.models import ScheduledTask
from nexus.storage.models.scheduler import ScheduledTaskModel


def _model_to_task(m: ScheduledTaskModel) -> ScheduledTask:
    """Convert a ScheduledTaskModel ORM instance to a ScheduledTask dataclass."""
    payload_raw = m.payload
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw or {}

    return ScheduledTask(
        id=str(m.id),
        agent_id=m.agent_id,
        executor_id=m.executor_id,
        task_type=m.task_type,
        payload=payload,
        priority_tier=PriorityTier(m.priority_tier),
        effective_tier=m.effective_tier,
        enqueued_at=m.enqueued_at,
        status=m.status,
        deadline=m.deadline,
        boost_amount=Decimal(str(m.boost_amount)) if m.boost_amount else Decimal("0"),
        boost_tiers=m.boost_tiers or 0,
        boost_reservation_id=m.boost_reservation_id,
        started_at=m.started_at,
        completed_at=m.completed_at,
        error_message=m.error_message,
        zone_id=m.zone_id or "root",
        idempotency_key=m.idempotency_key,
    )


class TaskQueue:
    """SQLAlchemy async ORM priority task queue.

    All methods use async sessions from the provided session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue(
        self,
        *,
        agent_id: str,
        executor_id: str,
        task_type: str,
        payload: dict[str, Any],
        priority_tier: int,
        effective_tier: int,
        zone_id: str = "root",
        deadline: datetime | None = None,
        boost_amount: Decimal = Decimal("0"),
        boost_tiers: int = 0,
        boost_reservation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Insert a task into the queue.

        Args:
            agent_id: Submitting agent.
            executor_id: Target executor.
            task_type: Type identifier.
            payload: Task data as dict.
            priority_tier: Base priority tier value.
            effective_tier: Computed effective tier.
            zone_id: Zone for multi-tenancy.
            deadline: Optional deadline.
            boost_amount: Credits paid for boost.
            boost_tiers: Computed boost tiers.
            boost_reservation_id: TigerBeetle reservation ID for boost.
            idempotency_key: Deduplication key.

        Returns:
            Task ID as string.
        """
        payload_json = json.dumps(payload)

        async with self._session_factory() as session:
            task = ScheduledTaskModel(
                agent_id=agent_id,
                executor_id=executor_id,
                task_type=task_type,
                payload=payload_json,
                priority_tier=priority_tier,
                effective_tier=effective_tier,
                deadline=deadline,
                boost_amount=boost_amount,
                boost_tiers=boost_tiers,
                boost_reservation_id=boost_reservation_id,
                zone_id=zone_id,
                idempotency_key=idempotency_key,
            )
            session.add(task)
            await session.flush()
            task_id = str(task.id)

            # pg_notify for dispatcher — requires text() as it's a PG-specific function call
            await session.execute(text("SELECT pg_notify('task_enqueued', :tid)"), {"tid": task_id})

            await session.commit()
            return task_id

    async def dequeue(self) -> ScheduledTask | None:
        """Dequeue the highest-priority task.

        Uses FOR UPDATE SKIP LOCKED to safely handle concurrent workers.
        Atomically sets status to 'running'.

        Returns:
            ScheduledTask if available, None if queue is empty.
        """
        async with self._session_factory() as session:
            # Subquery: pick the next queued task with row-level lock
            subq = (
                select(ScheduledTaskModel.id)
                .where(ScheduledTaskModel.status == "queued")
                .order_by(
                    ScheduledTaskModel.effective_tier.asc(),
                    ScheduledTaskModel.enqueued_at.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )

            # Update that row atomically
            stmt = (
                update(ScheduledTaskModel)
                .where(ScheduledTaskModel.id == subq)
                .values(status="running", started_at=func.now())
                .returning(ScheduledTaskModel)
            )
            result = await session.execute(stmt)
            row = result.scalars().first()
            if row is None:
                return None

            await session.commit()
            return _model_to_task(row)

    async def complete(
        self,
        task_id: str,
        *,
        status: str = TASK_STATUS_COMPLETED,
        error: str | None = None,
    ) -> None:
        """Mark a task as completed or failed.

        Args:
            task_id: Task to complete.
            status: Final status ('completed' or 'failed').
            error: Error message if failed.
        """
        async with self._session_factory() as session:
            stmt = (
                update(ScheduledTaskModel)
                .where(ScheduledTaskModel.id == task_id)
                .values(status=status, completed_at=func.now(), error_message=error)
            )
            await session.execute(stmt)
            await session.commit()

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued task.

        Only cancels tasks with status 'queued'. Running tasks cannot
        be cancelled through this method.

        Args:
            task_id: Task to cancel.

        Returns:
            True if cancelled, False if task was not in 'queued' status.
        """
        async with self._session_factory() as session:
            stmt = (
                update(ScheduledTaskModel)
                .where(
                    ScheduledTaskModel.id == task_id,
                    ScheduledTaskModel.status == "queued",
                )
                .values(status="cancelled")
                .returning(ScheduledTaskModel.status)
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            await session.commit()
            return row is not None

    async def get_task(self, task_id: str) -> ScheduledTask | None:
        """Look up a task by ID.

        Args:
            task_id: Task ID to look up.

        Returns:
            ScheduledTask if found, None otherwise.
        """
        async with self._session_factory() as session:
            stmt = select(ScheduledTaskModel).where(ScheduledTaskModel.id == task_id)
            result = await session.execute(stmt)
            row = result.scalars().first()
            if row is None:
                return None
            return _model_to_task(row)

    async def aging_sweep(self, now: datetime) -> int:
        """Run aging sweep to recalculate effective_tier for queued tasks.

        Updates tasks whose effective_tier has changed due to aging or
        max-wait escalation.

        Args:
            now: Current timestamp.

        Returns:
            Number of tasks updated.
        """
        async with self._session_factory() as session:
            T = ScheduledTaskModel  # noqa: N806 — alias for readability

            # Compute age in seconds
            age_seconds = func.extract("epoch", now - T.enqueued_at)
            aging_tiers = func.floor(age_seconds / AGING_THRESHOLD_SECONDS)

            # New effective tier = max(0, min(tier - boost - aging, max_wait_cap))
            max_wait_cap = case(
                (age_seconds > MAX_WAIT_SECONDS, 1),
                else_=T.priority_tier,
            )
            new_tier = func.greatest(
                0,
                func.least(
                    T.priority_tier - T.boost_tiers - func.cast(aging_tiers, SmallInteger),
                    max_wait_cap,
                ),
            )

            stmt = (
                update(T)
                .where(T.status == "queued", T.effective_tier != new_tier)
                .values(effective_tier=new_tier)
            )
            cursor = cast(Any, await session.execute(stmt))
            await session.commit()
            return cursor.rowcount or 0
