"""Tests for ResponseService.

Issue #1359 Phase 4: Full lifecycle — detect → throttle → suspend → appeal → reinstate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.governance.models import AnomalySeverity, FraudScore
from nexus.governance.response_service import ResponseService


def _make_mock_session_factory() -> MagicMock:
    """Create a mock async session factory."""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.add = MagicMock()
    session.flush = AsyncMock()
    session.execute = AsyncMock()

    begin_ctx = AsyncMock()
    begin_ctx.__aenter__ = AsyncMock()
    begin_ctx.__aexit__ = AsyncMock()
    session.begin = MagicMock(return_value=begin_ctx)

    factory = MagicMock(return_value=session)
    return factory


class TestAutoThrottle:
    """Tests for automatic throttling based on fraud scores."""

    @pytest.fixture
    def service(self) -> ResponseService:
        return ResponseService(
            session_factory=_make_mock_session_factory(),
            graph_service=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_low_score_no_throttle(self, service: ResponseService) -> None:
        score = FraudScore("agent-1", "zone-1", 0.3, {}, datetime.now(UTC))
        result = await service.auto_throttle("agent-1", "zone-1", score)
        assert result is None

    @pytest.mark.asyncio
    async def test_medium_score_throttle(self, service: ResponseService) -> None:
        score = FraudScore("agent-1", "zone-1", 0.6, {}, datetime.now(UTC))
        result = await service.auto_throttle("agent-1", "zone-1", score)
        assert result is not None
        assert result.max_tx_per_hour > 0
        assert result.max_amount_per_day > 0

    @pytest.mark.asyncio
    async def test_high_score_block(self, service: ResponseService) -> None:
        score = FraudScore("agent-1", "zone-1", 0.9, {}, datetime.now(UTC))
        result = await service.auto_throttle("agent-1", "zone-1", score)
        # Score >= 0.8 triggers block, not throttle
        assert result is None
        # graph_service.add_constraint should have been called with BLOCK
        service._graph_service.add_constraint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_throttle_scaling(self, service: ResponseService) -> None:
        """Higher fraud score = more restrictive throttle."""
        score_low = FraudScore("a1", "z1", 0.55, {}, datetime.now(UTC))
        score_high = FraudScore("a2", "z1", 0.75, {}, datetime.now(UTC))

        t_low = await service.auto_throttle("a1", "z1", score_low)
        t_high = await service.auto_throttle("a2", "z1", score_high)

        assert t_low is not None
        assert t_high is not None
        assert t_high.max_tx_per_hour <= t_low.max_tx_per_hour


class TestSuspendAgent:
    """Tests for agent suspension."""

    @pytest.fixture
    def service(self) -> ResponseService:
        return ResponseService(
            session_factory=_make_mock_session_factory(),
            graph_service=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_suspend_creates_record(self, service: ResponseService) -> None:
        record = await service.suspend_agent(
            agent_id="agent-1",
            zone_id="zone-1",
            reason="Fraud detected",
            duration_hours=24.0,
        )
        assert record.agent_id == "agent-1"
        assert record.zone_id == "zone-1"
        assert record.reason == "Fraud detected"
        assert record.suspended_at is not None
        assert record.expires_at is not None

    @pytest.mark.asyncio
    async def test_suspend_creates_block_constraint(self, service: ResponseService) -> None:
        await service.suspend_agent("agent-1", "zone-1", "Test")
        service._graph_service.add_constraint.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_suspend_with_custom_severity(self, service: ResponseService) -> None:
        record = await service.suspend_agent(
            agent_id="agent-1",
            zone_id="zone-1",
            reason="Critical fraud",
            severity=AnomalySeverity.CRITICAL,
        )
        assert record.severity == AnomalySeverity.CRITICAL


class TestAppealWorkflow:
    """Tests for suspension appeal lifecycle."""

    @pytest.fixture
    def service(self) -> ResponseService:
        """Service with mock that returns a suspension for get."""
        svc = ResponseService(
            session_factory=_make_mock_session_factory(),
            graph_service=AsyncMock(),
        )
        return svc

    @pytest.mark.asyncio
    async def test_appeal_nonexistent_raises(self, service: ResponseService) -> None:
        # Mock _get_suspension to return None
        service._get_suspension = AsyncMock(return_value=None)
        with pytest.raises(KeyError, match="not found"):
            await service.appeal_suspension("nonexistent", "I'm innocent")

    @pytest.mark.asyncio
    async def test_decide_nonexistent_raises(self, service: ResponseService) -> None:
        service._get_suspension = AsyncMock(return_value=None)
        with pytest.raises(KeyError, match="not found"):
            await service.decide_appeal("nonexistent", True, "admin")

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, service: ResponseService) -> None:
        """Test: suspend → appeal → approve."""

        # Create suspension
        record = await service.suspend_agent("agent-1", "zone-1", "Fraud")

        # Mock _get_suspension to return the record
        service._get_suspension = AsyncMock(return_value=record)
        service._update_suspension = AsyncMock()

        # Appeal
        appealed = await service.appeal_suspension(record.suspension_id, "I'm innocent")
        assert appealed.appeal_status == "pending"

        # Mock _get_suspension again with pending appeal
        service._get_suspension = AsyncMock(return_value=appealed)
        service._graph_service.list_constraints = AsyncMock(return_value=[])

        # Decide (approve)
        decided = await service.decide_appeal(record.suspension_id, True, "admin")
        assert decided.appeal_status == "approved"
        assert decided.decided_by == "admin"
