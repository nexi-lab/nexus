"""Tests for Astraea-style SchedulerService integration (Issue #1274).

Tests protocol conformance, classification, fair-share rejection,
HRRN dequeue, and state event handling.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.scheduler import AgentRequest
from nexus.services.scheduler.constants import (
    TASK_STATUS_RUNNING,
    PriorityClass,
    PriorityTier,
)
from nexus.services.scheduler.events import AgentStateEmitter, AgentStateEvent
from nexus.services.scheduler.models import ScheduledTask
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_queue():
    q = AsyncMock()
    q.enqueue = AsyncMock(return_value="task-uuid-astraea")
    q.dequeue = AsyncMock(return_value=None)
    q.dequeue_hrrn = AsyncMock(return_value=None)
    q.complete = AsyncMock()
    q.cancel = AsyncMock(return_value=True)
    q.get_task = AsyncMock(return_value=None)
    q.aging_sweep = AsyncMock(return_value=0)
    q.count_running_by_agent = AsyncMock(return_value={})
    q.update_executor_state = AsyncMock()
    q.promote_starved = AsyncMock(return_value=0)
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
def fair_share():
    return FairShareCounter(default_max_concurrent=10)


@pytest.fixture
def scheduler(mock_queue, mock_pool, fair_share):
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        fair_share=fair_share,
        use_hrrn=True,
    )


# =============================================================================
# Protocol Conformance
# =============================================================================


class TestProtocolConformance:
    """Verify all 8 protocol methods exist."""

    def test_has_submit(self, scheduler):
        assert hasattr(scheduler, "submit")

    def test_has_next(self, scheduler):
        assert hasattr(scheduler, "next")

    def test_has_pending_count(self, scheduler):
        assert hasattr(scheduler, "pending_count")

    def test_has_cancel(self, scheduler):
        assert hasattr(scheduler, "cancel")

    def test_has_get_status(self, scheduler):
        assert hasattr(scheduler, "get_status")

    def test_has_complete(self, scheduler):
        assert hasattr(scheduler, "complete")

    def test_has_classify(self, scheduler):
        assert hasattr(scheduler, "classify")

    def test_has_metrics(self, scheduler):
        assert hasattr(scheduler, "metrics")


# =============================================================================
# Classification
# =============================================================================


class TestClassify:
    """Test request classification via protocol."""

    @pytest.mark.asyncio
    async def test_classify_normal_is_batch(self, scheduler):
        req = AgentRequest(agent_id="a", zone_id=None, priority=2)
        result = await scheduler.classify(req)
        assert result == PriorityClass.BATCH

    @pytest.mark.asyncio
    async def test_classify_high_is_interactive(self, scheduler):
        req = AgentRequest(agent_id="a", zone_id=None, priority=1)
        result = await scheduler.classify(req)
        assert result == PriorityClass.INTERACTIVE

    @pytest.mark.asyncio
    async def test_classify_low_io_wait_promoted(self, scheduler):
        req = AgentRequest(agent_id="a", zone_id=None, priority=3, request_state="io_wait")
        result = await scheduler.classify(req)
        assert result == PriorityClass.BATCH


# =============================================================================
# Submit with Auto-Classification
# =============================================================================


class TestSubmitAutoClassify:
    """Test that submit auto-classifies priority_class."""

    @pytest.mark.asyncio
    async def test_submit_returns_task_id(self, scheduler, mock_queue):
        req = AgentRequest(
            agent_id="agent-a",
            zone_id=None,
            priority=2,
            executor_id="exec-1",
            task_type="compute",
        )
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-astraea"

    @pytest.mark.asyncio
    async def test_submit_auto_classifies(self, scheduler, mock_queue):
        req = AgentRequest(
            agent_id="agent-a",
            zone_id=None,
            priority=0,
            executor_id="exec-1",
            task_type="urgent",
        )
        await scheduler.submit(req)
        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["priority_class"] == "interactive"


# =============================================================================
# Fair-Share Rejection
# =============================================================================


class TestFairShareRejection:
    """Test that tasks are rejected when agent is at capacity."""

    @pytest.mark.asyncio
    async def test_submit_rejected_at_capacity(self, mock_queue, mock_pool):
        fs = FairShareCounter(default_max_concurrent=1)
        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool, fair_share=fs, use_hrrn=True)

        # Fill up the agent's capacity
        fs.record_start("exec-1")

        req = AgentRequest(
            agent_id="agent-a",
            zone_id=None,
            priority=2,
            executor_id="exec-1",
            task_type="compute",
        )
        with pytest.raises(ValueError, match="at capacity"):
            await svc.submit(req)


# =============================================================================
# HRRN Dequeue
# =============================================================================


class TestHrrnDequeue:
    """Test HRRN dequeue selection."""

    @pytest.mark.asyncio
    async def test_dequeue_uses_hrrn_when_enabled(self, scheduler, mock_queue):
        now = datetime.now(UTC)
        mock_queue.dequeue_hrrn = AsyncMock(
            return_value=ScheduledTask(
                id="task-hrrn",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
                priority_class="batch",
            )
        )

        task = await scheduler.dequeue_next()
        assert task is not None
        assert task.id == "task-hrrn"
        mock_queue.dequeue_hrrn.assert_called_once()
        mock_queue.dequeue.assert_not_called()

    @pytest.mark.asyncio
    async def test_dequeue_falls_back_when_hrrn_disabled(self, mock_queue, mock_pool, fair_share):
        svc = SchedulerService(
            queue=mock_queue, db_pool=mock_pool, fair_share=fair_share, use_hrrn=False
        )
        now = datetime.now(UTC)
        mock_queue.dequeue = AsyncMock(
            return_value=ScheduledTask(
                id="task-classic",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
            )
        )

        task = await svc.dequeue_next()
        assert task is not None
        assert task.id == "task-classic"
        mock_queue.dequeue.assert_called_once()

    @pytest.mark.asyncio
    async def test_dequeue_updates_fair_share(self, scheduler, mock_queue, fair_share):
        now = datetime.now(UTC)
        mock_queue.dequeue_hrrn = AsyncMock(
            return_value=ScheduledTask(
                id="task-fs",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
            )
        )

        await scheduler.dequeue_next()
        assert fair_share.snapshot("agent-a").running_count == 1


# =============================================================================
# Agent State Events
# =============================================================================


class TestAgentStateEvents:
    """Test state event handling."""

    @pytest.mark.asyncio
    async def test_state_event_updates_executor_state(self, mock_queue, mock_pool):
        emitter = AgentStateEmitter()
        SchedulerService(queue=mock_queue, db_pool=mock_pool, state_emitter=emitter)

        event = AgentStateEvent(
            agent_id="agent-1",
            previous_state="IDLE",
            new_state="CONNECTED",
            generation=2,
        )
        await emitter.emit(event)

        mock_queue.update_executor_state.assert_called_once_with(
            mock_pool.acquire().__aenter__.return_value,
            "agent-1",
            "CONNECTED",
        )

    @pytest.mark.asyncio
    async def test_sync_fair_share(self, scheduler, mock_queue, fair_share):
        mock_queue.count_running_by_agent = AsyncMock(return_value={"agent-a": 3, "agent-b": 1})
        await scheduler.sync_fair_share()

        assert fair_share.snapshot("agent-a").running_count == 3
        assert fair_share.snapshot("agent-b").running_count == 1


# =============================================================================
# Complete with Fair-Share
# =============================================================================


class TestTwoPhaseInit:
    """Test two-phase initialization pattern (Issue #2195)."""

    def test_uninitialized_pool_raises_runtime_error(self):
        """Accessing pool before initialize() raises RuntimeError."""
        svc = SchedulerService(queue=AsyncMock(), db_pool=None)
        assert svc._initialized is False
        with pytest.raises(RuntimeError, match="initialize"):
            _ = svc.pool

    @pytest.mark.asyncio
    async def test_initialize_sets_pool(self, mock_pool, mock_queue):
        """initialize() sets the pool and marks as initialized."""
        svc = SchedulerService(queue=mock_queue, db_pool=None)
        await svc.initialize(mock_pool)
        assert svc._initialized is True
        assert svc.pool is mock_pool

    def test_pool_property_succeeds_when_initialized(self, mock_pool, mock_queue):
        """Pool property succeeds when db_pool is provided at construction."""
        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool)
        assert svc._initialized is True
        assert svc.pool is mock_pool

    @pytest.mark.asyncio
    async def test_initialize_calls_sync_fair_share(self, mock_pool, mock_queue):
        """initialize() calls sync_fair_share to hydrate counters from DB."""
        svc = SchedulerService(queue=mock_queue, db_pool=None)
        await svc.initialize(mock_pool)
        mock_queue.count_running_by_agent.assert_awaited_once()


class TestCompleteWithFairShare:
    """Test that complete updates fair-share counters."""

    @pytest.mark.asyncio
    async def test_complete_decrements_fair_share(self, scheduler, mock_queue, fair_share):
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-done",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
            )
        )

        fair_share.record_start("agent-a")
        assert fair_share.snapshot("agent-a").running_count == 1

        await scheduler.complete("task-done")
        assert fair_share.snapshot("agent-a").running_count == 0
