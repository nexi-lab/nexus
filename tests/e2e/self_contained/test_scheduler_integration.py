"""Integration tests for the scheduler system.

Tests the full flow with mocked queue and CreditsService(enabled=False).
These tests verify the submit -> queue -> dequeue -> complete flow
without requiring PostgreSQL or TigerBeetle.

Test categories:
1. Full submit-dequeue-complete flow
2. Boost with credits integration
3. Cancel with credits release
4. Aging progression
5. Priority ordering

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.pay.credits import CreditsService
from nexus.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    BOOST_COST_PER_TIER,
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    PriorityTier,
)
from nexus.scheduler.models import ScheduledTask, TaskSubmission
from nexus.scheduler.priority import compute_effective_tier
from nexus.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def disabled_credits():
    """CreditsService in disabled mode (no TigerBeetle needed)."""
    return CreditsService(enabled=False)


@pytest.fixture
def mock_queue():
    """Mock TaskQueue that simulates in-memory queueing."""
    queue = AsyncMock()
    queue.enqueue = AsyncMock(return_value="task-int-001")
    queue.dequeue = AsyncMock(return_value=None)
    queue.complete = AsyncMock()
    queue.cancel = AsyncMock(return_value=True)
    queue.get_task = AsyncMock(return_value=None)
    queue.aging_sweep = AsyncMock(return_value=0)
    return queue


@pytest.fixture
def mock_pool():
    """Mock DB pool."""
    pool = MagicMock()
    conn = AsyncMock()
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=conn)
    acm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acm)
    return pool


@pytest.fixture
def scheduler(mock_queue, mock_pool, disabled_credits):
    """SchedulerService with disabled CreditsService."""
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        credits_service=disabled_credits,
    )


# =============================================================================
# 1. Full Submit-Dequeue-Complete Flow
# =============================================================================


class TestFullFlow:
    """Test complete task lifecycle."""

    @pytest.mark.asyncio
    async def test_submit_returns_queued_task(self, scheduler):
        """Submit should return a ScheduledTask with queued status."""
        submission = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            payload={"data": "test"},
        )

        task = await scheduler.submit_task(submission)

        assert task.id == "task-int-001"
        assert task.status == TASK_STATUS_QUEUED
        assert task.agent_id == "agent-a"
        assert task.executor_id == "agent-b"
        assert task.task_type == "process"
        assert task.payload == {"data": "test"}

    @pytest.mark.asyncio
    async def test_submit_dequeue_flow(self, scheduler, mock_queue):
        """Submit then dequeue should return the task."""
        submission = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
        )

        submitted = await scheduler.submit_task(submission)

        # Simulate dequeue returning the submitted task
        now = datetime.now(UTC)
        mock_queue.dequeue = AsyncMock(
            return_value=ScheduledTask(
                id=submitted.id,
                agent_id="agent-a",
                executor_id="agent-b",
                task_type="process",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
            )
        )

        dequeued = await scheduler.dequeue_next()
        assert dequeued is not None
        assert dequeued.id == submitted.id
        assert dequeued.status == TASK_STATUS_RUNNING


# =============================================================================
# 2. Boost with Credits Integration
# =============================================================================


class TestBoostIntegration:
    """Test boost with CreditsService (disabled mode)."""

    @pytest.mark.asyncio
    async def test_boost_with_disabled_credits(self, scheduler):
        """Boost should work with disabled CreditsService (pass-through)."""
        submission = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            priority=PriorityTier.LOW,
            boost_amount=BOOST_COST_PER_TIER * 2,
        )

        task = await scheduler.submit_task(submission)

        # Effective tier should be lowered by boost
        assert task.effective_tier == 1  # 3 (LOW) - 2 (boost) = 1 (HIGH)
        assert task.boost_tiers == 2


# =============================================================================
# 3. Cancel Flow
# =============================================================================


class TestCancelFlow:
    """Test cancel with credits release."""

    @pytest.mark.asyncio
    async def test_cancel_queued_task(self, scheduler, mock_queue):
        """Should cancel a queued task."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-cancel-1",
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

        result = await scheduler.cancel_task("task-cancel-1", agent_id="agent-a")
        assert result is True


# =============================================================================
# 4. Aging Progression
# =============================================================================


class TestAgingProgression:
    """Test priority aging over time."""

    def test_aging_increases_priority_over_time(self):
        """Tasks should gain priority as they wait."""
        task = TaskSubmission(
            agent_id="agent-a",
            executor_id="agent-b",
            task_type="process",
            priority=PriorityTier.LOW,  # tier 3
        )

        now = datetime.now(UTC)
        fresh_tier = compute_effective_tier(task, enqueued_at=now, now=now)
        assert fresh_tier == 3  # No aging

        aged_1 = compute_effective_tier(
            task,
            enqueued_at=now - timedelta(seconds=AGING_THRESHOLD_SECONDS + 1),
            now=now,
        )
        assert aged_1 == 2  # 3 - 1 = NORMAL

        aged_2 = compute_effective_tier(
            task,
            enqueued_at=now - timedelta(seconds=AGING_THRESHOLD_SECONDS * 2 + 1),
            now=now,
        )
        assert aged_2 == 1  # 3 - 2 = HIGH

        aged_3 = compute_effective_tier(
            task,
            enqueued_at=now - timedelta(seconds=AGING_THRESHOLD_SECONDS * 3 + 1),
            now=now,
        )
        assert aged_3 == 0  # 3 - 3 = CRITICAL (clamped)


# =============================================================================
# 5. Priority Ordering
# =============================================================================


class TestPriorityOrdering:
    """Test that tasks are ordered correctly by effective tier."""

    def test_effective_tier_determines_order(self):
        """Lower effective_tier should run first."""
        now = datetime.now(UTC)
        tasks = [
            TaskSubmission(agent_id="a", executor_id="b", task_type="t", priority=PriorityTier.LOW),
            TaskSubmission(
                agent_id="a", executor_id="b", task_type="t", priority=PriorityTier.CRITICAL
            ),
            TaskSubmission(
                agent_id="a", executor_id="b", task_type="t", priority=PriorityTier.NORMAL
            ),
        ]

        tiers = [compute_effective_tier(t, enqueued_at=now, now=now) for t in tasks]
        sorted_tiers = sorted(tiers)

        assert sorted_tiers == [0, 2, 3]  # CRITICAL, NORMAL, LOW

    def test_boost_changes_order(self):
        """Boosted LOW should outrank unboosted NORMAL."""
        now = datetime.now(UTC)

        low_boosted = TaskSubmission(
            agent_id="a",
            executor_id="b",
            task_type="t",
            priority=PriorityTier.LOW,
            boost_amount=BOOST_COST_PER_TIER * 2,
        )
        normal_no_boost = TaskSubmission(
            agent_id="a",
            executor_id="b",
            task_type="t",
            priority=PriorityTier.NORMAL,
        )

        boosted_tier = compute_effective_tier(low_boosted, enqueued_at=now, now=now)
        normal_tier = compute_effective_tier(normal_no_boost, enqueued_at=now, now=now)

        # LOW(3) - 2 boost = 1 < NORMAL(2)
        assert boosted_tier < normal_tier
