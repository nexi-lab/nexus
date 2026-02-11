"""PostgreSQL-backed task queue with priority ordering.

Uses SELECT ... FOR UPDATE SKIP LOCKED for concurrent, safe dequeue.
Tasks are ordered by (effective_tier ASC, enqueued_at ASC) for
strict priority ordering with FIFO within each tier.

Related: Issue #1212
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from nexus.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    MAX_WAIT_SECONDS,
    TASK_STATUS_COMPLETED,
    PriorityTier,
)
from nexus.scheduler.models import ScheduledTask

# =============================================================================
# SQL Statements
# =============================================================================

_SQL_ENQUEUE = """
INSERT INTO scheduled_tasks (
    agent_id, executor_id, task_type, payload,
    priority_tier, effective_tier, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    zone_id, idempotency_key
) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12)
RETURNING id::text
"""

_SQL_DEQUEUE = """
UPDATE scheduled_tasks
SET status = 'running', started_at = now()
WHERE id = (
    SELECT id FROM scheduled_tasks
    WHERE status = 'queued'
    ORDER BY effective_tier ASC, enqueued_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING
    id::text, agent_id, executor_id, task_type,
    payload::text, priority_tier, effective_tier,
    enqueued_at, status, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    started_at, completed_at, error_message,
    zone_id, idempotency_key
"""

_SQL_COMPLETE = """
UPDATE scheduled_tasks
SET status = $2, completed_at = now(), error_message = $3
WHERE id = $1::uuid
"""

_SQL_CANCEL = """
UPDATE scheduled_tasks
SET status = 'cancelled'
WHERE id = $1::uuid AND status = 'queued'
RETURNING status
"""

_SQL_GET_TASK = """
SELECT
    id::text, agent_id, executor_id, task_type,
    payload::text, priority_tier, effective_tier,
    enqueued_at, status, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    started_at, completed_at, error_message,
    zone_id, idempotency_key
FROM scheduled_tasks
WHERE id = $1::uuid
"""

_SQL_AGING_SWEEP = """
WITH updated AS (
    UPDATE scheduled_tasks
    SET effective_tier = GREATEST(
        0,
        LEAST(
            priority_tier - boost_tiers
                - FLOOR(EXTRACT(EPOCH FROM ($1::timestamptz - enqueued_at)) / $2)::int,
            CASE
                WHEN EXTRACT(EPOCH FROM ($1::timestamptz - enqueued_at)) > $3
                THEN 1
                ELSE priority_tier
            END
        )
    )
    WHERE status = 'queued'
      AND effective_tier != GREATEST(
          0,
          LEAST(
              priority_tier - boost_tiers
                  - FLOOR(EXTRACT(EPOCH FROM ($1::timestamptz - enqueued_at)) / $2)::int,
              CASE
                  WHEN EXTRACT(EPOCH FROM ($1::timestamptz - enqueued_at)) > $3
                  THEN 1
                  ELSE priority_tier
              END
          )
      )
    RETURNING id
)
SELECT count(*) FROM updated
"""

_SQL_NOTIFY = "SELECT pg_notify('task_enqueued', $1)"


# =============================================================================
# Row-to-Model Conversion
# =============================================================================


def _row_to_task(row: dict[str, Any]) -> ScheduledTask:
    """Convert a database row (dict) to a ScheduledTask."""
    payload_raw = row.get("payload", "{}")
    payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw or {}

    return ScheduledTask(
        id=str(row["id"]),
        agent_id=row["agent_id"],
        executor_id=row["executor_id"],
        task_type=row["task_type"],
        payload=payload,
        priority_tier=PriorityTier(row["priority_tier"]),
        effective_tier=row["effective_tier"],
        enqueued_at=row["enqueued_at"],
        status=row["status"],
        deadline=row.get("deadline"),
        boost_amount=Decimal(str(row.get("boost_amount", 0))),
        boost_tiers=row.get("boost_tiers", 0),
        boost_reservation_id=row.get("boost_reservation_id"),
        started_at=row.get("started_at"),
        completed_at=row.get("completed_at"),
        error_message=row.get("error_message"),
        zone_id=row.get("zone_id", "default"),
        idempotency_key=row.get("idempotency_key"),
    )


# =============================================================================
# TaskQueue
# =============================================================================


class TaskQueue:
    """PostgreSQL-backed priority task queue.

    All methods take an asyncpg connection as the first argument,
    allowing callers to manage transactions externally.
    """

    async def enqueue(
        self,
        conn: Any,
        *,
        agent_id: str,
        executor_id: str,
        task_type: str,
        payload: dict[str, Any],
        priority_tier: int,
        effective_tier: int,
        zone_id: str = "default",
        deadline: datetime | None = None,
        boost_amount: Decimal = Decimal("0"),
        boost_tiers: int = 0,
        boost_reservation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """Insert a task into the queue.

        Args:
            conn: asyncpg connection.
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

        task_id = await conn.fetchval(
            _SQL_ENQUEUE,
            agent_id,
            executor_id,
            task_type,
            payload_json,
            priority_tier,
            effective_tier,
            deadline,
            boost_amount,
            boost_tiers,
            boost_reservation_id,
            zone_id,
            idempotency_key,
        )

        # Notify dispatcher
        await conn.execute(_SQL_NOTIFY, str(task_id))

        return str(task_id)

    async def dequeue(self, conn: Any) -> ScheduledTask | None:
        """Dequeue the highest-priority task.

        Uses FOR UPDATE SKIP LOCKED to safely handle concurrent workers.
        Atomically sets status to 'running'.

        Args:
            conn: asyncpg connection.

        Returns:
            ScheduledTask if available, None if queue is empty.
        """
        row = await conn.fetchrow(_SQL_DEQUEUE)
        if row is None:
            return None
        return _row_to_task(row)

    async def complete(
        self,
        conn: Any,
        task_id: str,
        *,
        status: str = TASK_STATUS_COMPLETED,
        error: str | None = None,
    ) -> None:
        """Mark a task as completed or failed.

        Args:
            conn: asyncpg connection.
            task_id: Task to complete.
            status: Final status ('completed' or 'failed').
            error: Error message if failed.
        """
        await conn.execute(_SQL_COMPLETE, task_id, status, error)

    async def cancel(self, conn: Any, task_id: str) -> bool:
        """Cancel a queued task.

        Only cancels tasks with status 'queued'. Running tasks cannot
        be cancelled through this method.

        Args:
            conn: asyncpg connection.
            task_id: Task to cancel.

        Returns:
            True if cancelled, False if task was not in 'queued' status.
        """
        result = await conn.fetchval(_SQL_CANCEL, task_id)
        return result is not None

    async def get_task(self, conn: Any, task_id: str) -> ScheduledTask | None:
        """Look up a task by ID.

        Args:
            conn: asyncpg connection.
            task_id: Task ID to look up.

        Returns:
            ScheduledTask if found, None otherwise.
        """
        row = await conn.fetchrow(_SQL_GET_TASK, task_id)
        if row is None:
            return None
        return _row_to_task(row)

    async def aging_sweep(self, conn: Any, now: datetime) -> int:
        """Run aging sweep to recalculate effective_tier for queued tasks.

        Updates tasks whose effective_tier has changed due to aging or
        max-wait escalation.

        Args:
            conn: asyncpg connection.
            now: Current timestamp.

        Returns:
            Number of tasks updated.
        """
        count = await conn.fetchval(
            _SQL_AGING_SWEEP,
            now,
            AGING_THRESHOLD_SECONDS,
            MAX_WAIT_SECONDS,
        )
        return count or 0
