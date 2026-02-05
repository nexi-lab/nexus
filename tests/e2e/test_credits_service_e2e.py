"""End-to-end tests for CreditsService.

These tests verify the CreditsService works correctly in disabled mode
(no TigerBeetle server required). For full integration tests with TigerBeetle,
see tests/integration/pay/ (requires TigerBeetle container).

Tests verify:
- Service instantiation and configuration
- All operations work in disabled mode
- Module exports are correct
- Concurrent operation safety
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest


class TestCreditsServiceE2E:
    """End-to-end tests for CreditsService (disabled mode)."""

    @pytest.mark.asyncio
    async def test_service_initialization(self):
        """CreditsService should initialize without TigerBeetle in disabled mode."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)
        assert service._enabled is False

    @pytest.mark.asyncio
    async def test_full_transfer_workflow(self):
        """Complete transfer workflow should work in disabled mode."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        # 1. Check balance
        balance = await service.get_balance("agent-sender")
        assert balance >= Decimal("1000000")  # Unlimited in disabled mode

        # 2. Execute transfer
        transfer_id = await service.transfer(
            from_id="agent-sender",
            to_id="agent-receiver",
            amount=Decimal("100"),
            memo="Payment for services",
        )
        assert transfer_id is not None
        assert len(transfer_id) > 0

        # 3. Verify balance still unlimited
        balance_after = await service.get_balance("agent-sender")
        assert balance_after >= Decimal("1000000")

    @pytest.mark.asyncio
    async def test_reservation_workflow(self):
        """Two-phase transfer workflow should work in disabled mode."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        # 1. Reserve credits
        reservation_id = await service.reserve(
            agent_id="agent-1",
            amount=Decimal("50"),
            timeout_seconds=300,
        )
        assert reservation_id is not None

        # 2. Check balance with reserved
        available, reserved = await service.get_balance_with_reserved("agent-1")
        assert available >= Decimal("1000000")
        assert reserved == Decimal("0")  # Not tracked in disabled mode

        # 3. Commit reservation
        await service.commit_reservation(
            reservation_id=reservation_id,
            actual_amount=Decimal("45"),  # Less than reserved
        )

        # 4. Or release (test separately)
        reservation_id_2 = await service.reserve("agent-1", Decimal("25"))
        await service.release_reservation(reservation_id_2)

    @pytest.mark.asyncio
    async def test_fast_metering(self):
        """Fast deduction for API metering should work."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        # Simulate API metering
        for _ in range(100):
            result = await service.deduct_fast(
                agent_id="agent-api-user",
                amount=Decimal("0.001"),
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_batch_transfer(self):
        """Batch transfers should work atomically."""
        from nexus.pay import CreditsService, TransferRequest

        service = CreditsService(enabled=False)

        transfers = [
            TransferRequest(from_id="agent-1", to_id="agent-2", amount=Decimal("10")),
            TransferRequest(from_id="agent-1", to_id="agent-3", amount=Decimal("20")),
            TransferRequest(from_id="agent-1", to_id="agent-4", amount=Decimal("30")),
        ]

        result = await service.transfer_batch(transfers)
        assert len(result) == 3
        assert all(id is not None for id in result)

    @pytest.mark.asyncio
    async def test_wallet_provisioning(self):
        """Wallet provisioning should be idempotent."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        # Should not raise on first call
        await service.provision_wallet("new-agent", "tenant-1")

        # Should not raise on second call (idempotent)
        await service.provision_wallet("new-agent", "tenant-1")

    @pytest.mark.asyncio
    async def test_budget_check(self):
        """Budget check should always pass in disabled mode."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        result = await service.check_budget("agent-1", Decimal("999999999"))
        assert result is True

    @pytest.mark.asyncio
    async def test_concurrent_operations(self):
        """Concurrent operations should not interfere with each other."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        async def do_transfer(n: int) -> str:
            return await service.transfer(
                from_id=f"sender-{n}",
                to_id=f"receiver-{n}",
                amount=Decimal(n),
                idempotency_key=f"concurrent-test-{n}",
            )

        # Run 50 concurrent transfers
        tasks = [do_transfer(i) for i in range(50)]
        results = await asyncio.gather(*tasks)

        # All should succeed with unique IDs
        assert len(results) == 50
        assert len(set(results)) == 50  # All unique

    @pytest.mark.asyncio
    async def test_idempotency_across_calls(self):
        """Same idempotency key should produce same transfer ID."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        id1 = await service.transfer(
            from_id="a",
            to_id="b",
            amount=Decimal("10"),
            idempotency_key="stable-key-xyz",
        )
        id2 = await service.transfer(
            from_id="a",
            to_id="b",
            amount=Decimal("10"),
            idempotency_key="stable-key-xyz",
        )

        assert id1 == id2

    @pytest.mark.asyncio
    async def test_topup_from_treasury(self):
        """Top-up should work (treasury to agent)."""
        from nexus.pay import CreditsService

        service = CreditsService(enabled=False)

        transfer_id = await service.topup(
            agent_id="new-user",
            amount=Decimal("1000"),
            source="admin",
            external_tx_id="promo-bonus-123",
        )

        assert transfer_id is not None


class TestModuleExports:
    """Test that all expected exports are available."""

    def test_service_exports(self):
        """CreditsService and related classes should be importable."""
        from nexus.pay import (
            CreditsError,
            CreditsService,
            InsufficientCreditsError,
            ReservationError,
            TransferRequest,
            WalletNotFoundError,
        )

        assert CreditsService is not None
        assert TransferRequest is not None
        assert CreditsError is not None
        assert InsufficientCreditsError is not None
        assert WalletNotFoundError is not None
        assert ReservationError is not None

    def test_constant_exports(self):
        """All constants should be importable."""
        from nexus.pay import (
            ACCOUNT_CODE_WALLET,
            LEDGER_CREDITS,
            MICRO_UNIT_SCALE,
        )

        assert LEDGER_CREDITS == 1
        assert ACCOUNT_CODE_WALLET == 1
        assert MICRO_UNIT_SCALE == 1_000_000

    def test_utility_exports(self):
        """Utility functions should be importable and work."""
        from nexus.pay import (
            agent_id_to_tb_id,
            credits_to_micro,
            make_tb_account_id,
            micro_to_credits,
            tenant_to_tb_prefix,
        )

        # Test conversions
        assert credits_to_micro(1.0) == 1_000_000
        assert micro_to_credits(1_000_000) == 1.0

        # Test ID generation
        tb_id = agent_id_to_tb_id("test-agent")
        assert isinstance(tb_id, int)
        assert tb_id > 0

        prefix = tenant_to_tb_prefix("test-tenant")
        assert isinstance(prefix, int)

        full_id = make_tb_account_id("test-tenant", "test-agent")
        assert isinstance(full_id, int)
        assert full_id > 0
