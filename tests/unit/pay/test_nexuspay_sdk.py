"""Tests for NexusPay unified SDK.

TDD tests for issue #1207: Stripe-simple unified payment SDK
that wraps TigerBeetle + x402.

Test categories:
1. Initialization & configuration
2. Balance operations (get_balance, can_afford)
3. Transfer operations (auto-routing, credits, x402)
4. Batch transfers
5. Reservation operations (reserve, commit, release)
6. Fast metering (meter, check_rate_limit)
7. Decorators (@metered, @budget_limited)
8. Budget context manager
9. Quote & execute
10. Topup request
11. Edge cases & error handling
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.pay.credits import TransferRequest
from nexus.pay.sdk import (
    Balance,
    BudgetExceededError,
    NexusPay,
    NexusPayError,
    Quote,
    Receipt,
    Reservation,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_credits_service():
    """Create a mock CreditsService."""
    service = AsyncMock()
    service.get_balance = AsyncMock(return_value=Decimal("100.0"))
    service.get_balance_with_reserved = AsyncMock(return_value=(Decimal("100.0"), Decimal("0")))
    service.transfer = AsyncMock(return_value="tx-123")
    service.topup = AsyncMock(return_value="topup-123")
    service.reserve = AsyncMock(return_value="res-123")
    service.commit_reservation = AsyncMock()
    service.release_reservation = AsyncMock()
    service.deduct_fast = AsyncMock(return_value=True)
    service.check_budget = AsyncMock(return_value=True)
    service.transfer_batch = AsyncMock(return_value=["tx-1", "tx-2"])
    service.provision_wallet = AsyncMock()
    return service


@pytest.fixture
def mock_x402_client():
    """Create a mock X402Client."""
    from nexus.pay.x402 import X402Receipt

    client = AsyncMock()
    client.pay = AsyncMock(
        return_value=X402Receipt(
            tx_hash="0xabc123",
            network="eip155:8453",
            amount=Decimal("1.00"),
            currency="USDC",
            timestamp=None,
        )
    )
    client.close = AsyncMock()
    return client


@pytest.fixture
def nexuspay(mock_credits_service, mock_x402_client):
    """Create a NexusPay instance with mocked dependencies."""
    pay = NexusPay(
        api_key="nx_live_myagent",
        credits_service=mock_credits_service,
        x402_client=mock_x402_client,
    )
    return pay


@pytest.fixture
def nexuspay_no_x402(mock_credits_service):
    """NexusPay with x402 disabled."""
    pay = NexusPay(
        api_key="nx_live_myagent",
        credits_service=mock_credits_service,
        x402_enabled=False,
    )
    return pay


# =============================================================================
# 1. Initialization & Configuration
# =============================================================================


class TestNexusPayInit:
    """Test NexusPay initialization and configuration."""

    def test_init_with_api_key(self, mock_credits_service):
        pay = NexusPay(
            api_key="nx_live_myagent",
            credits_service=mock_credits_service,
        )
        assert pay.api_key == "nx_live_myagent"
        assert pay.agent_id == "myagent"

    def test_init_extracts_agent_id_from_key(self, mock_credits_service):
        pay = NexusPay(
            api_key="nx_live_agent_bob_42",
            credits_service=mock_credits_service,
        )
        assert pay.agent_id == "agent_bob_42"

    def test_init_test_key(self, mock_credits_service):
        pay = NexusPay(
            api_key="nx_test_demo",
            credits_service=mock_credits_service,
        )
        assert pay.agent_id == "demo"

    def test_init_invalid_key_raises(self, mock_credits_service):
        with pytest.raises(NexusPayError, match="Invalid API key format"):
            NexusPay(
                api_key="bad_key",
                credits_service=mock_credits_service,
            )

    def test_init_x402_disabled(self, mock_credits_service):
        pay = NexusPay(
            api_key="nx_live_test",
            credits_service=mock_credits_service,
            x402_enabled=False,
        )
        assert pay._x402 is None

    def test_init_with_x402_client(self, mock_credits_service, mock_x402_client):
        pay = NexusPay(
            api_key="nx_live_test",
            credits_service=mock_credits_service,
            x402_client=mock_x402_client,
        )
        assert pay._x402 is mock_x402_client


# =============================================================================
# 2. Balance Operations
# =============================================================================


class TestBalanceOperations:
    """Test balance query and affordability checks."""

    @pytest.mark.asyncio
    async def test_get_balance(self, nexuspay, mock_credits_service):
        mock_credits_service.get_balance_with_reserved.return_value = (
            Decimal("50.0"),
            Decimal("10.0"),
        )
        balance = await nexuspay.get_balance()

        assert isinstance(balance, Balance)
        assert balance.available == Decimal("50.0")
        assert balance.reserved == Decimal("10.0")
        assert balance.total == Decimal("60.0")

    @pytest.mark.asyncio
    async def test_get_balance_zero(self, nexuspay, mock_credits_service):
        mock_credits_service.get_balance_with_reserved.return_value = (
            Decimal("0"),
            Decimal("0"),
        )
        balance = await nexuspay.get_balance()
        assert balance.available == Decimal("0")
        assert balance.total == Decimal("0")

    @pytest.mark.asyncio
    async def test_can_afford_true(self, nexuspay, mock_credits_service):
        mock_credits_service.check_budget.return_value = True
        assert await nexuspay.can_afford(amount=10.0) is True

    @pytest.mark.asyncio
    async def test_can_afford_false(self, nexuspay, mock_credits_service):
        mock_credits_service.check_budget.return_value = False
        assert await nexuspay.can_afford(amount=1000.0) is False

    @pytest.mark.asyncio
    async def test_can_afford_decimal_input(self, nexuspay, mock_credits_service):
        mock_credits_service.check_budget.return_value = True
        assert await nexuspay.can_afford(amount=Decimal("5.50")) is True
        mock_credits_service.check_budget.assert_called_once_with(
            nexuspay.agent_id, Decimal("5.50"), zone_id="default"
        )


# =============================================================================
# 3. Transfer Operations
# =============================================================================


class TestTransferOperations:
    """Test transfer with auto-routing and explicit method selection."""

    @pytest.mark.asyncio
    async def test_transfer_internal_auto_routes_to_credits(self, nexuspay, mock_credits_service):
        receipt = await nexuspay.transfer(
            to="agent-bob",
            amount=0.05,
            memo="Task payment",
        )

        assert isinstance(receipt, Receipt)
        assert receipt.method == "credits"
        assert receipt.amount == Decimal("0.05")
        assert receipt.to_agent == "agent-bob"
        assert receipt.from_agent == "myagent"
        mock_credits_service.transfer.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_external_auto_routes_to_x402(self, nexuspay, mock_x402_client):
        receipt = await nexuspay.transfer(
            to="0x1234567890abcdef1234567890abcdef12345678",
            amount=1.0,
            memo="External payment",
        )

        assert isinstance(receipt, Receipt)
        assert receipt.method == "x402"
        assert receipt.tx_hash == "0xabc123"
        mock_x402_client.pay.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_explicit_credits_method(self, nexuspay, mock_credits_service):
        receipt = await nexuspay.transfer(
            to="agent-bob",
            amount=5.0,
            method="credits",
        )
        assert receipt.method == "credits"
        mock_credits_service.transfer.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_explicit_x402_method(self, nexuspay, mock_x402_client):
        receipt = await nexuspay.transfer(
            to="0x1234567890abcdef1234567890abcdef12345678",
            amount=1.0,
            method="x402",
        )
        assert receipt.method == "x402"
        mock_x402_client.pay.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_with_idempotency_key(self, nexuspay, mock_credits_service):
        await nexuspay.transfer(
            to="agent-bob",
            amount=1.0,
            idempotency_key="task-123-payment",
        )
        call_kwargs = mock_credits_service.transfer.call_args
        assert call_kwargs[1]["idempotency_key"] == "task-123-payment"

    @pytest.mark.asyncio
    async def test_transfer_x402_disabled_raises_for_external(self, nexuspay_no_x402):
        with pytest.raises(NexusPayError, match="x402 not enabled"):
            await nexuspay_no_x402.transfer(
                to="0x1234567890abcdef1234567890abcdef12345678",
                amount=1.0,
            )

    @pytest.mark.asyncio
    async def test_transfer_returns_receipt_with_memo(self, nexuspay, mock_credits_service):
        receipt = await nexuspay.transfer(
            to="agent-bob",
            amount=0.05,
            memo="Task payment",
        )
        assert receipt.memo == "Task payment"

    @pytest.mark.asyncio
    async def test_transfer_float_amount_converted_to_decimal(self, nexuspay, mock_credits_service):
        receipt = await nexuspay.transfer(to="agent-bob", amount=0.1)
        assert receipt.amount == Decimal("0.1")


# =============================================================================
# 4. Batch Transfers
# =============================================================================


class TestBatchTransfers:
    """Test atomic batch transfers."""

    @pytest.mark.asyncio
    async def test_transfer_batch(self, nexuspay, mock_credits_service):
        receipts = await nexuspay.transfer_batch(
            [
                TransferRequest(from_id="ignored", to_id="agent-a", amount=Decimal("0.05"), memo="Task 1"),
                TransferRequest(from_id="ignored", to_id="agent-b", amount=Decimal("0.10"), memo="Task 2"),
            ]
        )

        assert len(receipts) == 2
        assert all(isinstance(r, Receipt) for r in receipts)
        mock_credits_service.transfer_batch.assert_called_once()

    @pytest.mark.asyncio
    async def test_transfer_batch_empty(self, nexuspay):
        receipts = await nexuspay.transfer_batch([])
        assert receipts == []

    @pytest.mark.asyncio
    async def test_transfer_batch_preserves_memos(self, nexuspay, mock_credits_service):
        receipts = await nexuspay.transfer_batch(
            [
                TransferRequest(from_id="ignored", to_id="agent-a", amount=Decimal("1.0"), memo="First"),
                TransferRequest(from_id="ignored", to_id="agent-b", amount=Decimal("2.0"), memo="Second"),
            ]
        )
        assert receipts[0].memo == "First"
        assert receipts[1].memo == "Second"


# =============================================================================
# 5. Reservation Operations
# =============================================================================


class TestReservationOperations:
    """Test two-phase reserve/commit/release."""

    @pytest.mark.asyncio
    async def test_reserve(self, nexuspay, mock_credits_service):
        reservation = await nexuspay.reserve(
            amount=10.0,
            timeout=300,
            purpose="task",
            task_id="task-456",
        )

        assert isinstance(reservation, Reservation)
        assert reservation.amount == Decimal("10.0")
        assert reservation.purpose == "task"
        assert reservation.status == "pending"
        mock_credits_service.reserve.assert_called_once()

    @pytest.mark.asyncio
    async def test_commit(self, nexuspay, mock_credits_service):
        mock_credits_service.reserve.return_value = "res-456"
        reservation = await nexuspay.reserve(amount=10.0)

        await nexuspay.commit(reservation.id, actual_amount=7.50)
        mock_credits_service.commit_reservation.assert_called_once_with(
            "res-456", actual_amount=Decimal("7.50")
        )

    @pytest.mark.asyncio
    async def test_release(self, nexuspay, mock_credits_service):
        mock_credits_service.reserve.return_value = "res-789"
        reservation = await nexuspay.reserve(amount=10.0)

        await nexuspay.release(reservation.id)
        mock_credits_service.release_reservation.assert_called_once_with("res-789")

    @pytest.mark.asyncio
    async def test_reserve_default_timeout(self, nexuspay, mock_credits_service):
        await nexuspay.reserve(amount=5.0)
        call_kwargs = mock_credits_service.reserve.call_args
        assert call_kwargs[1]["timeout_seconds"] == 300


# =============================================================================
# 6. Fast Metering Operations
# =============================================================================


class TestMeteringOperations:
    """Test fast metering and rate limiting."""

    @pytest.mark.asyncio
    async def test_meter_success(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = True
        success = await nexuspay.meter(amount=0.001, event_type="api_call")
        assert success is True
        mock_credits_service.deduct_fast.assert_called_once()

    @pytest.mark.asyncio
    async def test_meter_insufficient_balance(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = False
        success = await nexuspay.meter(amount=0.001)
        assert success is False

    @pytest.mark.asyncio
    async def test_check_rate_limit_allowed(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = True
        assert await nexuspay.check_rate_limit(cost=1) is True

    @pytest.mark.asyncio
    async def test_check_rate_limit_denied(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = False
        assert await nexuspay.check_rate_limit(cost=1) is False

    @pytest.mark.asyncio
    async def test_bid_priority(self, nexuspay, mock_credits_service):
        reservation = await nexuspay.bid_priority(
            queue="task_queue",
            bid=0.10,
            task_id="task-789",
            timeout=300,
        )
        assert isinstance(reservation, Reservation)
        assert reservation.purpose == "priority_bid"
        mock_credits_service.reserve.assert_called_once()


# =============================================================================
# 7. Decorators
# =============================================================================


class TestDecorators:
    """Test @metered and @budget_limited decorators."""

    @pytest.mark.asyncio
    async def test_metered_decorator_charges(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = True

        @nexuspay.metered(price=0.001)
        async def expensive_search(query: str):
            return f"results for {query}"

        result = await expensive_search("test")
        assert result == "results for test"
        mock_credits_service.deduct_fast.assert_called_once()

    @pytest.mark.asyncio
    async def test_metered_decorator_blocks_on_insufficient(self, nexuspay, mock_credits_service):
        mock_credits_service.deduct_fast.return_value = False

        @nexuspay.metered(price=0.001)
        async def expensive_search(query: str):
            return f"results for {query}"

        with pytest.raises(NexusPayError, match="Insufficient credits"):
            await expensive_search("test")

    @pytest.mark.asyncio
    async def test_budget_limited_decorator(self, nexuspay, mock_credits_service):
        call_count = 0

        @nexuspay.budget_limited(max_cost=1.0)
        async def process_document(doc: str):
            nonlocal call_count
            call_count += 1
            return f"processed {doc}"

        result = await process_document("doc1")
        assert result == "processed doc1"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_budget_limited_exceeds_budget(self, nexuspay, mock_credits_service):
        mock_credits_service.check_budget.return_value = False

        @nexuspay.budget_limited(max_cost=1.0)
        async def process_document(doc: str):
            return f"processed {doc}"

        with pytest.raises(BudgetExceededError):
            await process_document("doc1")


# =============================================================================
# 8. Budget Context Manager
# =============================================================================


class TestBudgetContextManager:
    """Test budget-limited agent context."""

    @pytest.mark.asyncio
    async def test_budget_context_basic(self, nexuspay, mock_credits_service):
        async with nexuspay.budget(daily=10.0, per_tx=1.0) as agent:
            receipt = await agent.transfer(to="bob", amount=0.50)
            assert isinstance(receipt, Receipt)

    @pytest.mark.asyncio
    async def test_budget_context_per_tx_exceeded(self, nexuspay, mock_credits_service):
        async with nexuspay.budget(daily=10.0, per_tx=1.0) as agent:
            with pytest.raises(BudgetExceededError, match="per-transaction"):
                await agent.transfer(to="bob", amount=5.0)

    @pytest.mark.asyncio
    async def test_budget_context_daily_exceeded(self, nexuspay, mock_credits_service):
        async with nexuspay.budget(daily=1.0, per_tx=0.50) as agent:
            await agent.transfer(to="bob", amount=0.50)
            await agent.transfer(to="bob", amount=0.50)
            with pytest.raises(BudgetExceededError, match="daily"):
                await agent.transfer(to="bob", amount=0.50)

    @pytest.mark.asyncio
    async def test_budget_context_tracks_spending(self, nexuspay, mock_credits_service):
        async with nexuspay.budget(daily=10.0, per_tx=5.0) as agent:
            await agent.transfer(to="bob", amount=3.0)
            await agent.transfer(to="alice", amount=2.0)
            assert agent.spent == Decimal("5.0")
            assert agent.remaining == Decimal("5.0")


# =============================================================================
# 9. Quote & Execute
# =============================================================================


class TestQuoteAndExecute:
    """Test quote and execute for external services."""

    @pytest.mark.asyncio
    async def test_quote(self, nexuspay, mock_x402_client):
        mock_x402_client.pay_for_request = AsyncMock(
            return_value=(MagicMock(status_code=200, json=lambda: {"price": "0.05"}), None)
        )
        quote = await nexuspay.quote(
            service="firecrawl.scrape",
            params={"url": "https://example.com"},
        )
        assert isinstance(quote, Quote)
        assert quote.service == "firecrawl.scrape"

    @pytest.mark.asyncio
    async def test_quote_execute(self, nexuspay, mock_x402_client):
        from nexus.pay.x402 import X402Receipt

        mock_x402_client.pay = AsyncMock(
            return_value=X402Receipt(
                tx_hash="0xdef456",
                network="eip155:8453",
                amount=Decimal("0.05"),
                currency="USDC",
                timestamp=None,
            )
        )

        quote = Quote(
            id="q-123",
            service="firecrawl.scrape",
            price=Decimal("0.05"),
            params={"url": "https://example.com"},
            nexuspay=nexuspay,
        )
        receipt = await quote.execute()
        assert isinstance(receipt, Receipt)


# =============================================================================
# 10. Topup Request
# =============================================================================


class TestTopupRequest:
    """Test external topup request."""

    @pytest.mark.asyncio
    async def test_request_topup(self, nexuspay, mock_x402_client):
        mock_x402_client.wallet_address = "0xwallet123"
        topup = await nexuspay.request_topup(amount=100.0)
        assert topup["amount"] == Decimal("100.0")
        assert "address" in topup


# =============================================================================
# 11. Edge Cases & Error Handling
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_transfer_negative_amount_raises(self, nexuspay):
        with pytest.raises(NexusPayError, match="positive"):
            await nexuspay.transfer(to="bob", amount=-1.0)

    @pytest.mark.asyncio
    async def test_transfer_zero_amount_raises(self, nexuspay):
        with pytest.raises(NexusPayError, match="positive"):
            await nexuspay.transfer(to="bob", amount=0)

    @pytest.mark.asyncio
    async def test_reserve_negative_amount_raises(self, nexuspay):
        with pytest.raises(NexusPayError, match="positive"):
            await nexuspay.reserve(amount=-5.0)

    @pytest.mark.asyncio
    async def test_meter_negative_amount_raises(self, nexuspay):
        with pytest.raises(NexusPayError, match="positive"):
            await nexuspay.meter(amount=-0.001)

    @pytest.mark.asyncio
    async def test_is_external_detects_wallet_addresses(self, nexuspay):
        assert nexuspay._is_external("0x1234567890abcdef1234567890abcdef12345678") is True
        assert nexuspay._is_external("agent-bob") is False
        assert nexuspay._is_external("my-service") is False

    def test_receipt_dataclass(self):
        receipt = Receipt(
            id="tx-1",
            method="credits",
            amount=Decimal("5.0"),
            from_agent="alice",
            to_agent="bob",
            memo="test",
            timestamp=None,
            tx_hash=None,
        )
        assert receipt.id == "tx-1"
        assert receipt.amount == Decimal("5.0")

    def test_balance_dataclass(self):
        balance = Balance(
            available=Decimal("90"),
            reserved=Decimal("10"),
        )
        assert balance.total == Decimal("100")

    def test_reservation_dataclass(self):
        reservation = Reservation(
            id="res-1",
            amount=Decimal("10"),
            purpose="task",
            expires_at=None,
            status="pending",
        )
        assert reservation.status == "pending"

    @pytest.mark.asyncio
    async def test_transfer_very_small_amount(self, nexuspay, mock_credits_service):
        """Micro-payments should work (e.g., API metering at 0.000001)."""
        receipt = await nexuspay.transfer(to="bob", amount=Decimal("0.000001"))
        assert receipt.amount == Decimal("0.000001")

    @pytest.mark.asyncio
    async def test_transfer_large_amount(self, nexuspay, mock_credits_service):
        """Large transfers should work without overflow."""
        receipt = await nexuspay.transfer(to="bob", amount=Decimal("999999999"))
        assert receipt.amount == Decimal("999999999")

    def test_api_key_variations(self, mock_credits_service):
        """Various valid key formats."""
        pay = NexusPay(api_key="nx_live_a", credits_service=mock_credits_service)
        assert pay.agent_id == "a"

        pay = NexusPay(api_key="nx_test_agent-with-dashes", credits_service=mock_credits_service)
        assert pay.agent_id == "agent-with-dashes"

        pay = NexusPay(api_key="nx_live_agent.with.dots", credits_service=mock_credits_service)
        assert pay.agent_id == "agent.with.dots"

    def test_api_key_empty_suffix_raises(self, mock_credits_service):
        with pytest.raises(NexusPayError, match="Invalid API key"):
            NexusPay(api_key="nx_live_", credits_service=mock_credits_service)

    @pytest.mark.asyncio
    async def test_topup_without_x402_raises(self, nexuspay_no_x402):
        with pytest.raises(NexusPayError, match="x402 not enabled"):
            await nexuspay_no_x402.request_topup(amount=100.0)

    @pytest.mark.asyncio
    async def test_topup_negative_amount_raises(self, nexuspay):
        with pytest.raises(NexusPayError, match="positive"):
            await nexuspay.request_topup(amount=-50.0)

    @pytest.mark.asyncio
    async def test_quote_without_x402(self, nexuspay_no_x402):
        """Quote should still be created even without x402 (local quote)."""
        quote = await nexuspay_no_x402.quote(service="test.service")
        assert isinstance(quote, Quote)

    @pytest.mark.asyncio
    async def test_quote_execute_without_nexuspay_raises(self):
        """Executing an unbound quote should raise."""
        quote = Quote(id="q-1", service="test", price=Decimal("1.0"))
        with pytest.raises(NexusPayError, match="not bound"):
            await quote.execute()

    @pytest.mark.asyncio
    async def test_budget_context_zero_daily_blocks_all(self, nexuspay, mock_credits_service):
        async with nexuspay.budget(daily=0, per_tx=1.0) as agent:
            with pytest.raises(BudgetExceededError, match="daily"):
                await agent.transfer(to="bob", amount=0.01)

    @pytest.mark.asyncio
    async def test_metered_preserves_function_name(self, nexuspay):
        @nexuspay.metered(price=0.001)
        async def my_cool_function():
            pass

        assert my_cool_function.__name__ == "my_cool_function"

    @pytest.mark.asyncio
    async def test_budget_limited_preserves_function_name(self, nexuspay):
        @nexuspay.budget_limited(max_cost=1.0)
        async def my_other_function():
            pass

        assert my_other_function.__name__ == "my_other_function"

    @pytest.mark.asyncio
    async def test_credits_service_error_propagates(self, nexuspay, mock_credits_service):
        """Errors from CreditsService should propagate through NexusPay."""
        from nexus.pay.credits import InsufficientCreditsError

        mock_credits_service.transfer.side_effect = InsufficientCreditsError("Not enough")
        with pytest.raises(InsufficientCreditsError):
            await nexuspay.transfer(to="bob", amount=100.0)

    @pytest.mark.asyncio
    async def test_x402_error_propagates(self, nexuspay, mock_x402_client):
        """Errors from X402Client should propagate through NexusPay."""
        from nexus.pay.x402 import X402Error

        mock_x402_client.pay.side_effect = X402Error("Payment failed")
        with pytest.raises(X402Error):
            await nexuspay.transfer(
                to="0x1234567890abcdef1234567890abcdef12345678",
                amount=1.0,
            )
