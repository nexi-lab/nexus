"""Tests for PolicyEnforcedPayment wrapper.

Issue #1358: Recursive wrapper (Lego Mechanism 2) that enforces
spending policies before delegating to inner PaymentProtocol.

Test categories:
1. Protocol interface delegation (can_handle, protocol_name)
2. Transfer allowed → delegates to inner + records spending
3. Transfer denied → raises PolicyDeniedError, inner NOT called
4. Error propagation from inner protocol
5. Fire-and-forget spending recording
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.policy_wrapper import PolicyEnforcedPayment
from nexus.pay.protocol import ProtocolTransferRequest, ProtocolTransferResult
from nexus.pay.spending_policy import PolicyDeniedError, PolicyEvaluation

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_inner_protocol():
    """Mock PaymentProtocol (inner) with sensible defaults."""
    inner = MagicMock()
    inner.protocol_name = TransactionProtocol.INTERNAL
    inner.can_handle = MagicMock(return_value=True)
    inner.transfer = AsyncMock(
        return_value=ProtocolTransferResult(
            protocol=TransactionProtocol.INTERNAL,
            tx_id="tx-123",
            amount=Decimal("10"),
            from_agent="agent-a",
            to="agent-b",
        )
    )
    return inner


@pytest.fixture
def mock_policy_service():
    """Mock SpendingPolicyService."""
    service = AsyncMock()
    service.evaluate = AsyncMock(return_value=PolicyEvaluation(allowed=True))
    service.record_spending = AsyncMock()
    service.check_approval = AsyncMock(return_value=None)
    return service


@pytest.fixture
def wrapper(mock_inner_protocol, mock_policy_service):
    """PolicyEnforcedPayment wrapper with mocked dependencies."""
    return PolicyEnforcedPayment(inner=mock_inner_protocol, policy_service=mock_policy_service)


def _make_request(
    from_agent: str = "agent-a",
    to: str = "agent-b",
    amount: Decimal = Decimal("10"),
    zone_id: str = "default",
) -> ProtocolTransferRequest:
    return ProtocolTransferRequest(
        from_agent=from_agent,
        to=to,
        amount=amount,
        metadata={"zone_id": zone_id},
    )


# =============================================================================
# 1. Protocol Interface Delegation
# =============================================================================


class TestProtocolDelegation:
    """Wrapper delegates protocol_name and can_handle to inner."""

    def test_protocol_name_delegates(self, wrapper, mock_inner_protocol):
        assert wrapper.protocol_name == TransactionProtocol.INTERNAL

    def test_can_handle_delegates(self, wrapper, mock_inner_protocol):
        result = wrapper.can_handle("agent-b", {"foo": "bar"})
        assert result is True
        mock_inner_protocol.can_handle.assert_called_once_with("agent-b", {"foo": "bar"})

    def test_can_handle_returns_false(self, wrapper, mock_inner_protocol):
        mock_inner_protocol.can_handle.return_value = False
        assert wrapper.can_handle("unknown") is False


# =============================================================================
# 2. Transfer Allowed
# =============================================================================


class TestTransferAllowed:
    """When policy allows, wrapper delegates to inner and records spending."""

    @pytest.mark.asyncio
    async def test_transfer_delegates_to_inner(
        self, wrapper, mock_inner_protocol, mock_policy_service
    ):
        request = _make_request()
        result = await wrapper.transfer(request)

        assert result.tx_id == "tx-123"
        mock_inner_protocol.transfer.assert_called_once_with(request)
        mock_policy_service.evaluate.assert_called_once_with(
            agent_id="agent-a",
            zone_id="default",
            amount=Decimal("10"),
            to="agent-b",
            metadata={"zone_id": "default"},
        )

    @pytest.mark.asyncio
    async def test_records_spending_on_success(self, wrapper, mock_policy_service):
        """After successful transfer, spending is recorded."""
        request = _make_request()
        await wrapper.transfer(request)

        # Give the fire-and-forget task a moment
        import asyncio

        await asyncio.sleep(0.01)

        mock_policy_service.record_spending.assert_called_once_with(
            agent_id="agent-a",
            zone_id="default",
            amount=Decimal("10"),
        )

    @pytest.mark.asyncio
    async def test_uses_zone_from_metadata(self, wrapper, mock_policy_service):
        """Zone ID is extracted from request metadata."""
        request = _make_request(zone_id="custom-zone")
        await wrapper.transfer(request)

        mock_policy_service.evaluate.assert_called_once_with(
            agent_id="agent-a",
            zone_id="custom-zone",
            amount=Decimal("10"),
            to="agent-b",
            metadata={"zone_id": "custom-zone"},
        )

    @pytest.mark.asyncio
    async def test_default_zone_when_no_metadata(
        self, wrapper, mock_policy_service, mock_inner_protocol
    ):
        """Uses 'default' zone when metadata is empty."""
        request = ProtocolTransferRequest(
            from_agent="agent-a",
            to="agent-b",
            amount=Decimal("10"),
        )
        await wrapper.transfer(request)

        mock_policy_service.evaluate.assert_called_once_with(
            agent_id="agent-a",
            zone_id="default",
            amount=Decimal("10"),
            to="agent-b",
            metadata={},
        )


# =============================================================================
# 3. Transfer Denied
# =============================================================================


class TestTransferDenied:
    """When policy denies, wrapper raises PolicyDeniedError and does NOT call inner."""

    @pytest.mark.asyncio
    async def test_raises_policy_denied(self, wrapper, mock_inner_protocol, mock_policy_service):
        mock_policy_service.evaluate.return_value = PolicyEvaluation(
            allowed=False,
            denied_reason="Exceeds daily limit",
            policy_id="p1",
        )

        with pytest.raises(PolicyDeniedError) as exc_info:
            await wrapper.transfer(_make_request())

        assert "daily limit" in str(exc_info.value)
        assert exc_info.value.policy_id == "p1"
        mock_inner_protocol.transfer.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_spending_recorded_on_denial(self, wrapper, mock_policy_service):
        mock_policy_service.evaluate.return_value = PolicyEvaluation(
            allowed=False, denied_reason="Over budget"
        )

        with pytest.raises(PolicyDeniedError):
            await wrapper.transfer(_make_request())

        mock_policy_service.record_spending.assert_not_called()


# =============================================================================
# 4. Error Propagation
# =============================================================================


class TestErrorPropagation:
    """Errors from inner protocol propagate without spending recorded."""

    @pytest.mark.asyncio
    async def test_inner_error_propagates(self, wrapper, mock_inner_protocol, mock_policy_service):
        from nexus.pay.protocol import ProtocolError

        mock_inner_protocol.transfer.side_effect = ProtocolError("TigerBeetle down")

        with pytest.raises(ProtocolError, match="TigerBeetle down"):
            await wrapper.transfer(_make_request())

    @pytest.mark.asyncio
    async def test_no_spending_on_inner_error(
        self, wrapper, mock_inner_protocol, mock_policy_service
    ):
        from nexus.pay.credits import InsufficientCreditsError

        mock_inner_protocol.transfer.side_effect = InsufficientCreditsError("No balance")

        with pytest.raises(InsufficientCreditsError):
            await wrapper.transfer(_make_request())

        # Wait for any potential fire-and-forget tasks
        import asyncio

        await asyncio.sleep(0.01)

        mock_policy_service.record_spending.assert_not_called()


# =============================================================================
# 5. Multiple Sequential Transfers
# =============================================================================


class TestSequentialTransfers:
    """Multiple transfers accumulate correctly."""

    @pytest.mark.asyncio
    async def test_multiple_transfers_evaluate_independently(self, wrapper, mock_policy_service):
        """Each transfer triggers its own evaluation."""
        for i in range(3):
            await wrapper.transfer(_make_request(amount=Decimal(str(i + 1))))

        assert mock_policy_service.evaluate.call_count == 3
