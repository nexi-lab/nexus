"""Tests for owner-scoped scheduler operations.

Verifies that:
1. get_task_scoped() only returns tasks owned by the requesting agent
2. cancel_scoped() only cancels tasks owned by the requesting agent
3. get_status_scoped() returns None for tasks owned by other agents
4. cancel_by_id_scoped() returns False for tasks owned by other agents
5. Cross-user isolation: agent A cannot see/cancel agent B's tasks

Related: Security fix for task visibility and cancellation.
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.scheduler.constants import PriorityTier
from nexus.services.scheduler.models import ScheduledTask
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.queue import TaskQueue
from nexus.services.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


def _make_task(
    *,
    task_id: str = "task-1",
    agent_id: str = "agent-a",
    executor_id: str = "exec-1",
    status: str = "queued",
    boost_reservation_id: str | None = None,
) -> ScheduledTask:
    """Create a ScheduledTask with sensible defaults."""
    return ScheduledTask(
        id=task_id,
        agent_id=agent_id,
        executor_id=executor_id,
        task_type="test",
        payload={"action": "noop"},
        priority_tier=PriorityTier.NORMAL,
        effective_tier=2,
        enqueued_at=datetime.now(UTC),
        status=status,
        priority_class="batch",
        request_state="pending",
        boost_reservation_id=boost_reservation_id,
    )


@pytest.fixture
def mock_queue():
    q = AsyncMock()
    q.get_task = AsyncMock(return_value=None)
    q.get_task_scoped = AsyncMock(return_value=None)
    q.cancel = AsyncMock(return_value=True)
    q.cancel_scoped = AsyncMock(return_value=True)
    q.enqueue = AsyncMock(return_value="task-new")
    q.complete = AsyncMock()
    q.count_running_by_agent = AsyncMock(return_value={})
    q.get_queue_metrics = AsyncMock(return_value=[])
    return q


@pytest.fixture
def mock_conn():
    return AsyncMock()


@pytest.fixture
def mock_pool(mock_conn):
    pool = MagicMock()
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=mock_conn)
    acm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acm)
    return pool


@pytest.fixture
def scheduler(mock_queue, mock_pool):
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        fair_share=FairShareCounter(default_max_concurrent=10),
        use_hrrn=True,
    )


# =============================================================================
# TaskQueue.get_task_scoped
# =============================================================================


class TestGetTaskScoped:
    """Test TaskQueue.get_task_scoped() owner filtering."""

    @pytest.mark.asyncio
    async def test_returns_task_when_agent_matches(self, mock_conn):
        """Task is returned when agent_id matches."""
        task = _make_task(agent_id="agent-a")
        mock_conn.fetchrow = AsyncMock(
            return_value={
                "id": task.id,
                "agent_id": task.agent_id,
                "executor_id": task.executor_id,
                "task_type": task.task_type,
                "payload": '{"action": "noop"}',
                "priority_tier": task.priority_tier.value,
                "effective_tier": task.effective_tier,
                "enqueued_at": task.enqueued_at,
                "status": task.status,
                "deadline": None,
                "boost_amount": Decimal("0"),
                "boost_tiers": 0,
                "boost_reservation_id": None,
                "started_at": None,
                "completed_at": None,
                "error_message": None,
                "zone_id": "root",
                "idempotency_key": None,
                "request_state": "pending",
                "priority_class": "batch",
                "executor_state": "UNKNOWN",
                "estimated_service_time": 30.0,
            }
        )

        q = TaskQueue()
        result = await q.get_task_scoped(mock_conn, "task-1", "agent-a")
        assert result is not None
        assert result.id == "task-1"
        assert result.agent_id == "agent-a"

    @pytest.mark.asyncio
    async def test_returns_none_when_agent_mismatch(self, mock_conn):
        """Task is NOT returned when agent_id doesn't match (DB filters it)."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        q = TaskQueue()
        result = await q.get_task_scoped(mock_conn, "task-1", "agent-b")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent_task(self, mock_conn):
        """None returned for nonexistent task ID."""
        mock_conn.fetchrow = AsyncMock(return_value=None)

        q = TaskQueue()
        result = await q.get_task_scoped(mock_conn, "no-such-task", "agent-a")
        assert result is None


# =============================================================================
# TaskQueue.cancel_scoped
# =============================================================================


class TestCancelScoped:
    """Test TaskQueue.cancel_scoped() owner-filtered cancellation."""

    @pytest.mark.asyncio
    async def test_cancels_when_agent_matches(self, mock_conn):
        """Task is cancelled when agent_id matches."""
        mock_conn.fetchval = AsyncMock(return_value="cancelled")

        q = TaskQueue()
        result = await q.cancel_scoped(mock_conn, "task-1", "agent-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_fails_when_agent_mismatch(self, mock_conn):
        """Cancel fails when agent_id doesn't match."""
        mock_conn.fetchval = AsyncMock(return_value=None)

        q = TaskQueue()
        result = await q.cancel_scoped(mock_conn, "task-1", "agent-b")
        assert result is False

    @pytest.mark.asyncio
    async def test_fails_for_running_task(self, mock_conn):
        """Cancel fails for tasks that are already running (not queued)."""
        mock_conn.fetchval = AsyncMock(return_value=None)

        q = TaskQueue()
        result = await q.cancel_scoped(mock_conn, "running-task", "agent-a")
        assert result is False


# =============================================================================
# SchedulerService.get_status_scoped
# =============================================================================


class TestGetStatusScoped:
    """Test SchedulerService.get_status_scoped() owner isolation."""

    @pytest.mark.asyncio
    async def test_returns_status_for_owner(self, scheduler, mock_queue):
        """Status dict returned when agent is the task owner."""
        task = _make_task(agent_id="agent-a")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)

        status = await scheduler.get_status_scoped("task-1", agent_id="agent-a")
        assert status is not None
        assert status["id"] == "task-1"
        assert status["agent_id"] == "agent-a"
        assert status["status"] == "queued"
        assert status["priority_tier"] == "normal"
        assert status["priority_class"] == "batch"

    @pytest.mark.asyncio
    async def test_returns_none_for_non_owner(self, scheduler, mock_queue):
        """None returned when agent is NOT the task owner."""
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        status = await scheduler.get_status_scoped("task-1", agent_id="agent-b")
        assert status is None

    @pytest.mark.asyncio
    async def test_returns_none_for_nonexistent(self, scheduler, mock_queue):
        """None returned for nonexistent task."""
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        status = await scheduler.get_status_scoped("no-task", agent_id="agent-a")
        assert status is None

    @pytest.mark.asyncio
    async def test_status_contains_all_fields(self, scheduler, mock_queue):
        """Status dict includes all expected fields."""
        task = _make_task(agent_id="agent-a")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)

        status = await scheduler.get_status_scoped("task-1", agent_id="agent-a")
        expected_keys = {
            "id",
            "status",
            "agent_id",
            "executor_id",
            "task_type",
            "priority_tier",
            "effective_tier",
            "priority_class",
            "request_state",
            "enqueued_at",
            "started_at",
            "completed_at",
            "deadline",
            "boost_amount",
            "error_message",
        }
        assert set(status.keys()) == expected_keys


# =============================================================================
# SchedulerService.cancel_by_id_scoped
# =============================================================================


class TestCancelByIdScoped:
    """Test SchedulerService.cancel_by_id_scoped() owner isolation."""

    @pytest.mark.asyncio
    async def test_cancels_for_owner(self, scheduler, mock_queue):
        """Task cancelled when agent is the task owner."""
        task = _make_task(agent_id="agent-a")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)
        mock_queue.cancel_scoped = AsyncMock(return_value=True)

        result = await scheduler.cancel_by_id_scoped("task-1", agent_id="agent-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_fails_for_non_owner(self, scheduler, mock_queue):
        """Cancel fails when agent is NOT the task owner."""
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        result = await scheduler.cancel_by_id_scoped("task-1", agent_id="agent-b")
        assert result is False
        mock_queue.cancel_scoped.assert_not_called()

    @pytest.mark.asyncio
    async def test_fails_for_nonexistent(self, scheduler, mock_queue):
        """Cancel fails for nonexistent task."""
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        result = await scheduler.cancel_by_id_scoped("no-task", agent_id="agent-a")
        assert result is False

    @pytest.mark.asyncio
    async def test_releases_boost_reservation(self, scheduler, mock_queue):
        """Boost reservation is released on successful scoped cancel."""
        credits_mock = AsyncMock()
        scheduler._credits = credits_mock

        task = _make_task(agent_id="agent-a", boost_reservation_id="res-123")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)
        mock_queue.cancel_scoped = AsyncMock(return_value=True)

        result = await scheduler.cancel_by_id_scoped("task-1", agent_id="agent-a")
        assert result is True
        credits_mock.release_reservation.assert_awaited_once_with("res-123")

    @pytest.mark.asyncio
    async def test_no_release_when_no_reservation(self, scheduler, mock_queue):
        """No credit release when task has no boost reservation."""
        credits_mock = AsyncMock()
        scheduler._credits = credits_mock

        task = _make_task(agent_id="agent-a", boost_reservation_id=None)
        mock_queue.get_task_scoped = AsyncMock(return_value=task)
        mock_queue.cancel_scoped = AsyncMock(return_value=True)

        result = await scheduler.cancel_by_id_scoped("task-1", agent_id="agent-a")
        assert result is True
        credits_mock.release_reservation.assert_not_called()


# =============================================================================
# Cross-User Isolation (Security)
# =============================================================================


class TestCrossUserIsolation:
    """Verify that agent A cannot read or cancel agent B's tasks.

    This is the core security test: tasks are isolated per owner.
    """

    @pytest.mark.asyncio
    async def test_agent_b_cannot_read_agent_a_task(self, scheduler, mock_queue):
        """Agent B gets None when trying to read Agent A's task."""
        # Agent A owns the task
        agent_a_task = _make_task(agent_id="agent-a", task_id="secret-task")
        # get_task (unscoped) returns the task
        mock_queue.get_task = AsyncMock(return_value=agent_a_task)
        # get_task_scoped returns None because agent-b is not the owner
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        # Agent B tries to read it
        status = await scheduler.get_status_scoped("secret-task", agent_id="agent-b")
        assert status is None

        # Verify scoped query was called with agent-b
        mock_queue.get_task_scoped.assert_called_once()
        call_args = mock_queue.get_task_scoped.call_args
        assert call_args[0][1] == "secret-task"
        assert call_args[0][2] == "agent-b"

    @pytest.mark.asyncio
    async def test_agent_b_cannot_cancel_agent_a_task(self, scheduler, mock_queue):
        """Agent B cannot cancel Agent A's task."""
        # Scoped lookup returns None for agent-b
        mock_queue.get_task_scoped = AsyncMock(return_value=None)

        result = await scheduler.cancel_by_id_scoped("secret-task", agent_id="agent-b")
        assert result is False
        mock_queue.cancel_scoped.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_a_can_read_own_task(self, scheduler, mock_queue):
        """Agent A CAN read their own task."""
        task = _make_task(agent_id="agent-a", task_id="my-task")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)

        status = await scheduler.get_status_scoped("my-task", agent_id="agent-a")
        assert status is not None
        assert status["id"] == "my-task"

    @pytest.mark.asyncio
    async def test_agent_a_can_cancel_own_task(self, scheduler, mock_queue):
        """Agent A CAN cancel their own task."""
        task = _make_task(agent_id="agent-a", task_id="my-task")
        mock_queue.get_task_scoped = AsyncMock(return_value=task)
        mock_queue.cancel_scoped = AsyncMock(return_value=True)

        result = await scheduler.cancel_by_id_scoped("my-task", agent_id="agent-a")
        assert result is True

    @pytest.mark.asyncio
    async def test_unscoped_methods_still_work(self, scheduler, mock_queue):
        """Unscoped get_status still returns tasks regardless of owner.

        This is used internally (e.g. submit→get_status after enqueue).
        """
        task = _make_task(agent_id="agent-a")
        mock_queue.get_task = AsyncMock(return_value=task)

        status = await scheduler.get_status("task-1")
        assert status is not None
        assert status["agent_id"] == "agent-a"


# =============================================================================
# Router-Level _extract_agent_id
# =============================================================================


class TestExtractAgentId:
    """Test the _extract_agent_id helper used by the router."""

    def test_extracts_x_agent_id(self):
        from nexus.server.api.v2.routers.scheduler import _extract_agent_id

        result = _extract_agent_id({"x_agent_id": "agent-from-header", "subject_id": "fallback"})
        assert result == "agent-from-header"

    def test_falls_back_to_subject_id(self):
        from nexus.server.api.v2.routers.scheduler import _extract_agent_id

        result = _extract_agent_id({"subject_id": "agent-from-subject"})
        assert result == "agent-from-subject"

    def test_defaults_to_anonymous(self):
        from nexus.server.api.v2.routers.scheduler import _extract_agent_id

        result = _extract_agent_id({})
        assert result == "anonymous"

    def test_ignores_empty_x_agent_id(self):
        from nexus.server.api.v2.routers.scheduler import _extract_agent_id

        result = _extract_agent_id({"x_agent_id": "", "subject_id": "fallback"})
        assert result == "fallback"
