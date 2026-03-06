"""Tests for Phase 3 scheduler features (Issue #2761).

Tests batch dequeue, eviction with worker immunity, LISTEN health
monitoring, and adaptive poll interval.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.scheduler.dispatcher import (
    _LISTEN_HEALTH_CHECK_INTERVAL,
    _LISTEN_UNHEALTHY_POLL_INTERVAL,
    TaskDispatcher,
)
from nexus.services.scheduler.queue import TaskQueue

# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture()
def queue() -> TaskQueue:
    return TaskQueue()


@pytest.fixture()
def mock_conn() -> AsyncMock:
    return AsyncMock()


@pytest.fixture()
def mock_scheduler() -> MagicMock:
    svc = MagicMock()
    svc.dequeue_next = AsyncMock(return_value=None)
    svc.run_aging_sweep = AsyncMock(return_value=0)
    svc.run_starvation_promotion = AsyncMock(return_value=0)
    return svc


# ======================================================================
# Batch dequeue tests
# ======================================================================


class TestBatchDequeue:
    """Test dequeue_batch() for fan-out delegation."""

    async def test_batch_dequeue_returns_tasks(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch returns list of tasks from fetched rows."""
        mock_conn.fetch.return_value = [
            {
                "id": "1",
                "agent_id": "a1",
                "executor_id": "e1",
                "task_type": "default",
                "payload": "{}",
                "priority_tier": 2,
                "effective_tier": 2,
                "enqueued_at": "2024-01-01T00:00:00",
                "status": "running",
                "deadline": None,
                "boost_amount": 0,
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
            },
            {
                "id": "2",
                "agent_id": "a2",
                "executor_id": "e2",
                "task_type": "default",
                "payload": "{}",
                "priority_tier": 2,
                "effective_tier": 2,
                "enqueued_at": "2024-01-01T00:00:01",
                "status": "running",
                "deadline": None,
                "boost_amount": 0,
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
            },
        ]

        tasks = await queue.dequeue_batch(mock_conn, 5)
        assert len(tasks) == 2
        assert tasks[0].id == "1"
        assert tasks[1].id == "2"
        mock_conn.fetch.assert_called_once()
        # batch_size passed as parameter
        assert mock_conn.fetch.call_args.args[1] == 5

    async def test_batch_dequeue_empty(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch returns empty list when no tasks available."""
        mock_conn.fetch.return_value = []
        tasks = await queue.dequeue_batch(mock_conn, 10)
        assert tasks == []

    async def test_batch_dequeue_zero_size(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch with batch_size=0 returns empty list (no DB call)."""
        tasks = await queue.dequeue_batch(mock_conn, 0)
        assert tasks == []
        mock_conn.fetch.assert_not_called()

    async def test_batch_dequeue_negative_size(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch with negative batch_size returns empty list."""
        tasks = await queue.dequeue_batch(mock_conn, -1)
        assert tasks == []
        mock_conn.fetch.assert_not_called()

    async def test_batch_dequeue_hrrn_mode(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch with use_hrrn=True uses HRRN SQL."""
        mock_conn.fetch.return_value = []
        await queue.dequeue_batch(mock_conn, 3, use_hrrn=True)
        mock_conn.fetch.assert_called_once()
        # HRRN SQL contains "executor_state" filter and HRRN scoring
        sql = mock_conn.fetch.call_args.args[0]
        assert "executor_state" in sql
        assert "GREATEST" in sql

    async def test_batch_dequeue_classic_mode(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """dequeue_batch with use_hrrn=False uses classic tier SQL."""
        mock_conn.fetch.return_value = []
        await queue.dequeue_batch(mock_conn, 3, use_hrrn=False)
        mock_conn.fetch.assert_called_once()
        sql = mock_conn.fetch.call_args.args[0]
        # Classic SQL orders by effective_tier, enqueued_at
        assert "effective_tier ASC" in sql
        assert "enqueued_at ASC" in sql


# ======================================================================
# Eviction with worker immunity tests
# ======================================================================


class TestEviction:
    """Test evict_lowest_priority() with worker immunity."""

    async def test_evict_returns_cancelled_ids(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """Eviction returns list of cancelled task IDs."""
        mock_conn.fetch.return_value = [{"id": "t1"}, {"id": "t2"}]
        evicted = await queue.evict_lowest_priority(mock_conn, 5)
        assert evicted == ["t1", "t2"]

    async def test_evict_empty_queue(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """Eviction on empty queue returns empty list."""
        mock_conn.fetch.return_value = []
        evicted = await queue.evict_lowest_priority(mock_conn, 5)
        assert evicted == []

    async def test_evict_zero_count(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """Eviction with count=0 returns empty list (no DB call)."""
        evicted = await queue.evict_lowest_priority(mock_conn, 0)
        assert evicted == []
        mock_conn.fetch.assert_not_called()

    async def test_evict_with_immune_pids(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """Immune PIDs are passed to the SQL query."""
        mock_conn.fetch.return_value = []
        await queue.evict_lowest_priority(
            mock_conn,
            3,
            immune_pids=["pid-1", "pid-2"],
        )
        mock_conn.fetch.assert_called_once()
        # immune_pids is $2 parameter
        args = mock_conn.fetch.call_args.args
        assert args[1] == 3  # count
        assert args[2] == ["pid-1", "pid-2"]  # immune_pids

    async def test_evict_no_immune_pids_passes_empty(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """When no immune_pids, empty list is passed."""
        mock_conn.fetch.return_value = []
        await queue.evict_lowest_priority(mock_conn, 3)
        args = mock_conn.fetch.call_args.args
        assert args[2] == []  # empty immune_pids

    async def test_evict_negative_count(
        self,
        queue: TaskQueue,
        mock_conn: AsyncMock,
    ) -> None:
        """Eviction with negative count returns empty list."""
        evicted = await queue.evict_lowest_priority(mock_conn, -1)
        assert evicted == []
        mock_conn.fetch.assert_not_called()


# ======================================================================
# LISTEN health monitoring + adaptive poll tests
# ======================================================================


class TestDispatcherHealth:
    """Test LISTEN health monitoring and adaptive poll interval."""

    def test_default_listen_unhealthy(self, mock_scheduler: MagicMock) -> None:
        """Dispatcher starts with LISTEN unhealthy."""
        dispatcher = TaskDispatcher(mock_scheduler, poll_interval=30)
        assert not dispatcher.listen_healthy

    def test_effective_poll_when_unhealthy(self, mock_scheduler: MagicMock) -> None:
        """When LISTEN is unhealthy, poll interval is 2 seconds."""
        dispatcher = TaskDispatcher(mock_scheduler, poll_interval=30)
        assert dispatcher.effective_poll_interval == _LISTEN_UNHEALTHY_POLL_INTERVAL

    def test_effective_poll_when_healthy(self, mock_scheduler: MagicMock) -> None:
        """When LISTEN is healthy, poll interval is the configured value."""
        dispatcher = TaskDispatcher(mock_scheduler, poll_interval=30)
        dispatcher._listen_healthy = True
        assert dispatcher.effective_poll_interval == 30.0

    def test_health_check_interval_configured(self) -> None:
        """LISTEN health check interval is 10 seconds."""
        assert _LISTEN_HEALTH_CHECK_INTERVAL == 10

    def test_unhealthy_poll_interval_configured(self) -> None:
        """Unhealthy poll fallback is 2 seconds."""
        assert _LISTEN_UNHEALTHY_POLL_INTERVAL == 2

    def test_custom_poll_interval(self, mock_scheduler: MagicMock) -> None:
        """Custom poll_interval is used when LISTEN is healthy."""
        dispatcher = TaskDispatcher(mock_scheduler, poll_interval=60)
        dispatcher._listen_healthy = True
        assert dispatcher.effective_poll_interval == 60.0

    async def test_listen_loop_no_record_store(
        self,
        mock_scheduler: MagicMock,
    ) -> None:
        """_listen_loop exits immediately when no record_store."""
        dispatcher = TaskDispatcher(mock_scheduler)
        await dispatcher._listen_loop()
        assert not dispatcher.listen_healthy


# ======================================================================
# Service-level batch dequeue tests
# ======================================================================


class TestServiceBatchDequeue:
    """Test SchedulerService.dequeue_batch() integration."""

    async def test_dequeue_batch_delegates_to_queue(self) -> None:
        """Service.dequeue_batch passes through to queue.dequeue_batch."""
        from nexus.services.scheduler.service import SchedulerService

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        svc = SchedulerService(db_pool=mock_pool, use_hrrn=True)
        mock_conn.fetch.return_value = []

        tasks = await svc.dequeue_batch(5)
        assert tasks == []

    async def test_dequeue_batch_zero(self) -> None:
        """Service.dequeue_batch with 0 returns empty list."""
        from nexus.services.scheduler.service import SchedulerService

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        svc = SchedulerService(db_pool=mock_pool)
        tasks = await svc.dequeue_batch(0)
        assert tasks == []


# ======================================================================
# Service-level eviction tests
# ======================================================================


class TestServiceEviction:
    """Test SchedulerService.evict_lowest_priority() integration."""

    async def test_evict_delegates_to_queue(self) -> None:
        """Service.evict_lowest_priority passes through to queue."""
        from nexus.services.scheduler.service import SchedulerService

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_conn.fetch.return_value = [{"id": "evicted-1"}]

        svc = SchedulerService(db_pool=mock_pool)
        evicted = await svc.evict_lowest_priority(3, immune_pids=["safe-1"])
        assert evicted == ["evicted-1"]

    async def test_evict_with_no_immune_pids(self) -> None:
        """Service.evict_lowest_priority with no immune_pids."""
        from nexus.services.scheduler.service import SchedulerService

        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_conn.fetch.return_value = []

        svc = SchedulerService(db_pool=mock_pool)
        evicted = await svc.evict_lowest_priority(5)
        assert evicted == []
