"""Tests for overlap policies and idempotency behavior (Issue #2749).

Covers:
- Baseline UPSERT/idempotency behavior
- SKIP policy: reject when running task matches
- CANCEL_PREVIOUS policy: cancel old + enqueue new in transaction
- ALLOW policy: standard UPSERT behavior
- Short-circuit when idempotency_key is None
- cancel_by_id credit release branches
"""

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.scheduler import AgentRequest
from nexus.services.scheduler.constants import TASK_STATUS_RUNNING, PriorityTier
from nexus.services.scheduler.exceptions import (
    CapacityExceeded,
    RateLimitExceeded,
    TaskAlreadyRunning,
)
from nexus.services.scheduler.models import ScheduledTask
from nexus.services.scheduler.policies.admission import AdmissionPolicy
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.policies.rate_limiter import TokenBucketLimiter
from nexus.services.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_queue():
    q = AsyncMock()
    q.enqueue = AsyncMock(return_value="task-uuid-new")
    q.enqueue_skip = AsyncMock(return_value="task-uuid-skip")
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
    q.find_by_idempotency_key = AsyncMock(return_value=None)
    q.cancel_running_by_idempotency_key = AsyncMock(return_value=(None, None))
    return q


@pytest.fixture
def mock_conn():
    conn = AsyncMock()
    # Add transaction context manager support
    tx = AsyncMock()
    tx.__aenter__ = AsyncMock(return_value=None)
    tx.__aexit__ = AsyncMock(return_value=None)
    conn.transaction = MagicMock(return_value=tx)
    return conn


@pytest.fixture
def mock_pool(mock_conn):
    pool = MagicMock()
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=mock_conn)
    acm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acm)
    return pool


@pytest.fixture
def mock_credits():
    credits = AsyncMock()
    credits.reserve = AsyncMock(return_value="res-123")
    credits.release_reservation = AsyncMock()
    return credits


@pytest.fixture
def scheduler(mock_queue, mock_pool):
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        use_hrrn=True,
    )


@pytest.fixture
def scheduler_with_credits(mock_queue, mock_pool, mock_credits):
    return SchedulerService(
        queue=mock_queue,
        db_pool=mock_pool,
        credits_service=mock_credits,
        use_hrrn=True,
    )


def _make_request(
    *,
    idempotency_key: str | None = None,
    overlap_policy: str = "skip",
    agent_id: str = "agent-a",
    executor_id: str = "exec-1",
) -> AgentRequest:
    return AgentRequest(
        agent_id=agent_id,
        zone_id=None,
        priority=2,
        executor_id=executor_id,
        task_type="compute",
        idempotency_key=idempotency_key,
        overlap_policy=overlap_policy,
    )


# =============================================================================
# Baseline: No idempotency key → standard UPSERT
# =============================================================================


class TestNoIdempotencyKey:
    """When idempotency_key is None, overlap policy is ignored."""

    @pytest.mark.asyncio
    async def test_submit_without_key_uses_standard_enqueue(self, scheduler, mock_queue):
        req = _make_request(idempotency_key=None, overlap_policy="skip")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-new"
        mock_queue.enqueue.assert_called_once()
        mock_queue.enqueue_skip.assert_not_called()

    @pytest.mark.asyncio
    async def test_submit_without_key_allow_uses_standard_enqueue(self, scheduler, mock_queue):
        req = _make_request(idempotency_key=None, overlap_policy="allow")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-new"
        mock_queue.enqueue.assert_called_once()


# =============================================================================
# ALLOW policy
# =============================================================================


class TestAllowPolicy:
    """ALLOW policy: standard UPSERT regardless of running tasks."""

    @pytest.mark.asyncio
    async def test_allow_uses_standard_enqueue(self, scheduler, mock_queue):
        req = _make_request(idempotency_key="key-1", overlap_policy="allow")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-new"
        mock_queue.enqueue.assert_called_once()
        mock_queue.enqueue_skip.assert_not_called()


# =============================================================================
# SKIP policy
# =============================================================================


class TestSkipPolicy:
    """SKIP policy: reject if a running task with same key exists."""

    @pytest.mark.asyncio
    async def test_skip_enqueues_when_no_running_task(self, scheduler, mock_queue):
        """SKIP succeeds when no running task has the same key."""
        req = _make_request(idempotency_key="key-1", overlap_policy="skip")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-skip"
        mock_queue.enqueue_skip.assert_called_once()

    @pytest.mark.asyncio
    async def test_skip_raises_when_running_task_exists(self, scheduler, mock_queue):
        """SKIP raises TaskAlreadyRunning when CTE returns None."""
        mock_queue.enqueue_skip = AsyncMock(return_value=None)

        req = _make_request(idempotency_key="key-1", overlap_policy="skip")
        with pytest.raises(TaskAlreadyRunning, match="key-1"):
            await scheduler.submit(req)

    @pytest.mark.asyncio
    async def test_skip_releases_boost_reservation_on_rejection(
        self, scheduler_with_credits, mock_queue, mock_credits
    ):
        """SKIP releases any boost reservation when rejecting."""
        mock_queue.enqueue_skip = AsyncMock(return_value=None)

        req = AgentRequest(
            agent_id="agent-a",
            zone_id=None,
            priority=2,
            executor_id="exec-1",
            task_type="compute",
            idempotency_key="key-1",
            overlap_policy="skip",
            boost_amount="0.02",
        )

        with pytest.raises(TaskAlreadyRunning):
            await scheduler_with_credits.submit(req)

        mock_credits.release_reservation.assert_called_once_with("res-123")


# =============================================================================
# CANCEL_PREVIOUS policy
# =============================================================================


class TestCancelPreviousPolicy:
    """CANCEL_PREVIOUS: cancel running task + enqueue new one atomically."""

    @pytest.mark.asyncio
    async def test_cancel_previous_enqueues(self, scheduler, mock_queue):
        """CANCEL_PREVIOUS enqueues the new task."""
        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-new"
        mock_queue.cancel_running_by_idempotency_key.assert_called_once()
        mock_queue.enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_previous_cancels_running_task(self, scheduler, mock_queue):
        """CANCEL_PREVIOUS cancels the running task with same key."""
        mock_queue.cancel_running_by_idempotency_key = AsyncMock(return_value=("old-task-id", None))

        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        await scheduler.submit(req)

        mock_queue.cancel_running_by_idempotency_key.assert_called_once_with(
            mock_queue.cancel_running_by_idempotency_key.call_args.args[0],
            "key-1",
        )

    @pytest.mark.asyncio
    async def test_cancel_previous_releases_old_credits(
        self, scheduler_with_credits, mock_queue, mock_credits
    ):
        """CANCEL_PREVIOUS releases the old task's boost reservation."""
        mock_queue.cancel_running_by_idempotency_key = AsyncMock(
            return_value=("old-task-id", "old-res-456")
        )

        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        await scheduler_with_credits.submit(req)

        # Should release the OLD reservation (not the new one)
        mock_credits.release_reservation.assert_called_once_with("old-res-456")

    @pytest.mark.asyncio
    async def test_cancel_previous_no_running_task(self, scheduler, mock_queue):
        """CANCEL_PREVIOUS still enqueues even when no running task exists."""
        mock_queue.cancel_running_by_idempotency_key = AsyncMock(return_value=(None, None))

        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        task_id = await scheduler.submit(req)
        assert task_id == "task-uuid-new"

    @pytest.mark.asyncio
    async def test_cancel_previous_no_credit_release_when_no_reservation(
        self, scheduler_with_credits, mock_queue, mock_credits
    ):
        """No credit release when old task had no boost reservation."""
        mock_queue.cancel_running_by_idempotency_key = AsyncMock(return_value=("old-task-id", None))

        req = _make_request(idempotency_key="key-1", overlap_policy="cancel")
        await scheduler_with_credits.submit(req)

        # release_reservation might be called for new task's reservation
        # but should NOT be called with None
        for call in mock_credits.release_reservation.call_args_list:
            assert call.args[0] is not None


# =============================================================================
# Admission policy exceptions
# =============================================================================


class TestAdmissionExceptions:
    """Test that typed exceptions are raised instead of ValueError."""

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, mock_queue, mock_pool):
        """RateLimitExceeded raised when token bucket is empty."""
        time_now = 0.0
        rl = TokenBucketLimiter(rate=1.0, burst=1.0, clock=lambda: time_now)
        fs = FairShareCounter()
        admission = AdmissionPolicy(fair_share=fs, rate_limiter=rl)

        svc = SchedulerService(
            queue=mock_queue, db_pool=mock_pool, admission=admission, use_hrrn=True
        )

        # First submit drains the token
        req = _make_request()
        await svc.submit(req)

        # Second submit should be rate-limited
        with pytest.raises(RateLimitExceeded):
            await svc.submit(req)

    @pytest.mark.asyncio
    async def test_capacity_exceeded(self, mock_queue, mock_pool):
        """CapacityExceeded raised when fair-share is at capacity."""
        fs = FairShareCounter(default_max_concurrent=1)
        fs.record_start("exec-1")
        rl = TokenBucketLimiter(rate=100.0)
        admission = AdmissionPolicy(fair_share=fs, rate_limiter=rl)

        svc = SchedulerService(
            queue=mock_queue, db_pool=mock_pool, admission=admission, use_hrrn=True
        )

        req = _make_request()
        with pytest.raises(CapacityExceeded, match="at capacity"):
            await svc.submit(req)


# =============================================================================
# cancel_by_id credit release branches
# =============================================================================


class TestCancelByIdCreditRelease:
    """Test cancel_by_id credit release for all branches."""

    @pytest.mark.asyncio
    async def test_cancel_with_credit_release(self, mock_queue, mock_pool, mock_credits):
        """Cancel succeeds and releases boost reservation."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-1",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status="queued",
                boost_reservation_id="res-456",
                boost_amount=Decimal("0.02"),
            )
        )
        mock_queue.cancel = AsyncMock(return_value=True)

        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool, credits_service=mock_credits)
        result = await svc.cancel_by_id("task-1")

        assert result is True
        mock_credits.release_reservation.assert_called_once_with("res-456")

    @pytest.mark.asyncio
    async def test_cancel_without_credits_service(self, mock_queue, mock_pool):
        """Cancel succeeds without credits service (no release)."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-1",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status="queued",
                boost_reservation_id="res-456",
            )
        )
        mock_queue.cancel = AsyncMock(return_value=True)

        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool, credits_service=None)
        result = await svc.cancel_by_id("task-1")
        assert result is True

    @pytest.mark.asyncio
    async def test_cancel_fails_no_credit_release(self, mock_queue, mock_pool, mock_credits):
        """When cancel fails (task already running), no credit release."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-1",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status=TASK_STATUS_RUNNING,
                boost_reservation_id="res-456",
            )
        )
        mock_queue.cancel = AsyncMock(return_value=False)

        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool, credits_service=mock_credits)
        result = await svc.cancel_by_id("task-1")

        assert result is False
        mock_credits.release_reservation.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_no_boost_reservation(self, mock_queue, mock_pool, mock_credits):
        """Cancel succeeds but no credit release when no reservation."""
        now = datetime.now(UTC)
        mock_queue.get_task = AsyncMock(
            return_value=ScheduledTask(
                id="task-1",
                agent_id="agent-a",
                executor_id="exec-1",
                task_type="compute",
                payload={},
                priority_tier=PriorityTier.NORMAL,
                effective_tier=2,
                enqueued_at=now,
                status="queued",
                boost_reservation_id=None,
            )
        )
        mock_queue.cancel = AsyncMock(return_value=True)

        svc = SchedulerService(queue=mock_queue, db_pool=mock_pool, credits_service=mock_credits)
        result = await svc.cancel_by_id("task-1")

        assert result is True
        mock_credits.release_reservation.assert_not_called()
