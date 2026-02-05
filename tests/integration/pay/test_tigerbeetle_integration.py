"""Integration tests for CreditsService with real TigerBeetle.

These tests require TigerBeetle to be running:
    docker compose --profile pay up -d tigerbeetle

Tests verify:
- Real TigerBeetle client connection
- Account creation and balance queries
- Transfer operations
- Two-phase transfers (reserve/commit/release)
- Concurrent operations
- Error handling with real errors
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from decimal import Decimal

import pytest


def _is_tigerbeetle_available() -> tuple[bool, str]:
    """Check if TigerBeetle is available (module + server)."""
    # Check module
    try:
        import tigerbeetle  # noqa: F401
    except ImportError:
        return False, "TigerBeetle Python client not installed. Run: pip install tigerbeetle"

    # Check server
    address = os.environ.get("TIGERBEETLE_ADDRESS", "127.0.0.1:3000")
    try:
        host, port = address.split(":")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex((host, int(port)))
        sock.close()
        if result != 0:
            return (
                False,
                f"TigerBeetle server not available at {address}. Run: docker compose --profile pay up -d tigerbeetle",
            )
    except Exception as e:
        return False, f"TigerBeetle connection error: {e}"

    return True, ""


_tb_available, _tb_skip_reason = _is_tigerbeetle_available()

# Skip all tests if TigerBeetle not available
pytestmark = pytest.mark.skipif(not _tb_available, reason=_tb_skip_reason)


def unique_agent_id() -> str:
    """Generate unique agent ID for test isolation."""
    return f"test-agent-{uuid.uuid4().hex[:8]}"


class TestTigerBeetleConnection:
    """Test TigerBeetle client connection."""

    @pytest.mark.asyncio
    async def test_connect_to_tigerbeetle(self, tigerbeetle_address: str):
        """Should connect to TigerBeetle server."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        # Connection is lazy, force it by making a query
        balance = await service.get_balance(unique_agent_id())
        # New accounts should have zero balance
        assert balance == Decimal("0")


class TestWalletProvisioning:
    """Test wallet creation with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_provision_wallet(self, tigerbeetle_address: str):
        """Should create TigerBeetle account."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id, "test-zone")

        # Verify account exists by checking balance
        balance = await service.get_balance(agent_id, "test-zone")
        assert balance == Decimal("0")

    @pytest.mark.asyncio
    async def test_provision_wallet_idempotent(self, tigerbeetle_address: str):
        """Provisioning same wallet twice should not fail."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()

        # First provision
        await service.provision_wallet(agent_id, "test-zone")

        # Second provision should not raise
        await service.provision_wallet(agent_id, "test-zone")


class TestTransferOperations:
    """Test credit transfers with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_topup_and_check_balance(self, tigerbeetle_address: str):
        """Should add credits and reflect in balance."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)

        # Top up 100 credits
        await service.topup(agent_id, Decimal("100"), "test")

        # Check balance
        balance = await service.get_balance(agent_id)
        assert balance == Decimal("100")

    @pytest.mark.asyncio
    async def test_transfer_between_agents(self, tigerbeetle_address: str):
        """Should transfer credits between agents."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        sender_id = unique_agent_id()
        receiver_id = unique_agent_id()

        # Provision both wallets
        await service.provision_wallet(sender_id)
        await service.provision_wallet(receiver_id)

        # Top up sender
        await service.topup(sender_id, Decimal("100"), "test")

        # Transfer 30 credits
        transfer_id = await service.transfer(
            from_id=sender_id,
            to_id=receiver_id,
            amount=Decimal("30"),
        )

        assert transfer_id is not None

        # Verify balances
        sender_balance = await service.get_balance(sender_id)
        receiver_balance = await service.get_balance(receiver_id)

        assert sender_balance == Decimal("70")
        assert receiver_balance == Decimal("30")

    @pytest.mark.asyncio
    async def test_transfer_insufficient_balance(self, tigerbeetle_address: str):
        """Should fail transfer when balance insufficient."""
        from nexus.pay.credits import CreditsService, InsufficientCreditsError

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        sender_id = unique_agent_id()
        receiver_id = unique_agent_id()

        await service.provision_wallet(sender_id)
        await service.provision_wallet(receiver_id)

        # Sender has no credits
        with pytest.raises(InsufficientCreditsError):
            await service.transfer(
                from_id=sender_id,
                to_id=receiver_id,
                amount=Decimal("100"),
            )


class TestTwoPhaseTransfers:
    """Test reservation (two-phase) operations with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_reserve_and_commit(self, tigerbeetle_address: str):
        """Should reserve credits then commit."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)
        await service.topup(agent_id, Decimal("100"), "test")

        # Reserve 50 credits
        reservation_id = await service.reserve(
            agent_id=agent_id,
            amount=Decimal("50"),
            timeout_seconds=300,
        )

        # Check balance with reserved
        available, reserved = await service.get_balance_with_reserved(agent_id)
        assert available == Decimal("100")  # Still shows full balance
        assert reserved == Decimal("50")  # But 50 is reserved

        # Commit reservation (using only 40)
        await service.commit_reservation(reservation_id, Decimal("40"))

        # Final balance should be 60 (100 - 40)
        final_balance = await service.get_balance(agent_id)
        assert final_balance == Decimal("60")

    @pytest.mark.asyncio
    async def test_reserve_and_release(self, tigerbeetle_address: str):
        """Should reserve credits then release (void)."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)
        await service.topup(agent_id, Decimal("100"), "test")

        # Reserve 50 credits
        reservation_id = await service.reserve(
            agent_id=agent_id,
            amount=Decimal("50"),
        )

        # Release reservation
        await service.release_reservation(reservation_id)

        # Balance should be fully restored
        available, reserved = await service.get_balance_with_reserved(agent_id)
        assert available == Decimal("100")
        assert reserved == Decimal("0")

    @pytest.mark.asyncio
    async def test_reserve_insufficient_balance(self, tigerbeetle_address: str):
        """Should fail reservation when balance insufficient."""
        from nexus.pay.credits import CreditsService, InsufficientCreditsError

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)
        # No top-up, balance is 0

        with pytest.raises(InsufficientCreditsError):
            await service.reserve(agent_id=agent_id, amount=Decimal("50"))


class TestFastMetering:
    """Test fast deduction for API metering."""

    @pytest.mark.asyncio
    async def test_deduct_fast_success(self, tigerbeetle_address: str):
        """Should deduct credits quickly."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)
        await service.topup(agent_id, Decimal("10"), "test")

        # Deduct 100 micro-credits (0.0001)
        result = await service.deduct_fast(agent_id, Decimal("0.0001"))
        assert result is True

        # Verify deduction
        balance = await service.get_balance(agent_id)
        assert balance == Decimal("9.9999")

    @pytest.mark.asyncio
    async def test_deduct_fast_insufficient(self, tigerbeetle_address: str):
        """Should return False when insufficient balance."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        agent_id = unique_agent_id()
        await service.provision_wallet(agent_id)
        # No top-up

        result = await service.deduct_fast(agent_id, Decimal("1"))
        assert result is False


class TestBatchOperations:
    """Test batch transfers with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_batch_transfer(self, tigerbeetle_address: str):
        """Should execute batch transfers atomically."""
        from nexus.pay.credits import CreditsService, TransferRequest

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        sender_id = unique_agent_id()
        receiver1_id = unique_agent_id()
        receiver2_id = unique_agent_id()
        receiver3_id = unique_agent_id()

        # Provision all wallets
        for agent_id in [sender_id, receiver1_id, receiver2_id, receiver3_id]:
            await service.provision_wallet(agent_id)

        # Top up sender
        await service.topup(sender_id, Decimal("100"), "test")

        # Batch transfer
        transfers = [
            TransferRequest(from_id=sender_id, to_id=receiver1_id, amount=Decimal("10")),
            TransferRequest(from_id=sender_id, to_id=receiver2_id, amount=Decimal("20")),
            TransferRequest(from_id=sender_id, to_id=receiver3_id, amount=Decimal("30")),
        ]

        result = await service.transfer_batch(transfers)
        assert len(result) == 3

        # Verify final balances
        assert await service.get_balance(sender_id) == Decimal("40")
        assert await service.get_balance(receiver1_id) == Decimal("10")
        assert await service.get_balance(receiver2_id) == Decimal("20")
        assert await service.get_balance(receiver3_id) == Decimal("30")


class TestConcurrentOperations:
    """Test concurrent operations with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_concurrent_transfers(self, tigerbeetle_address: str):
        """Multiple concurrent transfers should not interfere."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        # Create multiple agents
        agents = [unique_agent_id() for _ in range(10)]
        for agent_id in agents:
            await service.provision_wallet(agent_id)
            await service.topup(agent_id, Decimal("100"), "test")

        # Concurrent transfers between random pairs
        async def do_transfer(from_idx: int, to_idx: int) -> str:
            return await service.transfer(
                from_id=agents[from_idx],
                to_id=agents[to_idx],
                amount=Decimal("1"),
            )

        # Execute 50 concurrent transfers
        tasks = [do_transfer(i % 10, (i + 1) % 10) for i in range(50)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 50
        assert all(r is not None for r in results)

        # Total balance should still be 1000 (10 agents * 100 each)
        total = sum([await service.get_balance(a) for a in agents])
        assert total == Decimal("1000")


class TestIdempotency:
    """Test idempotency with real TigerBeetle."""

    @pytest.mark.asyncio
    async def test_idempotent_transfer(self, tigerbeetle_address: str):
        """Same idempotency key should not duplicate transfer."""
        from nexus.pay.credits import CreditsService

        service = CreditsService(
            tigerbeetle_address=tigerbeetle_address,
            enabled=True,
        )

        sender_id = unique_agent_id()
        receiver_id = unique_agent_id()

        await service.provision_wallet(sender_id)
        await service.provision_wallet(receiver_id)
        await service.topup(sender_id, Decimal("100"), "test")

        idempotency_key = f"test-key-{uuid.uuid4().hex}"

        # First transfer
        id1 = await service.transfer(
            from_id=sender_id,
            to_id=receiver_id,
            amount=Decimal("25"),
            idempotency_key=idempotency_key,
        )

        # Second transfer with same key - should return same ID
        id2 = await service.transfer(
            from_id=sender_id,
            to_id=receiver_id,
            amount=Decimal("25"),
            idempotency_key=idempotency_key,
        )

        assert id1 == id2

        # Balance should only reflect ONE transfer
        sender_balance = await service.get_balance(sender_id)
        assert sender_balance == Decimal("75")  # 100 - 25, not 100 - 50
