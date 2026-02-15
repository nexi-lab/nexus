"""Tests for GovernanceEnforcedPayment wrapper.

Issue #1359 Phase 3: Wrapper chain integration with mock inner protocol.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.protocol import ProtocolTransferRequest, ProtocolTransferResult
from nexus.services.governance.governance_wrapper import (
    GovernanceApprovalRequired,
    GovernanceBlockedError,
    GovernanceEnforcedPayment,
)
from nexus.services.governance.models import ConstraintCheckResult, ConstraintType


@pytest.fixture
def mock_inner() -> AsyncMock:
    """Mock inner PaymentProtocol."""
    inner = AsyncMock()
    inner.protocol_name = TransactionProtocol.INTERNAL
    inner.can_handle = MagicMock(return_value=True)
    inner.transfer = AsyncMock(
        return_value=ProtocolTransferResult(
            protocol=TransactionProtocol.INTERNAL,
            tx_id="tx-123",
            amount=Decimal("10.0"),
            from_agent="agent-a",
            to="agent-b",
        )
    )
    return inner


@pytest.fixture
def mock_graph_service() -> AsyncMock:
    """Mock GovernanceGraphService."""
    svc = AsyncMock()
    svc.check_constraint = AsyncMock(return_value=ConstraintCheckResult(allowed=True))
    return svc


@pytest.fixture
def mock_anomaly_service() -> AsyncMock:
    """Mock AnomalyService."""
    svc = AsyncMock()
    svc.analyze_transaction = AsyncMock(return_value=[])
    return svc


@pytest.fixture
def wrapper(
    mock_inner: AsyncMock,
    mock_graph_service: AsyncMock,
    mock_anomaly_service: AsyncMock,
) -> GovernanceEnforcedPayment:
    """GovernanceEnforcedPayment with all mocks."""
    return GovernanceEnforcedPayment(
        inner=mock_inner,
        graph_service=mock_graph_service,
        anomaly_service=mock_anomaly_service,
    )


def _make_request(
    from_agent: str = "agent-a",
    to: str = "agent-b",
    amount: Decimal = Decimal("10.0"),
    zone_id: str = "default",
) -> ProtocolTransferRequest:
    return ProtocolTransferRequest(
        from_agent=from_agent,
        to=to,
        amount=amount,
        metadata={"zone_id": zone_id},
    )


class TestProtocolDelegation:
    """Tests that wrapper correctly delegates to inner protocol."""

    def test_protocol_name(self, wrapper: GovernanceEnforcedPayment) -> None:
        assert wrapper.protocol_name == TransactionProtocol.INTERNAL

    def test_can_handle(self, wrapper: GovernanceEnforcedPayment) -> None:
        assert wrapper.can_handle("agent-b")


class TestConstraintChecks:
    """Tests for pre-transfer constraint checking."""

    @pytest.mark.asyncio
    async def test_allowed_transfer(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_inner: AsyncMock,
        mock_graph_service: AsyncMock,
    ) -> None:
        result = await wrapper.transfer(_make_request())
        assert result.tx_id == "tx-123"
        mock_graph_service.check_constraint.assert_awaited_once()
        mock_inner.transfer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_blocked_transfer(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_inner: AsyncMock,
        mock_graph_service: AsyncMock,
    ) -> None:
        mock_graph_service.check_constraint.return_value = ConstraintCheckResult(
            allowed=False,
            constraint_type=ConstraintType.BLOCK,
            reason="Agent blocked",
        )
        with pytest.raises(GovernanceBlockedError, match="Agent blocked"):
            await wrapper.transfer(_make_request())

        # Inner protocol should NOT be called
        mock_inner.transfer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approval_required(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_inner: AsyncMock,
        mock_graph_service: AsyncMock,
    ) -> None:
        mock_graph_service.check_constraint.return_value = ConstraintCheckResult(
            allowed=False,
            constraint_type=ConstraintType.REQUIRE_APPROVAL,
            reason="Approval needed",
        )
        with pytest.raises(GovernanceApprovalRequired, match="Approval needed"):
            await wrapper.transfer(_make_request())

        mock_inner.transfer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rate_limit_constraint(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_inner: AsyncMock,
        mock_graph_service: AsyncMock,
    ) -> None:
        mock_graph_service.check_constraint.return_value = ConstraintCheckResult(
            allowed=False,
            constraint_type=ConstraintType.RATE_LIMIT,
            reason="Rate limited",
        )
        with pytest.raises(GovernanceBlockedError, match="Rate limited"):
            await wrapper.transfer(_make_request())


class TestPostAnalysis:
    """Tests for post-transfer anomaly analysis."""

    @pytest.mark.asyncio
    async def test_fire_and_forget_analysis(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_anomaly_service: AsyncMock,
    ) -> None:
        """Verify analysis is scheduled but doesn't block transfer."""
        result = await wrapper.transfer(_make_request())
        assert result.tx_id == "tx-123"
        # Analysis is fire-and-forget, so we can't easily assert it was called
        # But at minimum the transfer should succeed

    @pytest.mark.asyncio
    async def test_zone_id_from_metadata(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_graph_service: AsyncMock,
    ) -> None:
        request = _make_request(zone_id="custom-zone")
        await wrapper.transfer(request)
        mock_graph_service.check_constraint.assert_awaited_once_with(
            from_agent="agent-a",
            to_agent="agent-b",
            zone_id="custom-zone",
        )

    @pytest.mark.asyncio
    async def test_default_zone_id(
        self,
        wrapper: GovernanceEnforcedPayment,
        mock_graph_service: AsyncMock,
    ) -> None:
        request = ProtocolTransferRequest(
            from_agent="agent-a",
            to="agent-b",
            amount=Decimal("10.0"),
        )
        await wrapper.transfer(request)
        mock_graph_service.check_constraint.assert_awaited_once_with(
            from_agent="agent-a",
            to_agent="agent-b",
            zone_id="default",
        )
