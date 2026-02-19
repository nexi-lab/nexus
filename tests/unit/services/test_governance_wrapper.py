"""Tests for GovernanceEnforcedPayment wrapper.

Issue #1519, 10A: Tests governance constraint checks, exception propagation,
delegation to inner protocol, and fire-and-forget anomaly analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.governance.governance_wrapper import (
    GovernanceApprovalRequired,
    GovernanceBlockedError,
    GovernanceEnforcedPayment,
)


@dataclass
class _FakeConstraintCheck:
    """Minimal constraint check result for testing."""

    allowed: bool
    constraint_type: object = None
    reason: str | None = None
    edge_id: str | None = None


@pytest.fixture()
def inner_protocol():
    mock = MagicMock()
    mock.protocol_name = "credits"
    mock.can_handle.return_value = True
    # transfer() is async, so use AsyncMock for it
    mock.transfer = AsyncMock(return_value=MagicMock(success=True, tx_id="tx-1"))
    return mock


@pytest.fixture()
def graph_service():
    return AsyncMock()


@pytest.fixture()
def anomaly_service():
    return AsyncMock()


@pytest.fixture()
def wrapper(inner_protocol, graph_service, anomaly_service):
    return GovernanceEnforcedPayment(
        inner=inner_protocol,
        graph_service=graph_service,
        anomaly_service=anomaly_service,
    )


@pytest.fixture()
def transfer_request():
    """Create a minimal ProtocolTransferRequest-like object."""
    from decimal import Decimal

    from nexus.bricks.pay.protocol import ProtocolTransferRequest

    return ProtocolTransferRequest(
        from_agent="agent-1",
        to="agent-2",
        amount=Decimal("100.0"),
        metadata={"zone_id": "acme"},
    )


class TestGovernanceExceptions:
    """Tests for GovernanceBlockedError and GovernanceApprovalRequired."""

    def test_blocked_error_stores_edge_id(self):
        err = GovernanceBlockedError("blocked", edge_id="edge-1")
        assert str(err) == "blocked"
        assert err.edge_id == "edge-1"

    def test_blocked_error_edge_id_defaults_to_none(self):
        err = GovernanceBlockedError("blocked")
        assert err.edge_id is None

    def test_approval_required_stores_edge_id(self):
        err = GovernanceApprovalRequired("needs approval", edge_id="edge-2")
        assert str(err) == "needs approval"
        assert err.edge_id == "edge-2"


class TestDelegation:
    """Tests for protocol_name and can_handle delegation."""

    def test_protocol_name_delegates_to_inner(self, wrapper, inner_protocol):
        assert wrapper.protocol_name == inner_protocol.protocol_name

    def test_can_handle_delegates_to_inner(self, wrapper, inner_protocol):
        assert wrapper.can_handle("agent-2") is True
        inner_protocol.can_handle.assert_called_once_with("agent-2", None)


class TestTransferPreCheck:
    """Tests for governance constraint pre-check in transfer()."""

    @pytest.mark.asyncio
    async def test_transfer_allowed_delegates_to_inner(
        self, wrapper, graph_service, inner_protocol, transfer_request
    ):
        graph_service.check_constraint.return_value = _FakeConstraintCheck(allowed=True)

        result = await wrapper.transfer(transfer_request)

        inner_protocol.transfer.assert_awaited_once_with(transfer_request)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_transfer_blocked_raises_governance_blocked_error(
        self, wrapper, graph_service, transfer_request
    ):
        from nexus.bricks.governance.models import ConstraintType

        graph_service.check_constraint.return_value = _FakeConstraintCheck(
            allowed=False,
            constraint_type=ConstraintType.BLOCK,
            reason="Forbidden by policy",
            edge_id="edge-99",
        )

        with pytest.raises(GovernanceBlockedError, match="Forbidden by policy") as exc_info:
            await wrapper.transfer(transfer_request)

        assert exc_info.value.edge_id == "edge-99"

    @pytest.mark.asyncio
    async def test_transfer_approval_required_raises(
        self, wrapper, graph_service, transfer_request
    ):
        from nexus.bricks.governance.models import ConstraintType

        graph_service.check_constraint.return_value = _FakeConstraintCheck(
            allowed=False,
            constraint_type=ConstraintType.REQUIRE_APPROVAL,
            reason="Needs manager sign-off",
            edge_id="edge-55",
        )

        with pytest.raises(GovernanceApprovalRequired, match="Needs manager sign-off"):
            await wrapper.transfer(transfer_request)

    @pytest.mark.asyncio
    async def test_transfer_rate_limit_blocks_by_default(
        self, wrapper, graph_service, transfer_request
    ):
        from nexus.bricks.governance.models import ConstraintType

        graph_service.check_constraint.return_value = _FakeConstraintCheck(
            allowed=False,
            constraint_type=ConstraintType.RATE_LIMIT,
        )

        with pytest.raises(GovernanceBlockedError):
            await wrapper.transfer(transfer_request)

    @pytest.mark.asyncio
    async def test_transfer_unknown_constraint_blocks(
        self, wrapper, graph_service, transfer_request
    ):
        graph_service.check_constraint.return_value = _FakeConstraintCheck(
            allowed=False,
            constraint_type="UNKNOWN_TYPE",
        )

        with pytest.raises(GovernanceBlockedError):
            await wrapper.transfer(transfer_request)


class TestPostAnalysis:
    """Tests for fire-and-forget anomaly analysis after transfer."""

    @pytest.mark.asyncio
    async def test_transfer_fires_anomaly_analysis(self, wrapper, graph_service, transfer_request):
        graph_service.check_constraint.return_value = _FakeConstraintCheck(allowed=True)

        with patch.object(wrapper, "_fire_and_forget_analysis") as mock_analysis:
            await wrapper.transfer(transfer_request)
            mock_analysis.assert_called_once_with(
                agent_id="agent-1",
                zone_id="acme",
                amount=100.0,
                to="agent-2",
            )

    @pytest.mark.asyncio
    async def test_safe_analyze_logs_exception(self, wrapper, anomaly_service, caplog):
        anomaly_service.analyze_transaction.side_effect = RuntimeError("DB down")

        import logging

        with caplog.at_level(logging.ERROR):
            await wrapper._safe_analyze("agent-1", "acme", 50.0, "agent-2")

        assert "Failed to analyze transaction" in caplog.text

    def test_default_zone_id_when_metadata_missing(self, wrapper, graph_service):
        """Zone defaults to 'default' when metadata is empty."""
        from decimal import Decimal

        from nexus.bricks.pay.protocol import ProtocolTransferRequest

        request = ProtocolTransferRequest(
            from_agent="agent-1",
            to="agent-2",
            amount=Decimal("10.0"),
        )

        graph_service.check_constraint.return_value = _FakeConstraintCheck(
            allowed=False,
            constraint_type="BLOCK",
        )

        with pytest.raises(GovernanceBlockedError):
            import asyncio

            asyncio.run(wrapper.transfer(request))
