"""Tests for SchedulerService.

TDD tests for the scheduler service that orchestrates
priority computation, queue operations, and credits integration.

Test categories:
1. Submit task
2. Submit task with boost
3. Get task status
4. Cancel task
5. Dequeue
6. Aging sweep
7. Validation errors
8. Edge cases

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.scheduler.constants import (
    BOOST_COST_PER_TIER,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    PriorityTier,
)
from nexus.scheduler.models import ScheduledTask, TaskSubmission
from nexus.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_queue():
    """Mock TaskQueue."""
    q = AsyncMock()
    q.enqueue = AsyncMock(return_value="task-uuid-123")
    q.dequeue = AsyncMock(return_value=None)
    q.complete = AsyncMock()
    q.cancel = AsyncMock(return_value=True)
    q.get_task = AsyncMock(return_value=None)
    q.aging_sweep = AsyncMock(return_value=0)
    return q


@pytest.fixture
def mock_conn():
    """Mock asyncpg connection."""
    return AsyncMock()


@pytest.fixture
def mock_pool(mock_conn):
    """Mock asyncpg connection pool with async context manager support."""
    pool = MagicMock()
    # pool.acquire() returns an async context manager that yields mock_conn
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=mock_conn)
    acm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acm)
    return pool


@pytest.fixture
def mock_credits():
    """Mock CreditsService."""
    service = AsyncMock()
    service.reserve = AsyncMock(return_value="res-boost-123")
    service.release_reservation = AsyncMock()
    service.commit_reservation = AsyncMock()
    return service


@pytest.fixture
def scheduler(mock_queue, mock_pool, mock_credits):
    """Create SchedulerService with mocked dependencies."""
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        credits_service=mock_credits,
    )


# =============================================================================
# 1. Submit Task
# =============================================================================


class TestSubmitTask:
    """Test task submission."""

    @pytest.mark.asyncio
    async def test_submit_basic(self, scheduler, mock_queue):
        """Submit with default priority returns ScheduledTask."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            payload={"key": "value"},
        )

        result = await scheduler.submit_task(task)

        assert isinstance(result, ScheduledTask)
        assert result.id == "task-uuid-123"
        assert result.priority_tier == PriorityTier.NORMAL
        assert result.effective_tier == 2
        assert result.status == TASK_STATUS_QUEUED
        mock_queue.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_submit_critical_priority(self, scheduler, mock_queue):
        """Submit with CRITICAL priority."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="urgent",
            priority=PriorityTier.CRITICAL,
        )

        result = await scheduler.submit_task(task)
        assert result.effective_tier == 0

    @pytest.mark.asyncio
    async def test_submit_with_deadline(self, scheduler, mock_queue):
        """Submit with deadline stores deadline."""
        deadline = datetime.now(UTC) + timedelta(hours=1)
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="timed",
            deadline=deadline,
        )

        result = await scheduler.submit_task(task)
        assert result.deadline == deadline

    @pytest.mark.asyncio
    async def test_submit_with_idempotency_key(self, scheduler, mock_queue):
        """Submit with idempotency key passes it to queue."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            idempotency_key="unique-key-123",
        )

        result = await scheduler.submit_task(task)
        assert result.idempotency_key == "unique-key-123"

        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["idempotency_key"] == "unique-key-123"


# =============================================================================
# 2. Submit Task with Boost
# =============================================================================


class TestSubmitWithBoost:
    """Test task submission with price boost."""

    @pytest.mark.asyncio
    async def test_boost_reserves_credits(self, scheduler, mock_credits, mock_queue):
        """Boost should reserve credits via CreditsService."""
        boost_amount = BOOST_COST_PER_TIER * 2
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            boost_amount=boost_amount,
        )

        result = await scheduler.submit_task(task)

        mock_credits.reserve.assert_called_once()
        assert result.boost_reservation_id == "res-boost-123"
        assert result.boost_tiers == 2

    @pytest.mark.asyncio
    async def test_boost_lowers_effective_tier(self, scheduler, mock_queue):
        """Boost should lower effective tier."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            priority=PriorityTier.LOW,  # tier 3
            boost_amount=BOOST_COST_PER_TIER * 2,  # +2 boost
        )

        result = await scheduler.submit_task(task)
        assert result.effective_tier == 1  # 3 - 2

    @pytest.mark.asyncio
    async def test_no_boost_no_reservation(self, scheduler, mock_credits):
        """Zero boost should not reserve credits."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
        )

        await scheduler.submit_task(task)
        mock_credits.reserve.assert_not_called()


# =============================================================================
# 3. Get Task Status
# =============================================================================


class TestGetStatus:
    """Test task status lookup."""

    @pytest.mark.asyncio
    async def test_get_existing_task(self, scheduler, mock_queue):
        """Should return task status."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-123",
                agent_id="agent-a",
                executor_id="agent-b",
                task_type="process",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_QUEUED,
            )
        )

        result = await scheduler.get_status("task-123")
        assert result is not None
        assert result.id == "task-123"

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, scheduler, mock_queue):
        """Should return None for non-existent task."""
        mock_queue.get_task = AsyncMock(return_value=None)

        result = await scheduler.get_status("nonexistent")
        assert result is None


# =============================================================================
# 4. Cancel Task
# =============================================================================


class TestCancelTask:
    """Test task cancellation."""

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self, scheduler, mock_queue):
        """Should cancel a queued task."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-123",
                agent_id="agent-a",
                executor_id="agent-b",
                task_type="process",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_QUEUED,
            )
        )
        mock_queue.cancel = AsyncMock(return_value=True)

        result = await scheduler.cancel_task("task-123", agent_id="agent-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_releases_boost_reservation(self, scheduler, mock_queue, mock_credits):
        """Cancel should release boost reservation."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-123",
                agent_id="agent-a",
                executor_id="agent-b",
                task_type="process",
                payload={},
                priority_tier=PriorityTier.LOW,
                effective_tier=1,
                enqueued_at=now,
                status=TASK_STATUS_QUEUED,
                boost_reservation_id="res-boost-456",
            )
        )
        mock_queue.cancel = AsyncMock(return_value=True)

        await scheduler.cancel_task("task-123", agent_id="agent-a")

        mock_credits.release_reservation.assert_called_once_with("res-boost-456")

    @pytest.mark.asyncio
    async def test_cancel_running_task_returns_false(self, scheduler, mock_queue):
        """Should not cancel a running task."""
        mock_queue.cancel = AsyncMock(return_value=False)

        result = await scheduler.cancel_task("task-123", agent_id="agent-a")
        assert result is False


# =============================================================================
# 5. Dequeue
# =============================================================================


class TestDequeue:
    """Test task dequeue."""

    @pytest.mark.asyncio
    async def test_dequeue_returns_task(self, scheduler, mock_queue):
        """Should return highest-priority task."""
        now = datetime.now(UTC)
        mock_queue.dequeue = AsyncMock(
            return_value=ScheduledTask(
                id="task-123",
                agent_id="agent-a",
                executor_id="agent-b",
                task_type="process",
                payload={"key": "val"},
                priority_tier=PriorityTier.HIGH,
                effective_tier=1,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
            )
        )

        task = await scheduler.dequeue_next()
        assert task is not None
        assert task.id == "task-123"

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self, scheduler, mock_queue):
        """Should return None when queue is empty."""
        mock_queue.dequeue = AsyncMock(return_value=None)

        task = await scheduler.dequeue_next()
        assert task is None


# =============================================================================
# 6. Aging Sweep
# =============================================================================


class TestAgingSweep:
    """Test aging sweep."""

    @pytest.mark.asyncio
    async def test_aging_sweep(self, scheduler, mock_queue):
        """Should delegate to queue and return count."""
        mock_queue.aging_sweep = AsyncMock(return_value=3)

        count = await scheduler.run_aging_sweep()
        assert count == 3


# =============================================================================
# 7. Validation Errors
# =============================================================================


class TestValidationErrors:
    """Test that invalid submissions are rejected."""

    @pytest.mark.asyncio
    async def test_empty_agent_id_raises(self, scheduler):
        """Should reject empty agent_id."""
        task = TaskSubmission(
            agent_id="",
            executor_id="agent-b",
            task_type="process",
        )
        with pytest.raises(ValueError, match="agent_id"):
            await scheduler.submit_task(task)

    @pytest.mark.asyncio
    async def test_negative_boost_raises(self, scheduler):
        """Should reject negative boost."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            boost_amount=Decimal("-0.01"),
        )
        with pytest.raises(ValueError, match="boost"):
            await scheduler.submit_task(task)
