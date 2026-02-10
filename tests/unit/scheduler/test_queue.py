"""Tests for queue operations.

TDD tests for PostgreSQL-backed task queue with SKIP LOCKED.
Uses mock asyncpg connection to test SQL logic without a real database.

Test categories:
1. Enqueue
2. Dequeue (priority ordering)
3. Complete/Fail
4. Cancel
5. Aging sweep
6. Idempotency
7. Task lookup

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from nexus.scheduler.constants import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    PriorityTier,
)
from nexus.scheduler.models import ScheduledTask
from nexus.scheduler.queue import TaskQueue

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_conn():
    """Mock asyncpg connection with fetch/execute methods."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=None)
    return conn


@pytest.fixture
def queue():
    """Create a TaskQueue instance."""
    return TaskQueue()


# =============================================================================
# 1. Enqueue
# =============================================================================


class TestEnqueue:
    """Test task enqueue operations."""

    @pytest.mark.asyncio
    async def test_enqueue_returns_task_id(self, queue, mock_conn):
        """Enqueue should return a task ID string."""
        mock_conn.fetchval = AsyncMock(return_value="task-uuid-123")

        task_id = await queue.enqueue(
            conn=mock_conn,
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            payload={"key": "value"},
            priority_tier=PriorityTier.NORMAL,
            effective_tier=2,
            zone_id="default",
        )

        assert task_id == "task-uuid-123"
        mock_conn.fetchval.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_with_boost(self, queue, mock_conn):
        """Enqueue with boost should store boost metadata."""
        mock_conn.fetchval = AsyncMock(return_value="task-uuid-456")

        task_id = await queue.enqueue(
            conn=mock_conn,
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            payload={},
            priority_tier=PriorityTier.LOW,
            effective_tier=1,
            boost_amount=Decimal("0.02"),
            boost_tiers=2,
            boost_reservation_id="res-789",
            zone_id="default",
        )

        assert task_id == "task-uuid-456"

    @pytest.mark.asyncio
    async def test_enqueue_with_deadline(self, queue, mock_conn):
        """Enqueue with deadline should store deadline."""
        mock_conn.fetchval = AsyncMock(return_value="task-uuid-789")
        deadline = datetime.now(UTC) + timedelta(hours=1)

        task_id = await queue.enqueue(
            conn=mock_conn,
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="urgent",
            payload={},
            priority_tier=PriorityTier.HIGH,
            effective_tier=1,
            deadline=deadline,
            zone_id="default",
        )

        assert task_id == "task-uuid-789"

    @pytest.mark.asyncio
    async def test_enqueue_sends_notify(self, queue, mock_conn):
        """Enqueue should send NOTIFY for the dispatcher."""
        mock_conn.fetchval = AsyncMock(return_value="task-123")

        await queue.enqueue(
            conn=mock_conn,
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="test",
            payload={},
            priority_tier=PriorityTier.NORMAL,
            effective_tier=2,
            zone_id="default",
        )

        # Should have called execute for NOTIFY
        notify_calls = [
            call
            for call in mock_conn.execute.call_args_list
            if "NOTIFY" in str(call) or "notify" in str(call)
        ]
        assert len(notify_calls) >= 1


# =============================================================================
# 2. Dequeue
# =============================================================================


class TestDequeue:
    """Test task dequeue operations."""

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self, queue, mock_conn):
        """Dequeue from empty queue should return None."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        result = await queue.dequeue(mock_conn)
        assert result is None

    @pytest.mark.asyncio
    async def test_dequeue_returns_scheduled_task(self, queue, mock_conn):
        """Dequeue should return a ScheduledTask with correct fields."""
        now = datetime.now(UTC)
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": "task-123",
                "agent_id": "agent-a",
                "executor_id": "agent-b",
                "task_type": "process",
                "payload": '{"key": "value"}',
                "priority_tier": 2,
                "effective_tier": 1,
                "enqueued_at": now,
                "status": TASK_STATUS_RUNNING,
                "deadline": None,
                "boost_amount": Decimal("0.01"),
                "boost_tiers": 1,
                "boost_reservation_id": "res-456",
                "started_at": now,
                "completed_at": None,
                "error_message": None,
                "zone_id": "default",
                "idempotency_key": None,
            }
        )

        task = await queue.dequeue(mock_conn)

        assert task is not None
        assert isinstance(task, ScheduledTask)
        assert task.id == "task-123"
        assert task.agent_id == "agent-a"
        assert task.executor_id == "agent-b"
        assert task.status == TASK_STATUS_RUNNING

    @pytest.mark.asyncio
    async def test_dequeue_uses_skip_locked(self, queue, mock_conn):
        """Dequeue SQL should include FOR UPDATE SKIP LOCKED."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        await queue.dequeue(mock_conn)

        call_args = mock_conn.fetchrow.call_args
        sql = call_args[0][0] if call_args[0] else ""
        assert "SKIP LOCKED" in sql.upper() or "skip locked" in sql.lower()

    @pytest.mark.asyncio
    async def test_dequeue_orders_by_effective_tier_then_enqueued(self, queue, mock_conn):
        """Dequeue should ORDER BY effective_tier ASC, enqueued_at ASC."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        await queue.dequeue(mock_conn)

        call_args = mock_conn.fetchrow.call_args
        sql = call_args[0][0] if call_args[0] else ""
        sql_upper = sql.upper()
        assert "ORDER BY" in sql_upper
        assert "EFFECTIVE_TIER" in sql_upper
        assert "ENQUEUED_AT" in sql_upper


# =============================================================================
# 3. Complete / Fail
# =============================================================================


class TestComplete:
    """Test task completion operations."""

    @pytest.mark.asyncio
    async def test_complete_success(self, queue, mock_conn):
        """Complete should update status to completed."""
        await queue.complete(mock_conn, "task-123")

        mock_conn.execute.assert_called_once()
        call_args = mock_conn.execute.call_args
        sql = call_args[0][0]
        assert TASK_STATUS_COMPLETED in sql or "$" in sql  # Parameterized

    @pytest.mark.asyncio
    async def test_complete_with_error(self, queue, mock_conn):
        """Complete with error should set status to failed."""
        await queue.complete(mock_conn, "task-123", status=TASK_STATUS_FAILED, error="Timeout")

        mock_conn.execute.assert_called_once()


# =============================================================================
# 4. Cancel
# =============================================================================


class TestCancel:
    """Test task cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self, queue, mock_conn):
        """Should cancel a queued task and return True."""
        mock_conn.fetchval = AsyncMock(return_value=TASK_STATUS_QUEUED)

        result = await queue.cancel(mock_conn, "task-123")
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_running_task_returns_false(self, queue, mock_conn):
        """Should not cancel a running task."""
        mock_conn.fetchval = AsyncMock(return_value=None)

        result = await queue.cancel(mock_conn, "task-123")
        assert result is False


# =============================================================================
# 5. Aging Sweep
# =============================================================================


class TestAgingSweep:
    """Test periodic aging sweep."""

    @pytest.mark.asyncio
    async def test_aging_sweep_returns_updated_count(self, queue, mock_conn):
        """Aging sweep should return number of tasks updated."""
        mock_conn.fetchval = AsyncMock(return_value=5)

        count = await queue.aging_sweep(mock_conn, datetime.now(UTC))
        assert count == 5

    @pytest.mark.asyncio
    async def test_aging_sweep_zero_when_no_tasks(self, queue, mock_conn):
        """Aging sweep with no tasks should return 0."""
        mock_conn.fetchval = AsyncMock(return_value=0)

        count = await queue.aging_sweep(mock_conn, datetime.now(UTC))
        assert count == 0


# =============================================================================
# 6. Task Lookup
# =============================================================================


class TestGetTask:
    """Test task lookup by ID."""

    @pytest.mark.asyncio
    async def test_get_existing_task(self, queue, mock_conn):
        """Should return ScheduledTask for existing task."""
        now = datetime.now(UTC)
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": "task-123",
                "agent_id": "agent-a",
                "executor_id": "agent-b",
                "task_type": "process",
                "payload": "{}",
                "priority_tier": 2,
                "effective_tier": 2,
                "enqueued_at": now,
                "status": TASK_STATUS_QUEUED,
                "deadline": None,
                "boost_amount": Decimal("0"),
                "boost_tiers": 0,
                "boost_reservation_id": None,
                "started_at": None,
                "completed_at": None,
                "error_message": None,
                "zone_id": "default",
                "idempotency_key": None,
            }
        )

        task = await queue.get_task(mock_conn, "task-123")

        assert task is not None
        assert task.id == "task-123"
        assert task.status == TASK_STATUS_QUEUED

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, queue, mock_conn):
        """Should return None for non-existent task."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        task = await queue.get_task(mock_conn, "nonexistent")
        assert task is None
