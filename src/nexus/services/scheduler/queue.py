"""PostgreSQL-backed task queue with priority ordering.

Uses SELECT ... FOR UPDATE SKIP LOCKED for concurrent, safe dequeue.
Tasks are ordered by (effective_tier ASC, enqueued_at ASC) for
strict priority ordering with FIFO within each tier.

HRRN dequeue (Issue #1274) orders by priority_class ASC,
inline HRRN score DESC, enqueued_at ASC for Astraea-style scheduling.

Related: Issue #1212, #1274
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    DEFAULT_EST_SERVICE_TIME_SECS,
    MAX_WAIT_SECONDS,
    TASK_STATUS_COMPLETED,
    PriorityTier,
)
from nexus.services.scheduler.models import ScheduledTask

# =============================================================================
# SQL Statements
# =============================================================================

_SQL_ENQUEUE = """
INSERT INTO scheduled_tasks (
    agent_id, executor_id, task_type, payload,
    priority_tier, effective_tier, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    zone_id, idempotency_key,
    request_state, priority_class, estimated_service_time
) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
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
    zone_id, idempotency_key,
    request_state, priority_class, executor_state, estimated_service_time
"""

_SQL_DEQUEUE_BY_EXECUTOR = """
UPDATE scheduled_tasks
SET status = 'running', started_at = now()
WHERE id = (
    SELECT id FROM scheduled_tasks
    WHERE status = 'queued' AND executor_id = $1
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
    zone_id, idempotency_key,
    request_state, priority_class, executor_state, estimated_service_time
"""

_SQL_DEQUEUE_HRRN = """
UPDATE scheduled_tasks
SET status = 'running', started_at = now()
WHERE id = (
    SELECT id FROM scheduled_tasks
    WHERE status = 'queued'
      AND executor_state IN ('CONNECTED', 'IDLE', 'UNKNOWN')
    ORDER BY
        priority_class ASC,
        (EXTRACT(EPOCH FROM (now() - enqueued_at)) + estimated_service_time)
            / GREATEST(estimated_service_time, 0.001) DESC,
        enqueued_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING
    id::text, agent_id, executor_id, task_type,
    payload::text, priority_tier, effective_tier,
    enqueued_at, status, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    started_at, completed_at, error_message,
    zone_id, idempotency_key,
    request_state, priority_class, executor_state, estimated_service_time
"""

_SQL_DEQUEUE_HRRN_BY_EXECUTOR = """
UPDATE scheduled_tasks
SET status = 'running', started_at = now()
WHERE id = (
    SELECT id FROM scheduled_tasks
    WHERE status = 'queued' AND executor_id = $1
      AND executor_state IN ('CONNECTED', 'IDLE', 'UNKNOWN')
    ORDER BY
        priority_class ASC,
        (EXTRACT(EPOCH FROM (now() - enqueued_at)) + estimated_service_time)
            / GREATEST(estimated_service_time, 0.001) DESC,
        enqueued_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
RETURNING
    id::text, agent_id, executor_id, task_type,
    payload::text, priority_tier, effective_tier,
    enqueued_at, status, deadline,
    boost_amount, boost_tiers, boost_reservation_id,
    started_at, completed_at, error_message,
    zone_id, idempotency_key,
    request_state, priority_class, executor_state, estimated_service_time
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
    zone_id, idempotency_key,
    request_state, priority_class, executor_state, estimated_service_time
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

_SQL_CANCEL_BY_AGENT = """
UPDATE scheduled_tasks
SET status = 'cancelled'
WHERE agent_id = $1 AND status = 'queued'
RETURNING id
"""

# --- Astraea additions (Issue #1274) ---

_SQL_COUNT_RUNNING_BY_AGENT = """
SELECT agent_id, count(*) AS running_count
FROM scheduled_tasks
WHERE status = 'running'
GROUP BY agent_id
"""

_SQL_UPDATE_EXECUTOR_STATE = """
UPDATE scheduled_tasks
SET executor_state = $2
WHERE agent_id = $1 AND status = 'queued'
"""

_SQL_STARVATION_PROMOTE = """
UPDATE scheduled_tasks
SET priority_class = 'batch'
WHERE status = 'queued'
  AND priority_class = 'background'
  AND EXTRACT(EPOCH FROM (now() - enqueued_at)) > $1
RETURNING id
"""

_SQL_COUNT_PENDING = """
SELECT count(*) AS cnt
FROM scheduled_tasks
WHERE status = 'queued'
"""

_SQL_COUNT_PENDING_BY_ZONE = """
SELECT count(*) AS cnt
FROM scheduled_tasks
WHERE status = 'queued' AND zone_id = $1
"""

_SQL_PENDING_METRICS = """
SELECT
    priority_class,
    count(*) AS cnt,
    avg(EXTRACT(EPOCH FROM (now() - enqueued_at))) AS avg_wait,
    max(EXTRACT(EPOCH FROM (now() - enqueued_at))) AS max_wait
FROM scheduled_tasks
WHERE status = 'queued'
GROUP BY priority_class
"""

_SQL_PENDING_METRICS_BY_ZONE = """
SELECT
    priority_class,
    count(*) AS cnt,
    avg(EXTRACT(EPOCH FROM (now() - enqueued_at))) AS avg_wait,
    max(EXTRACT(EPOCH FROM (now() - enqueued_at))) AS max_wait
FROM scheduled_tasks
WHERE status = 'queued' AND zone_id = $1
GROUP BY priority_class
"""

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
        zone_id=row.get("zone_id", ROOT_ZONE_ID),
        idempotency_key=row.get("idempotency_key"),
        # Astraea fields with defaults for backward compat
        request_state=row.get("request_state", "pending"),
        priority_class=row.get("priority_class", "batch"),
        executor_state=row.get("executor_state", "UNKNOWN"),
        estimated_service_time=float(
            row.get("estimated_service_time", DEFAULT_EST_SERVICE_TIME_SECS)
        ),
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
        zone_id: str = ROOT_ZONE_ID,
        deadline: datetime | None = None,
        boost_amount: Decimal = Decimal("0"),
        boost_tiers: int = 0,
        boost_reservation_id: str | None = None,
        idempotency_key: str | None = None,
        request_state: str = "pending",
        priority_class: str = "batch",
        estimated_service_time: float = DEFAULT_EST_SERVICE_TIME_SECS,
    ) -> str:
        """Insert a task into the queue.

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
            request_state,
            priority_class,
            estimated_service_time,
        )

        # Notify dispatcher
        await conn.execute(_SQL_NOTIFY, str(task_id))

        return str(task_id)

    async def dequeue(self, conn: Any, *, executor_id: str | None = None) -> ScheduledTask | None:
        """Dequeue the highest-priority task (classic effective_tier ordering).

        Uses FOR UPDATE SKIP LOCKED to safely handle concurrent workers.
        Atomically sets status to 'running'.

        Args:
            conn: Database connection.
            executor_id: If provided, only dequeue tasks assigned to this executor.
        """
        if executor_id is not None:
            row = await conn.fetchrow(_SQL_DEQUEUE_BY_EXECUTOR, executor_id)
        else:
            row = await conn.fetchrow(_SQL_DEQUEUE)
        if row is None:
            return None
        return _row_to_task(row)

    async def dequeue_hrrn(
        self, conn: Any, *, executor_id: str | None = None
    ) -> ScheduledTask | None:
        """Dequeue using HRRN scoring within priority classes (Astraea).

        Orders by: priority_class ASC, HRRN score DESC, enqueued_at ASC.
        Filters out tasks whose executor is SUSPENDED.

        Args:
            conn: Database connection.
            executor_id: If provided, only dequeue tasks assigned to this executor.
        """
        if executor_id is not None:
            row = await conn.fetchrow(_SQL_DEQUEUE_HRRN_BY_EXECUTOR, executor_id)
        else:
            row = await conn.fetchrow(_SQL_DEQUEUE_HRRN)
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
        """Mark a task as completed or failed."""
        await conn.execute(_SQL_COMPLETE, task_id, status, error)

    async def cancel(self, conn: Any, task_id: str) -> bool:
        """Cancel a queued task. Returns True if cancelled."""
        result = await conn.fetchval(_SQL_CANCEL, task_id)
        return result is not None

    async def get_task(self, conn: Any, task_id: str) -> ScheduledTask | None:
        """Look up a task by ID."""
        row = await conn.fetchrow(_SQL_GET_TASK, task_id)
        if row is None:
            return None
        return _row_to_task(row)

    async def aging_sweep(self, conn: Any, now: datetime) -> int:
        """Run aging sweep to recalculate effective_tier for queued tasks."""
        count = await conn.fetchval(
            _SQL_AGING_SWEEP,
            now,
            AGING_THRESHOLD_SECONDS,
            MAX_WAIT_SECONDS,
        )
        return count or 0

    async def cancel_by_agent(self, conn: Any, agent_id: str) -> int:
        """Cancel all queued tasks for an agent. Returns count cancelled."""
        rows = await conn.fetch(_SQL_CANCEL_BY_AGENT, agent_id)
        return len(rows)

    # --- Astraea methods (Issue #1274) ---

    async def count_running_by_agent(self, conn: Any) -> dict[str, int]:
        """Get running task count per agent for fair-share sync."""
        rows = await conn.fetch(_SQL_COUNT_RUNNING_BY_AGENT)
        return {row["agent_id"]: row["running_count"] for row in rows}

    async def update_executor_state(self, conn: Any, agent_id: str, executor_state: str) -> None:
        """Update executor_state for all queued tasks of an agent."""
        await conn.execute(_SQL_UPDATE_EXECUTOR_STATE, agent_id, executor_state)

    async def promote_starved(self, conn: Any, threshold_seconds: float) -> int:
        """Promote BACKGROUND tasks that have waited longer than threshold to BATCH."""
        rows = await conn.fetch(_SQL_STARVATION_PROMOTE, threshold_seconds)
        return len(rows)

    async def count_pending(self, conn: Any, *, zone_id: str | None = None) -> int:
        """Count pending (queued) tasks via a direct COUNT(*).

        More efficient than ``get_queue_metrics()`` when only the total
        count is needed (avoids GROUP BY aggregation).
        """
        if zone_id is not None:
            row = await conn.fetchrow(_SQL_COUNT_PENDING_BY_ZONE, zone_id)
        else:
            row = await conn.fetchrow(_SQL_COUNT_PENDING)
        return row["cnt"] if row else 0

    async def get_queue_metrics(
        self, conn: Any, *, zone_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Get aggregated queue metrics by priority_class.

        Args:
            conn: Database connection.
            zone_id: If provided, filter metrics to this zone only.
        """
        if zone_id is not None:
            rows = await conn.fetch(_SQL_PENDING_METRICS_BY_ZONE, zone_id)
        else:
            rows = await conn.fetch(_SQL_PENDING_METRICS)
        return [dict(row) for row in rows]
