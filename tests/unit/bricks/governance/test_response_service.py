"""Unit tests for ResponseService.

Tests suspension lifecycle, appeal workflow, and throttle logic
using mocked database dependencies.
"""

from unittest.mock import AsyncMock

import pytest

from nexus.bricks.governance.models import (
    AnomalySeverity,
    FraudScore,
    SuspensionRecord,
)
from nexus.bricks.governance.response_service import ResponseService


class _FakeAsyncSession:
    """Fake async session that works as an async context manager."""

    def __init__(self) -> None:
        self._added: list[object] = []

    async def __aenter__(self) -> "_FakeAsyncSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    def begin(self) -> "_FakeAsyncSession":
        """Return self as async context manager for begin()."""
        return self

    def add(self, model: object) -> None:
        self._added.append(model)

    async def flush(self) -> None:
        pass


def _mock_session_factory() -> object:
    """Create a session factory that returns a proper async context manager."""
    session = _FakeAsyncSession()

    def factory() -> "_FakeAsyncSession":
        return session

    return factory


# ---------------------------------------------------------------------------
# suspend_agent
# ---------------------------------------------------------------------------


class TestSuspendAgent:
    """Tests for ResponseService.suspend_agent."""

    @pytest.mark.asyncio
    async def test_creates_suspension_record(self) -> None:
        factory = _mock_session_factory()
        svc = ResponseService(session_factory=factory)
        record = await svc.suspend_agent(
            agent_id="a1",
            zone_id="z1",
            reason="Fraudulent activity",
            duration_hours=48.0,
        )
        assert record.agent_id == "a1"
        assert record.zone_id == "z1"
        assert record.reason == "Fraudulent activity"
        assert record.severity == AnomalySeverity.HIGH
        assert record.appeal_status == "none"
        assert record.suspended_at is not None
        assert record.expires_at is not None

    @pytest.mark.asyncio
    async def test_suspension_with_custom_severity(self) -> None:
        factory = _mock_session_factory()
        svc = ResponseService(session_factory=factory)
        record = await svc.suspend_agent(
            agent_id="a1",
            zone_id="z1",
            reason="Critical violation",
            severity=AnomalySeverity.CRITICAL,
        )
        assert record.severity == AnomalySeverity.CRITICAL

    @pytest.mark.asyncio
    async def test_suspension_calls_graph_service_block(self) -> None:
        factory = _mock_session_factory()
        graph_service = AsyncMock()
        svc = ResponseService(session_factory=factory, graph_service=graph_service)
        await svc.suspend_agent("a1", "z1", "test")
        graph_service.add_constraint.assert_called_once()
        call_kwargs = graph_service.add_constraint.call_args
        assert call_kwargs.kwargs["from_agent"] == "a1"

    @pytest.mark.asyncio
    async def test_suspension_without_graph_service(self) -> None:
        factory = _mock_session_factory()
        svc = ResponseService(session_factory=factory, graph_service=None)
        # Should not raise even without graph_service
        record = await svc.suspend_agent("a1", "z1", "test")
        assert record.agent_id == "a1"


# ---------------------------------------------------------------------------
# auto_throttle thresholds
# ---------------------------------------------------------------------------


class TestAutoThrottle:
    """Tests for auto_throttle score-based response."""

    @pytest.mark.asyncio
    async def test_below_threshold_no_action(self) -> None:
        svc = ResponseService(session_factory=_mock_session_factory())
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.2)
        result = await svc.auto_throttle("a1", "z1", score)
        assert result is None

    @pytest.mark.asyncio
    async def test_at_exact_throttle_threshold(self) -> None:
        factory = _mock_session_factory()
        svc = ResponseService(session_factory=factory)
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.5)
        result = await svc.auto_throttle("a1", "z1", score)
        assert result is not None
        assert result.agent_id == "a1"

    @pytest.mark.asyncio
    async def test_throttle_max_tx_decreases_with_score(self) -> None:
        svc = ResponseService(session_factory=_mock_session_factory())

        score_low = FraudScore(agent_id="a1", zone_id="z1", score=0.5)
        result_low = await svc.auto_throttle("a1", "z1", score_low)

        score_high = FraudScore(agent_id="a1", zone_id="z1", score=0.75)
        result_high = await svc.auto_throttle("a1", "z1", score_high)

        assert result_low is not None
        assert result_high is not None
        # Higher fraud score -> fewer allowed transactions
        assert result_high.max_tx_per_hour <= result_low.max_tx_per_hour

    @pytest.mark.asyncio
    async def test_block_threshold_creates_block_constraint(self) -> None:
        graph_service = AsyncMock()
        svc = ResponseService(
            session_factory=_mock_session_factory(),
            graph_service=graph_service,
        )
        score = FraudScore(agent_id="a1", zone_id="z1", score=0.85)
        result = await svc.auto_throttle("a1", "z1", score)
        assert result is None  # Blocked, not throttled
        graph_service.add_constraint.assert_called_once()


# ---------------------------------------------------------------------------
# Appeal workflow (in-memory)
# ---------------------------------------------------------------------------


class TestAppealWorkflow:
    """Tests for the appeal workflow integration in ResponseService.

    These test the in-memory ApprovalWorkflow usage within ResponseService.
    Since appeal_suspension and decide_appeal require DB reads,
    we test the internal _appeal_workflow directly.
    """

    def test_appeal_workflow_initialized(self) -> None:
        svc = ResponseService(session_factory=_mock_session_factory())
        # The internal workflow should be initialized with 168h (7 days) expiry
        assert svc._appeal_workflow._default_expiry_hours == 168.0

    def test_can_submit_and_approve_via_workflow(self) -> None:
        svc = ResponseService(session_factory=_mock_session_factory())
        record = svc._appeal_workflow.submit("agent-1", record_id="s-123")
        assert record.status.value == "pending"

        approved = svc._appeal_workflow.approve("s-123", "admin")
        assert approved.status.value == "approved"

    def test_cannot_double_approve_via_workflow(self) -> None:
        from nexus.bricks.governance.approval.state_machine import InvalidTransitionError

        svc = ResponseService(session_factory=_mock_session_factory())
        svc._appeal_workflow.submit("agent-1", record_id="s-123")
        svc._appeal_workflow.approve("s-123", "admin")
        with pytest.raises(InvalidTransitionError):
            svc._appeal_workflow.approve("s-123", "admin2")


# ---------------------------------------------------------------------------
# Suspension record immutability
# ---------------------------------------------------------------------------


class TestSuspensionRecordImmutability:
    """Tests that SuspensionRecord is frozen."""

    def test_suspension_record_frozen(self) -> None:
        record = SuspensionRecord(
            suspension_id="s1",
            agent_id="a1",
            zone_id="z1",
            reason="test",
        )
        with pytest.raises(AttributeError):
            record.reason = "modified"
