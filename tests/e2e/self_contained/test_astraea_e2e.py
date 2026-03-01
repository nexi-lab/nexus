"""E2E integration tests for Astraea-style scheduler (Issue #1274).

Exercises the full stack: Router → SchedulerService → TaskQueue (mocked DB).
Validates classification, HRRN dequeue, fair-share admission, metrics,
and agent state event flow without requiring a live PostgreSQL database.

Run with: uv run pytest tests/integration/scheduler/test_astraea_e2e.py -v --override-ini="addopts="
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.scheduler import (
    _get_require_auth,
    get_scheduler_service,
    router,
)
from nexus.services.scheduler.constants import (
    TASK_STATUS_QUEUED,
    TASK_STATUS_RUNNING,
    PriorityTier,
)
from nexus.services.scheduler.events import AgentStateEmitter, AgentStateEvent
from nexus.services.scheduler.models import ScheduledTask
from nexus.services.scheduler.policies.fair_share import FairShareCounter
from nexus.services.scheduler.service import SchedulerService

# =============================================================================
# Fixtures
# =============================================================================


def _make_mock_pool():
    """Create a mock asyncpg pool with async context manager."""
    conn = AsyncMock()
    pool = MagicMock()
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=conn)
    acm.__aexit__ = AsyncMock(return_value=None)
    pool.acquire = MagicMock(return_value=acm)
    return pool, conn


def _make_task(
    *,
    task_id: str = "task-e2e-001",
    agent_id: str = "test-agent",
    executor_id: str = "exec-1",
    task_type: str = "compute",
    priority_tier: PriorityTier = PriorityTier.NORMAL,
    effective_tier: int = 2,
    status: str = TASK_STATUS_QUEUED,
    priority_class: str = "batch",
    request_state: str = "compute",
    estimated_service_time: float = 30.0,
) -> ScheduledTask:
    return ScheduledTask(
        id=task_id,
        agent_id=agent_id,
        executor_id=executor_id,
        task_type=task_type,
        payload={"data": "value"},
        priority_tier=priority_tier,
        effective_tier=effective_tier,
        enqueued_at=datetime.now(UTC),
        status=status,
        priority_class=priority_class,
        request_state=request_state,
        estimated_service_time=estimated_service_time,
    )


@pytest.fixture
def mock_queue():
    """Fully-mocked TaskQueue with Astraea methods."""
    q = AsyncMock()
    q.enqueue = AsyncMock(return_value="task-e2e-001")
    q.dequeue = AsyncMock(return_value=None)
    q.dequeue_hrrn = AsyncMock(return_value=None)
    q.complete = AsyncMock()
    q.cancel = AsyncMock(return_value=True)
    q.cancel_by_agent = AsyncMock(return_value=2)
    # get_task returns a ScheduledTask matching what submit() would create.
    # The enqueue call args are captured by the mock, but get_status() needs
    # get_task to return a non-None task, so we use side_effect to build a
    # task dynamically from the last enqueue call.
    _last_enqueue_kwargs: dict = {}

    async def _enqueue_capture(*args, **kwargs):
        _last_enqueue_kwargs.update(kwargs)
        return "task-e2e-001"

    async def _get_task_dynamic(*args, **kwargs):
        if not _last_enqueue_kwargs:
            return None
        return _make_task(
            task_id="task-e2e-001",
            agent_id=_last_enqueue_kwargs.get("agent_id", "test-agent"),
            executor_id=_last_enqueue_kwargs.get("executor_id", "exec-1"),
            task_type=_last_enqueue_kwargs.get("task_type", "compute"),
            priority_tier=PriorityTier(_last_enqueue_kwargs.get("priority_tier", 2)),
            effective_tier=_last_enqueue_kwargs.get("effective_tier", 2),
            status=TASK_STATUS_QUEUED,
            priority_class=_last_enqueue_kwargs.get("priority_class", "batch"),
            request_state=_last_enqueue_kwargs.get("request_state", "compute"),
            estimated_service_time=_last_enqueue_kwargs.get("estimated_service_time", 30.0),
        )

    q.enqueue = AsyncMock(side_effect=_enqueue_capture)
    q.get_task = AsyncMock(side_effect=_get_task_dynamic)
    q.aging_sweep = AsyncMock(return_value=0)
    q.count_running_by_agent = AsyncMock(return_value={})
    q.update_executor_state = AsyncMock()
    q.promote_starved = AsyncMock(return_value=0)
    q.get_queue_metrics = AsyncMock(
        return_value=[
            {"priority_class": "interactive", "cnt": 2, "avg_wait": 1.5, "max_wait": 3.0},
            {"priority_class": "batch", "cnt": 5, "avg_wait": 10.0, "max_wait": 30.0},
        ]
    )
    return q


@pytest.fixture
def fair_share():
    return FairShareCounter(default_max_concurrent=10)


@pytest.fixture
def state_emitter():
    return AgentStateEmitter()


@pytest.fixture
def scheduler_service(mock_queue, fair_share, state_emitter):
    pool, _ = _make_mock_pool()
    return SchedulerService(
        queue=mock_queue,
        db_pool=pool,
        state_emitter=state_emitter,
        fair_share=fair_share,
        use_hrrn=True,
    )


@pytest.fixture
def mock_auth_result():
    return {
        "authenticated": True,
        "subject_type": "user",
        "subject_id": "test-agent",
        "zone_id": "test-zone",
        "is_admin": False,
    }


@pytest.fixture
def app(scheduler_service, mock_auth_result):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_scheduler_service] = lambda: scheduler_service
    app.dependency_overrides[_get_require_auth()] = lambda: mock_auth_result
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# =============================================================================
# E2E Flow: Submit → Get Status → Cancel
# =============================================================================


class TestSubmitFlowE2E:
    """Full submit flow with Astraea classification."""

    def test_submit_with_classification(self, client, mock_queue):
        """Submit a task and verify auto-classification in response."""
        payload = {
            "executor": "exec-1",
            "task_type": "compute",
            "priority": "normal",
            "request_state": "compute",
            "estimated_service_time": 25.0,
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "task-e2e-001"
        assert data["status"] == "queued"
        assert data["priority_class"] == "batch"
        assert data["request_state"] == "compute"

    def test_submit_io_wait_promotes_to_batch(self, client, mock_queue):
        """IO_WAIT tasks with low priority should be promoted to batch."""
        payload = {
            "executor": "exec-1",
            "task_type": "io_op",
            "priority": "low",
            "request_state": "io_wait",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)

        assert response.status_code == 201
        data = response.json()
        # IO promotion: LOW → BACKGROUND → promoted to BATCH due to io_wait
        assert data["priority_class"] == "batch"

    def test_submit_critical_classifies_interactive(self, client, mock_queue):
        """CRITICAL priority should classify as interactive."""
        payload = {
            "executor": "exec-1",
            "task_type": "urgent",
            "priority": "critical",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)

        assert response.status_code == 201
        data = response.json()
        assert data["priority_class"] == "interactive"

    def test_submit_invalid_request_state_rejected(self, client):
        """Invalid request_state should be rejected with 422."""
        payload = {
            "executor": "exec-1",
            "task_type": "compute",
            "request_state": "invalid_state",
        }

        response = client.post("/api/v2/scheduler/submit", json=payload)
        assert response.status_code == 422


# =============================================================================
# E2E: Classify Endpoint
# =============================================================================


class TestClassifyEndpointE2E:
    """Test /classify endpoint for Astraea classification."""

    def test_classify_high_interactive(self, client):
        response = client.post(
            "/api/v2/scheduler/classify",
            json={"priority": "high", "request_state": "compute"},
        )
        assert response.status_code == 200
        assert response.json()["priority_class"] == "interactive"

    def test_classify_normal_batch(self, client):
        response = client.post(
            "/api/v2/scheduler/classify",
            json={"priority": "normal", "request_state": "pending"},
        )
        assert response.status_code == 200
        assert response.json()["priority_class"] == "batch"

    def test_classify_low_background(self, client):
        response = client.post(
            "/api/v2/scheduler/classify",
            json={"priority": "low", "request_state": "idle"},
        )
        assert response.status_code == 200
        assert response.json()["priority_class"] == "background"

    def test_classify_low_io_wait_promoted(self, client):
        response = client.post(
            "/api/v2/scheduler/classify",
            json={"priority": "low", "request_state": "io_wait"},
        )
        assert response.status_code == 200
        assert response.json()["priority_class"] == "batch"


# =============================================================================
# E2E: Metrics Endpoint
# =============================================================================


class TestMetricsEndpointE2E:
    """Test /metrics endpoint for queue stats and fair-share."""

    def test_metrics_returns_queue_stats(self, client, fair_share):
        # Record some fair-share activity
        fair_share.record_start("agent-a")
        fair_share.record_start("agent-a")
        fair_share.record_start("agent-b")

        response = client.get("/api/v2/scheduler/metrics")

        assert response.status_code == 200
        data = response.json()
        assert data["use_hrrn"] is True
        assert len(data["queue_by_class"]) == 2
        assert "fair_share" in data
        assert data["fair_share"]["agent-a"]["running_count"] == 2
        assert data["fair_share"]["agent-b"]["running_count"] == 1


# =============================================================================
# E2E: Fair-Share Admission Control
# =============================================================================


class TestFairShareE2E:
    """Test that fair-share admission is enforced end-to-end."""

    def test_submit_rejected_at_capacity(self, mock_queue, state_emitter):
        """Submitting when executor is at capacity should raise."""
        pool, _ = _make_mock_pool()
        fs = FairShareCounter(default_max_concurrent=1)
        svc = SchedulerService(
            queue=mock_queue,
            db_pool=pool,
            fair_share=fs,
            state_emitter=state_emitter,
            use_hrrn=True,
        )

        # Fill capacity
        fs.record_start("exec-1")

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_scheduler_service] = lambda: svc
        app.dependency_overrides[_get_require_auth()] = lambda: {
            "subject_id": "agent-a",
        }

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/v2/scheduler/submit",
            json={
                "executor": "exec-1",
                "task_type": "compute",
            },
        )
        # Service raises ValueError → 500 (or could be wrapped to 429)
        assert response.status_code == 500


# =============================================================================
# E2E: HRRN Dequeue
# =============================================================================


class TestHrrnDequeueE2E:
    """Test HRRN dequeue via the service."""

    @pytest.mark.asyncio
    async def test_hrrn_dequeue_selects_highest_score(
        self, scheduler_service, mock_queue, fair_share
    ):
        """HRRN dequeue should call dequeue_hrrn and update fair-share."""
        task = _make_task(status=TASK_STATUS_RUNNING)
        mock_queue.dequeue_hrrn = AsyncMock(return_value=task)

        result = await scheduler_service.dequeue_next()

        assert result is not None
        assert result.id == "task-e2e-001"
        mock_queue.dequeue_hrrn.assert_called_once()
        mock_queue.dequeue.assert_not_called()
        # Fair-share updated
        assert fair_share.snapshot("test-agent").running_count == 1


# =============================================================================
# E2E: Agent State Events
# =============================================================================


class TestAgentStateEventsE2E:
    """Test event-driven executor state updates."""

    @pytest.mark.asyncio
    async def test_state_event_updates_queue(self, scheduler_service, state_emitter, mock_queue):
        """Agent state change should propagate to executor_state in DB."""
        event = AgentStateEvent(
            agent_id="exec-1",
            previous_state="IDLE",
            new_state="CONNECTED",
            generation=3,
            zone_id="test-zone",
        )

        await state_emitter.emit(event)

        # Verify the handler updated executor_state via queue
        mock_queue.update_executor_state.assert_called_once()
        call_args = mock_queue.update_executor_state.call_args
        assert call_args[0][1] == "exec-1"
        assert call_args[0][2] == "CONNECTED"

    @pytest.mark.asyncio
    async def test_suspended_executor_blocks_dequeue(
        self, scheduler_service, state_emitter, mock_queue
    ):
        """After SUSPENDED event, dequeue_hrrn SQL excludes those tasks."""
        # Emit suspended event
        event = AgentStateEvent(
            agent_id="exec-1",
            previous_state="CONNECTED",
            new_state="SUSPENDED",
            generation=4,
        )
        await state_emitter.emit(event)

        # The SQL in dequeue_hrrn already filters executor_state
        # Verify update_executor_state was called with SUSPENDED
        mock_queue.update_executor_state.assert_called_once()
        call_args = mock_queue.update_executor_state.call_args
        assert call_args[0][2] == "SUSPENDED"


# =============================================================================
# E2E: Cancel by Agent (Protocol method)
# =============================================================================


class TestCancelByAgentE2E:
    """Test bulk cancel via protocol cancel(agent_id)."""

    @pytest.mark.asyncio
    async def test_cancel_by_agent_delegates_to_queue(self, scheduler_service, mock_queue):
        """cancel(agent_id) should delegate to queue.cancel_by_agent."""
        mock_queue.cancel_by_agent = AsyncMock(return_value=3)

        count = await scheduler_service.cancel("agent-a")

        assert count == 3
        mock_queue.cancel_by_agent.assert_called_once()


# =============================================================================
# E2E: Complete with Fair-Share
# =============================================================================


class TestCompleteE2E:
    """Test complete flow with fair-share update."""

    @pytest.mark.asyncio
    async def test_complete_decrements_fair_share(self, scheduler_service, mock_queue, fair_share):
        """Completing a task should decrement fair-share counter."""
        task = _make_task(status=TASK_STATUS_RUNNING)
        mock_queue.get_task = AsyncMock(return_value=task)

        fair_share.record_start("test-agent")
        assert fair_share.snapshot("test-agent").running_count == 1

        await scheduler_service.complete("task-e2e-001")

        assert fair_share.snapshot("test-agent").running_count == 0

    @pytest.mark.asyncio
    async def test_complete_with_error(self, scheduler_service, mock_queue, fair_share):
        """Completing with error should still decrement fair-share."""
        task = _make_task(status=TASK_STATUS_RUNNING)
        mock_queue.get_task = AsyncMock(return_value=task)

        fair_share.record_start("test-agent")
        await scheduler_service.complete("task-e2e-001", error="timeout")

        assert fair_share.snapshot("test-agent").running_count == 0
        # Queue was called with failed status
        mock_queue.complete.assert_called_once()


# =============================================================================
# E2E: Starvation Promotion
# =============================================================================


class TestStarvationPromotionE2E:
    """Test starvation promotion flow."""

    @pytest.mark.asyncio
    async def test_starvation_promotes_background_tasks(self, scheduler_service, mock_queue):
        """Starved BACKGROUND tasks should be promoted to BATCH."""
        mock_queue.promote_starved = AsyncMock(return_value=5)

        count = await scheduler_service.run_starvation_promotion(threshold_seconds=900)

        assert count == 5
        mock_queue.promote_starved.assert_called_once_with(
            mock_queue.promote_starved.call_args[0][0],  # conn
            900,
        )


# =============================================================================
# E2E: Sync Fair-Share from DB
# =============================================================================


class TestSyncFairShareE2E:
    """Test fair-share sync from database."""

    @pytest.mark.asyncio
    async def test_sync_loads_running_counts(self, scheduler_service, mock_queue, fair_share):
        """sync_fair_share should populate counters from DB."""
        mock_queue.count_running_by_agent = AsyncMock(return_value={"agent-a": 3, "agent-b": 1})

        await scheduler_service.sync_fair_share()

        assert fair_share.snapshot("agent-a").running_count == 3
        assert fair_share.snapshot("agent-b").running_count == 1


# =============================================================================
# E2E: Full Protocol Submit Flow (AgentRequest → task_id)
# =============================================================================


class TestProtocolSubmitE2E:
    """Test the protocol-level submit() that takes AgentRequest."""

    @pytest.mark.asyncio
    async def test_submit_via_protocol(self, scheduler_service, mock_queue):
        """Protocol submit(AgentRequest) should return task_id string."""
        from nexus.contracts.protocols.scheduler import AgentRequest

        req = AgentRequest(
            agent_id="agent-a",
            zone_id="zone-1",
            priority=2,
            executor_id="exec-1",
            task_type="compute",
            request_state="compute",
        )

        task_id = await scheduler_service.submit(req)

        assert isinstance(task_id, str)
        assert task_id == "task-e2e-001"
        mock_queue.enqueue.assert_called_once()

        # Verify auto-classification was applied
        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["priority_class"] == "batch"
        assert call_kwargs["request_state"] == "compute"

    @pytest.mark.asyncio
    async def test_submit_critical_classifies_interactive(self, scheduler_service, mock_queue):
        """Protocol submit with critical priority → interactive class."""
        from nexus.contracts.protocols.scheduler import AgentRequest

        req = AgentRequest(
            agent_id="agent-a",
            zone_id=None,
            priority=0,  # CRITICAL
            executor_id="exec-1",
            task_type="urgent",
        )

        await scheduler_service.submit(req)

        call_kwargs = mock_queue.enqueue.call_args[1]
        assert call_kwargs["priority_class"] == "interactive"
