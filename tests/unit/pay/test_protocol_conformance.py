"""Parametrized conformance suite for PaymentProtocol implementations.

Issue #1357 Phase 1: Verifies that all concrete protocol implementations
conform to the PaymentProtocol ABC contract.

Every protocol must:
    - Have a valid TransactionProtocol protocol_name
    - Return bool from can_handle()
    - Return ProtocolTransferResult from transfer()
    - Set valid result fields (protocol, tx_id, amount, from_agent, to)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from nexus.pay.audit_types import TransactionProtocol
from nexus.pay.protocol import (
    CreditsPaymentProtocol,
    PaymentProtocol,
    ProtocolTransferRequest,
    ProtocolTransferResult,
    X402PaymentProtocol,
)
from nexus.pay.x402 import X402Receipt

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(params=["x402", "credits"])
def protocol_with_destination(request) -> tuple[PaymentProtocol, str]:
    """Parametrized fixture providing (protocol, valid_destination) pairs."""
    if request.param == "x402":
        mock_client = AsyncMock()
        mock_client.pay = AsyncMock(
            return_value=X402Receipt(
                tx_hash="0xconformance",
                network="eip155:8453",
                amount=Decimal("1.0"),
                currency="USDC",
                timestamp=None,
            )
        )
        proto = X402PaymentProtocol(client=mock_client)
        return proto, "0x1234567890abcdef1234567890abcdef12345678"
    else:
        mock_service = AsyncMock()
        mock_service.transfer = AsyncMock(return_value="tx-conformance")
        proto = CreditsPaymentProtocol(service=mock_service, zone_id="default")
        return proto, "agent-bob"


# =============================================================================
# Conformance Tests
# =============================================================================


class TestProtocolConformance:
    """Every PaymentProtocol implementation must pass these tests."""

    def test_protocol_name_is_transaction_protocol(self, protocol_with_destination):
        proto, _ = protocol_with_destination
        assert isinstance(proto.protocol_name, TransactionProtocol)

    def test_can_handle_returns_bool(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        result = proto.can_handle(dest)
        assert isinstance(result, bool)
        assert result is True  # fixture provides matching destination

    def test_can_handle_with_metadata_returns_bool(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        result = proto.can_handle(dest, metadata={"key": "value"})
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_transfer_returns_protocol_transfer_result(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="test-sender",
            to=dest,
            amount=Decimal("1.0"),
            memo="conformance test",
        )
        result = await proto.transfer(request)
        assert isinstance(result, ProtocolTransferResult)

    @pytest.mark.asyncio
    async def test_transfer_result_protocol_matches(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="test-sender",
            to=dest,
            amount=Decimal("1.0"),
        )
        result = await proto.transfer(request)
        assert result.protocol == proto.protocol_name

    @pytest.mark.asyncio
    async def test_transfer_result_has_tx_id(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="test-sender",
            to=dest,
            amount=Decimal("1.0"),
        )
        result = await proto.transfer(request)
        assert result.tx_id is not None
        assert len(result.tx_id) > 0

    @pytest.mark.asyncio
    async def test_transfer_result_preserves_amount(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="test-sender",
            to=dest,
            amount=Decimal("42.50"),
        )
        result = await proto.transfer(request)
        assert result.amount == Decimal("42.50")

    @pytest.mark.asyncio
    async def test_transfer_result_preserves_from_agent(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="conformance-agent",
            to=dest,
            amount=Decimal("1.0"),
        )
        result = await proto.transfer(request)
        assert result.from_agent == "conformance-agent"

    @pytest.mark.asyncio
    async def test_transfer_result_preserves_to(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="sender",
            to=dest,
            amount=Decimal("1.0"),
        )
        result = await proto.transfer(request)
        assert result.to == dest

    @pytest.mark.asyncio
    async def test_transfer_result_metadata_is_dict(self, protocol_with_destination):
        proto, dest = protocol_with_destination
        request = ProtocolTransferRequest(
            from_agent="sender",
            to=dest,
            amount=Decimal("1.0"),
        )
        result = await proto.transfer(request)
        assert isinstance(result.metadata, dict)

    def test_is_payment_protocol_subclass(self, protocol_with_destination):
        proto, _ = protocol_with_destination
        assert isinstance(proto, PaymentProtocol)
