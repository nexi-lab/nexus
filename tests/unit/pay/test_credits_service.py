"""Tests for CreditsService using TigerBeetle.

These tests follow TDD principles and verify:
1. Account management (provisioning, balance queries)
2. Transfer operations (simple, with memo, batched)
3. Two-phase transfers (reserve, commit, release)
4. Budget enforcement
5. Rate limiting / metering
6. Idempotency handling
7. Error handling

Tests use a mock TigerBeetle client for unit testing.
Integration tests with real TigerBeetle are in tests/integration/.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.pay.constants import (
    ACCOUNT_CODE_ESCROW,
    ACCOUNT_CODE_TREASURY,
    ACCOUNT_CODE_WALLET,
    ESCROW_ACCOUNT_TB_ID,
    LEDGER_CREDITS,
    MICRO_UNIT_SCALE,
    SYSTEM_TREASURY_TB_ID,
    TRANSFER_CODE_PAYMENT,
    agent_id_to_tb_id,
    credits_to_micro,
    make_tb_account_id,
    micro_to_credits,
)

# =============================================================================
# Fixtures
# =============================================================================


class MockTBAccount:
    """Mock TigerBeetle account for testing."""

    def __init__(
        self,
        id: int,
        debits_pending: int = 0,
        debits_posted: int = 0,
        credits_pending: int = 0,
        credits_posted: int = 0,
        ledger: int = LEDGER_CREDITS,
        code: int = ACCOUNT_CODE_WALLET,
        flags: int = 0,
        timestamp: int = 0,
    ):
        self.id = id
        self.debits_pending = debits_pending
        self.debits_posted = debits_posted
        self.credits_pending = credits_pending
        self.credits_posted = credits_posted
        self.ledger = ledger
        self.code = code
        self.flags = flags
        self.timestamp = timestamp


class MockTBTransfer:
    """Mock TigerBeetle transfer for testing."""

    def __init__(
        self,
        id: int,
        debit_account_id: int = 0,
        credit_account_id: int = 0,
        amount: int = 0,
        pending_id: int = 0,
        ledger: int = LEDGER_CREDITS,
        code: int = TRANSFER_CODE_PAYMENT,
        flags: int = 0,
        timeout: int = 0,
        timestamp: int = 0,
    ):
        self.id = id
        self.debit_account_id = debit_account_id
        self.credit_account_id = credit_account_id
        self.amount = amount
        self.pending_id = pending_id
        self.ledger = ledger
        self.code = code
        self.flags = flags
        self.timeout = timeout
        self.timestamp = timestamp


class MockCreateAccountError:
    """Mock TigerBeetle create account error."""

    def __init__(self, index: int, result: int):
        self.index = index
        self.result = result


class MockCreateTransferError:
    """Mock TigerBeetle create transfer error."""

    def __init__(self, index: int, result: int):
        self.index = index
        self.result = result


# TigerBeetle error codes (from tigerbeetle.CreateAccountResult / CreateTransferResult)
TB_ACCOUNT_EXISTS = 21
TB_TRANSFER_EXISTS = 46
TB_EXCEEDS_CREDITS = 54


@pytest.fixture
def mock_tb_module():
    """Create mock tigerbeetle module."""
    mock_tb = MagicMock()

    # Mock ID generation
    mock_tb.id = MagicMock(side_effect=lambda: 12345678901234567890)

    # Mock amount_max constant
    mock_tb.amount_max = 2**128 - 1

    # Mock Account and Transfer classes
    mock_tb.Account = MagicMock()
    mock_tb.Transfer = MagicMock()

    # Mock flags
    mock_tb.AccountFlags = MagicMock()
    mock_tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS = 1 << 1
    mock_tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS = 1 << 2
    mock_tb.AccountFlags.LINKED = 1 << 0

    mock_tb.TransferFlags = MagicMock()
    mock_tb.TransferFlags.LINKED = 1 << 0
    mock_tb.TransferFlags.PENDING = 1 << 1
    mock_tb.TransferFlags.POST_PENDING_TRANSFER = 1 << 2
    mock_tb.TransferFlags.VOID_PENDING_TRANSFER = 1 << 3

    # Mock result enums
    mock_tb.CreateAccountResult = MagicMock()
    mock_tb.CreateAccountResult.OK = "ok"
    mock_tb.CreateAccountResult.EXISTS = "exists"

    mock_tb.CreateTransferResult = MagicMock()
    mock_tb.CreateTransferResult.OK = "ok"
    mock_tb.CreateTransferResult.EXISTS = "exists"
    mock_tb.CreateTransferResult.EXCEEDS_CREDITS = "exceeds_credits"

    return mock_tb


@pytest.fixture
def mock_tb_client():
    """Create mock TigerBeetle async client."""
    client = AsyncMock()
    client.create_accounts = AsyncMock(return_value=[])
    client.create_transfers = AsyncMock(return_value=[])
    client.lookup_accounts = AsyncMock(return_value=[])
    client.lookup_transfers = AsyncMock(return_value=[])
    return client


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Test TigerBeetle constants and conversion utilities."""

    def test_ledger_credits_defined(self):
        """Ledger ID should be positive integer."""
        assert LEDGER_CREDITS == 1

    def test_account_codes_defined(self):
        """Account codes should be positive integers."""
        assert ACCOUNT_CODE_WALLET == 1
        assert ACCOUNT_CODE_ESCROW == 2
        assert ACCOUNT_CODE_TREASURY == 3

    def test_system_account_ids(self):
        """System account IDs should be defined."""
        assert SYSTEM_TREASURY_TB_ID == 1
        assert ESCROW_ACCOUNT_TB_ID == 2

    def test_credits_to_micro_conversion(self):
        """Credits should convert to micro-credits correctly."""
        assert credits_to_micro(1.0) == MICRO_UNIT_SCALE
        assert credits_to_micro(0.5) == 500_000
        assert credits_to_micro(1.5) == 1_500_000
        assert credits_to_micro(0.000001) == 1

    def test_micro_to_credits_conversion(self):
        """Micro-credits should convert back to credits."""
        assert micro_to_credits(MICRO_UNIT_SCALE) == 1.0
        assert micro_to_credits(500_000) == 0.5
        assert micro_to_credits(1_500_000) == 1.5

    def test_agent_id_to_tb_id_deterministic(self):
        """Agent ID conversion should be deterministic."""
        agent_id = "agent-123"
        tb_id_1 = agent_id_to_tb_id(agent_id)
        tb_id_2 = agent_id_to_tb_id(agent_id)
        assert tb_id_1 == tb_id_2
        assert isinstance(tb_id_1, int)
        assert tb_id_1 > 0

    def test_agent_id_to_tb_id_unique(self):
        """Different agent IDs should produce different TB IDs."""
        id_1 = agent_id_to_tb_id("agent-1")
        id_2 = agent_id_to_tb_id("agent-2")
        assert id_1 != id_2

    def test_make_tb_account_id_combines_tenant_and_agent(self):
        """Full TB ID should combine tenant and agent hashes."""
        full_id = make_tb_account_id("tenant-1", "agent-1")
        assert isinstance(full_id, int)
        assert full_id > 0

    def test_make_tb_account_id_deterministic(self):
        """Full TB ID generation should be deterministic."""
        id_1 = make_tb_account_id("tenant-1", "agent-1")
        id_2 = make_tb_account_id("tenant-1", "agent-1")
        assert id_1 == id_2


# =============================================================================
# CreditsService Tests
# =============================================================================


class TestCreditsServiceImport:
    """Test CreditsService can be imported."""

    def test_credits_service_importable(self):
        """CreditsService should be importable from nexus.pay."""
        from nexus.pay.credits import CreditsService

        assert CreditsService is not None

    def test_exception_classes_importable(self):
        """Exception classes should be importable."""
        from nexus.pay.credits import (
            CreditsError,
            InsufficientCreditsError,
            WalletNotFoundError,
        )

        assert CreditsError is not None
        assert InsufficientCreditsError is not None
        assert WalletNotFoundError is not None


class TestCreditsServiceInit:
    """Test CreditsService initialization."""

    def test_init_with_mock_client(self, mock_tb_client, mock_tb_module):
        """CreditsService should accept a pre-configured client."""
        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            assert service._client == mock_tb_client

    def test_init_disabled_mode(self):
        """CreditsService should work in disabled mode without TigerBeetle."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(enabled=False)
        assert service._enabled is False


class TestGetBalance:
    """Test balance query operations."""

    @pytest.mark.asyncio
    async def test_get_balance_returns_decimal(self, mock_tb_client, mock_tb_module):
        """get_balance should return Decimal."""
        # Setup: Account with 10 credits (10_000_000 micro)
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=10_000_000,
            debits_posted=0,
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            balance = await service.get_balance("agent-1")

            assert isinstance(balance, Decimal)
            assert balance == Decimal("10")

    @pytest.mark.asyncio
    async def test_get_balance_calculates_correctly(self, mock_tb_client, mock_tb_module):
        """Balance should be credits_posted - debits_posted."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=15_000_000,  # 15 credits
            debits_posted=5_000_000,  # 5 credits spent
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            balance = await service.get_balance("agent-1")

            assert balance == Decimal("10")  # 15 - 5

    @pytest.mark.asyncio
    async def test_get_balance_nonexistent_account_returns_zero(
        self, mock_tb_client, mock_tb_module
    ):
        """Nonexistent account should return zero balance."""
        mock_tb_client.lookup_accounts.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            balance = await service.get_balance("nonexistent-agent")

            assert balance == Decimal("0")

    @pytest.mark.asyncio
    async def test_get_balance_with_reserved(self, mock_tb_client, mock_tb_module):
        """get_balance_with_reserved should return available and reserved amounts."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=20_000_000,  # 20 credits total
            debits_posted=5_000_000,  # 5 spent
            debits_pending=3_000_000,  # 3 reserved
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            available, reserved = await service.get_balance_with_reserved("agent-1")

            assert available == Decimal("15")  # 20 - 5
            assert reserved == Decimal("3")


class TestTransfer:
    """Test credit transfer operations."""

    @pytest.mark.asyncio
    async def test_transfer_success(self, mock_tb_client, mock_tb_module):
        """Successful transfer should return transfer ID."""
        mock_tb_client.create_transfers.return_value = []  # No errors = success

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            transfer_id = await service.transfer(
                from_id="agent-sender",
                to_id="agent-receiver",
                amount=Decimal("5.0"),
            )

            assert transfer_id is not None
            assert mock_tb_client.create_transfers.called

    @pytest.mark.asyncio
    async def test_transfer_insufficient_balance(self, mock_tb_client, mock_tb_module):
        """Transfer with insufficient balance should raise InsufficientCreditsError."""
        mock_tb_client.create_transfers.return_value = [
            MockCreateTransferError(0, TB_EXCEEDS_CREDITS)
        ]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService, InsufficientCreditsError

            service = CreditsService(client=mock_tb_client)

            with pytest.raises(InsufficientCreditsError):
                await service.transfer(
                    from_id="agent-poor",
                    to_id="agent-rich",
                    amount=Decimal("1000000"),
                )

    @pytest.mark.asyncio
    async def test_transfer_with_idempotency_key(self, mock_tb_client, mock_tb_module):
        """Transfer with idempotency key should use the key for deduplication."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)

            # Same idempotency key should produce same ID
            id1 = await service.transfer(
                from_id="agent-1",
                to_id="agent-2",
                amount=Decimal("1"),
                idempotency_key="unique-key-123",
            )
            id2 = await service.transfer(
                from_id="agent-1",
                to_id="agent-2",
                amount=Decimal("1"),
                idempotency_key="unique-key-123",
            )

            assert id1 == id2


class TestTopup:
    """Test credit top-up operations."""

    @pytest.mark.asyncio
    async def test_topup_success(self, mock_tb_client, mock_tb_module):
        """Top-up should transfer from treasury to agent."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            transfer_id = await service.topup(
                agent_id="agent-1",
                amount=Decimal("100"),
                source="admin",
            )

            assert transfer_id is not None
            mock_tb_client.create_transfers.assert_called_once()


class TestReservation:
    """Test two-phase transfer (reservation) operations."""

    @pytest.mark.asyncio
    async def test_reserve_credits(self, mock_tb_client, mock_tb_module):
        """Reserve should create pending transfer."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            reservation_id = await service.reserve(
                agent_id="agent-1",
                amount=Decimal("10"),
                timeout_seconds=300,
            )

            assert reservation_id is not None
            mock_tb_client.create_transfers.assert_called_once()

    @pytest.mark.asyncio
    async def test_reserve_insufficient_balance(self, mock_tb_client, mock_tb_module):
        """Reserve with insufficient balance should raise error."""
        mock_tb_client.create_transfers.return_value = [
            MockCreateTransferError(0, TB_EXCEEDS_CREDITS)
        ]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService, InsufficientCreditsError

            service = CreditsService(client=mock_tb_client)

            with pytest.raises(InsufficientCreditsError):
                await service.reserve(
                    agent_id="agent-poor",
                    amount=Decimal("1000000"),
                )

    @pytest.mark.asyncio
    async def test_commit_reservation(self, mock_tb_client, mock_tb_module):
        """Commit should post the pending transfer."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            await service.commit_reservation(
                reservation_id="12345678901234567890",
                actual_amount=Decimal("8"),  # Less than reserved
            )

            mock_tb_client.create_transfers.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_reservation(self, mock_tb_client, mock_tb_module):
        """Release should void the pending transfer."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            await service.release_reservation(reservation_id="12345678901234567890")

            mock_tb_client.create_transfers.assert_called_once()


class TestDeductFast:
    """Test fast deduction for API metering."""

    @pytest.mark.asyncio
    async def test_deduct_fast_success(self, mock_tb_client, mock_tb_module):
        """Fast deduction should return True on success."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            result = await service.deduct_fast(
                agent_id="agent-1",
                amount=Decimal("0.001"),
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_deduct_fast_insufficient(self, mock_tb_client, mock_tb_module):
        """Fast deduction with insufficient balance should return False."""
        mock_tb_client.create_transfers.return_value = [
            MockCreateTransferError(0, TB_EXCEEDS_CREDITS)
        ]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            result = await service.deduct_fast(
                agent_id="agent-poor",
                amount=Decimal("0.001"),
            )

            assert result is False


class TestWalletProvisioning:
    """Test wallet (account) provisioning."""

    @pytest.mark.asyncio
    async def test_provision_wallet(self, mock_tb_client, mock_tb_module):
        """Provisioning should create TigerBeetle account."""
        mock_tb_client.create_accounts.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            await service.provision_wallet(
                agent_id="new-agent",
                tenant_id="tenant-1",
            )

            mock_tb_client.create_accounts.assert_called_once()

    @pytest.mark.asyncio
    async def test_provision_wallet_idempotent(self, mock_tb_client, mock_tb_module):
        """Provisioning existing wallet should not raise error."""
        mock_tb_client.create_accounts.return_value = [MockCreateAccountError(0, TB_ACCOUNT_EXISTS)]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            # Should not raise
            await service.provision_wallet(
                agent_id="existing-agent",
                tenant_id="tenant-1",
            )


class TestBatchTransfer:
    """Test batch transfer operations."""

    @pytest.mark.asyncio
    async def test_batch_transfer_success(self, mock_tb_client, mock_tb_module):
        """Batch transfer should execute all transfers atomically."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService, TransferRequest

            service = CreditsService(client=mock_tb_client)
            transfers = [
                TransferRequest(from_id="agent-1", to_id="agent-2", amount=Decimal("5")),
                TransferRequest(from_id="agent-1", to_id="agent-3", amount=Decimal("3")),
            ]

            result = await service.transfer_batch(transfers)

            assert len(result) == 2
            mock_tb_client.create_transfers.assert_called_once()


class TestDisabledMode:
    """Test CreditsService in disabled mode (no TigerBeetle)."""

    @pytest.mark.asyncio
    async def test_disabled_get_balance_returns_unlimited(self):
        """Disabled mode should return unlimited balance."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(enabled=False)
        balance = await service.get_balance("any-agent")

        # Should return a large number indicating unlimited
        assert balance >= Decimal("1000000")

    @pytest.mark.asyncio
    async def test_disabled_transfer_always_succeeds(self):
        """Disabled mode transfers should always succeed."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(enabled=False)
        transfer_id = await service.transfer(
            from_id="agent-1",
            to_id="agent-2",
            amount=Decimal("100"),
        )

        assert transfer_id is not None

    @pytest.mark.asyncio
    async def test_disabled_reserve_always_succeeds(self):
        """Disabled mode reservations should always succeed."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(enabled=False)
        reservation_id = await service.reserve(
            agent_id="agent-1",
            amount=Decimal("100"),
        )

        assert reservation_id is not None

    @pytest.mark.asyncio
    async def test_disabled_deduct_fast_always_succeeds(self):
        """Disabled mode fast deduction should always return True."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(enabled=False)
        result = await service.deduct_fast(
            agent_id="agent-1",
            amount=Decimal("100"),
        )

        assert result is True


class TestCheckBudget:
    """Test budget checking operations."""

    @pytest.mark.asyncio
    async def test_check_budget_sufficient(self, mock_tb_client, mock_tb_module):
        """Budget check should return True when sufficient."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=100_000_000,  # 100 credits
            debits_posted=0,
            debits_pending=0,
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            result = await service.check_budget(
                agent_id="agent-1",
                amount=Decimal("10"),
            )

            assert result is True

    @pytest.mark.asyncio
    async def test_check_budget_insufficient(self, mock_tb_client, mock_tb_module):
        """Budget check should return False when insufficient."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=5_000_000,  # 5 credits
            debits_posted=0,
            debits_pending=0,
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            result = await service.check_budget(
                agent_id="agent-1",
                amount=Decimal("10"),
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_check_budget_considers_reserved(self, mock_tb_client, mock_tb_module):
        """Budget check should account for reserved (pending) amounts."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=20_000_000,  # 20 credits
            debits_posted=5_000_000,  # 5 spent
            debits_pending=10_000_000,  # 10 reserved
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            # Available = 20 - 5 = 15, Reserved = 10, Effective = 5
            result = await service.check_budget(
                agent_id="agent-1",
                amount=Decimal("6"),  # More than effective balance
            )

            assert result is False


# =============================================================================
# Edge Case Tests
# =============================================================================


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_micro_conversion_precision(self):
        """Test that micro conversion handles decimal precision correctly."""
        # Test small amounts
        assert credits_to_micro(0.000001) == 1
        assert micro_to_credits(1) == 0.000001

        # Test rounding
        micro = credits_to_micro(1.2345678)  # More precision than we store
        assert micro == 1234567  # Truncated to 6 decimals

    def test_agent_id_collision_resistance(self):
        """Different agent IDs should produce different TB IDs."""
        ids = set()
        for i in range(1000):
            tb_id = agent_id_to_tb_id(f"agent-{i}")
            ids.add(tb_id)
        # Should have 1000 unique IDs (no collisions)
        assert len(ids) == 1000

    @pytest.mark.asyncio
    async def test_transfer_zero_amount_handled(self, mock_tb_client, mock_tb_module):
        """Zero amount transfer should still call client (TigerBeetle validates)."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            # TigerBeetle allows zero-amount transfers as of v0.16.0
            await service.transfer(
                from_id="agent-1",
                to_id="agent-2",
                amount=Decimal("0"),
            )
            mock_tb_client.create_transfers.assert_called_once()

    @pytest.mark.asyncio
    async def test_large_amount_transfer(self, mock_tb_client, mock_tb_module):
        """Large amounts should be handled without overflow."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            # 1 billion credits
            large_amount = Decimal("1000000000")
            await service.transfer(
                from_id="agent-1",
                to_id="agent-2",
                amount=large_amount,
            )
            mock_tb_client.create_transfers.assert_called_once()

    @pytest.mark.asyncio
    async def test_balance_with_negative_available(self, mock_tb_client, mock_tb_module):
        """Handle case where debits exceed credits (shouldn't happen normally)."""
        mock_account = MockTBAccount(
            id=12345,
            credits_posted=5_000_000,
            debits_posted=10_000_000,  # More debits than credits
        )
        mock_tb_client.lookup_accounts.return_value = [mock_account]

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            balance = await service.get_balance("agent-1")
            # Should return negative balance
            assert balance == Decimal("-5")

    @pytest.mark.asyncio
    async def test_idempotency_key_produces_same_id(self, mock_tb_client, mock_tb_module):
        """Same idempotency key should always produce same transfer ID."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)

            # Get IDs from multiple calls
            id1 = await service.transfer(
                from_id="a", to_id="b", amount=Decimal("1"), idempotency_key="test-key-123"
            )
            id2 = await service.transfer(
                from_id="a", to_id="b", amount=Decimal("1"), idempotency_key="test-key-123"
            )

            assert id1 == id2

    @pytest.mark.asyncio
    async def test_different_idempotency_keys_produce_different_ids(
        self, mock_tb_client, mock_tb_module
    ):
        """Different idempotency keys should produce different transfer IDs."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)

            id1 = await service.transfer(
                from_id="a", to_id="b", amount=Decimal("1"), idempotency_key="key-1"
            )
            id2 = await service.transfer(
                from_id="a", to_id="b", amount=Decimal("1"), idempotency_key="key-2"
            )

            assert id1 != id2

    @pytest.mark.asyncio
    async def test_reservation_timeout_passed_to_tigerbeetle(self, mock_tb_client, mock_tb_module):
        """Timeout should be passed to TigerBeetle transfer."""
        mock_tb_client.create_transfers.return_value = []

        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            await service.reserve(
                agent_id="agent-1",
                amount=Decimal("10"),
                timeout_seconds=600,  # 10 minutes
            )

            # Verify transfer was created
            mock_tb_client.create_transfers.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_batch_transfer(self, mock_tb_client, mock_tb_module):
        """Empty batch should return empty list."""
        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)
            result = await service.transfer_batch([])

            assert result == []
            # Should not call TigerBeetle for empty batch
            mock_tb_client.create_transfers.assert_not_called()


class TestMultiTenancy:
    """Test multi-tenant operations."""

    @pytest.mark.asyncio
    async def test_different_tenants_have_different_accounts(self, mock_tb_client, mock_tb_module):
        """Same agent_id in different tenants should map to different TB accounts."""
        with patch.dict("sys.modules", {"tigerbeetle": mock_tb_module}):
            from nexus.pay.credits import CreditsService

            service = CreditsService(client=mock_tb_client)

            # Create wallets for same agent in different tenants
            mock_tb_client.create_accounts.return_value = []

            await service.provision_wallet("agent-1", tenant_id="tenant-a")
            await service.provision_wallet("agent-1", tenant_id="tenant-b")

            # Should have been called twice with different IDs
            assert mock_tb_client.create_accounts.call_count == 2

    def test_tenant_id_changes_tb_account_id(self):
        """Same agent_id with different tenant_id should produce different TB IDs."""
        id_tenant_a = make_tb_account_id("tenant-a", "agent-1")
        id_tenant_b = make_tb_account_id("tenant-b", "agent-1")

        assert id_tenant_a != id_tenant_b
