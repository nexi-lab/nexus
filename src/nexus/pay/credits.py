"""Credits service for agent-to-agent payments using TigerBeetle.

This module provides the core credits management functionality:
- Balance queries (get_balance, get_balance_with_reserved)
- Transfers (transfer, topup, transfer_batch)
- Two-phase transfers (reserve, commit_reservation, release_reservation)
- Fast metering (deduct_fast)
- Wallet provisioning (provision_wallet)

TigerBeetle Best Practices Applied:
- Single shared client for automatic request batching (thread-safe)
- Two-phase transfers with auto-timeout for reservations
- DEBITS_MUST_NOT_EXCEED_CREDITS flag prevents overdraft
- Idempotency keys for retry-safe transfers
- Linked transfers for atomic batch operations

References:
- https://docs.tigerbeetle.com/coding/clients/python/
- https://docs.tigerbeetle.com/coding/two-phase-transfers/
- https://docs.tigerbeetle.com/coding/recipes/rate-limiting/
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from nexus.pay.constants import (
    ACCOUNT_CODE_ESCROW,
    ACCOUNT_CODE_TREASURY,
    ACCOUNT_CODE_WALLET,
    ESCROW_ACCOUNT_TB_ID,
    LEDGER_CREDITS,
    SYSTEM_TREASURY_TB_ID,
    TRANSFER_CODE_API_USAGE,
    TRANSFER_CODE_PAYMENT,
    TRANSFER_CODE_RESERVATION,
    TRANSFER_CODE_TOPUP,
    credits_to_micro,
    make_tb_account_id,
    micro_to_credits,
)

if TYPE_CHECKING:
    pass


# =============================================================================
# Exceptions
# =============================================================================


class CreditsError(Exception):
    """Base exception for credits operations."""

    pass


class InsufficientCreditsError(CreditsError):
    """Raised when account has insufficient credits for operation."""

    pass


class WalletNotFoundError(CreditsError):
    """Raised when wallet/account does not exist."""

    pass


class ReservationError(CreditsError):
    """Raised when reservation operation fails."""

    pass


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class TransferRequest:
    """Request for a single transfer in a batch."""

    from_id: str
    to_id: str
    amount: Decimal
    memo: str = ""


# =============================================================================
# CreditsService
# =============================================================================


class CreditsService:
    """High-performance credits service using TigerBeetle.

    Supports both financial transfers and infrastructure token use cases:
    - Agent-to-agent payments
    - Queue priority bidding
    - API metering / rate limiting
    - Task scheduling with reservations

    Best Practices:
    - Use a single CreditsService instance shared across tasks (thread-safe)
    - Use idempotency_key for retry-safe transfers
    - Use reserve/commit for two-phase operations
    - Use deduct_fast for high-throughput metering

    Example:
        >>> service = CreditsService()  # Uses env vars for config
        >>> balance = await service.get_balance("agent-123")
        >>> await service.transfer("agent-a", "agent-b", Decimal("10"))
    """

    # Unlimited balance for disabled mode
    DISABLED_UNLIMITED_BALANCE = Decimal("999999999")

    def __init__(
        self,
        *,
        client: Any = None,
        tigerbeetle_address: str = "127.0.0.1:3000",
        cluster_id: int = 0,
        enabled: bool = True,
    ):
        """Initialize CreditsService.

        Args:
            client: Pre-configured TigerBeetle client (for testing).
            tigerbeetle_address: TigerBeetle server address.
            cluster_id: TigerBeetle cluster ID.
            enabled: If False, operates in pass-through mode (no real ledger).
        """
        self._enabled = enabled
        self._client = client
        self._address = tigerbeetle_address
        self._cluster_id = cluster_id
        self._tb = None  # Lazy import TigerBeetle module

        if enabled and client is None:
            # Will lazy-load client on first use
            pass

    def _get_tb_module(self) -> Any:
        """Lazy import TigerBeetle module."""
        if self._tb is None:
            import tigerbeetle as tb

            self._tb = tb
        return self._tb

    async def _get_client(self) -> Any:
        """Get or create TigerBeetle client.

        Best Practice: Single client shared for automatic batching.
        """
        if self._client is not None:
            return self._client

        tb = self._get_tb_module()
        # Note: In production, use async context manager
        # This is simplified for the initial implementation
        self._client = tb.ClientAsync(
            cluster_id=self._cluster_id,
            replica_addresses=self._address,
        )

        # Ensure system accounts exist on first connection
        await self._ensure_system_accounts()

        return self._client

    async def _ensure_system_accounts(self) -> None:
        """Create system accounts (treasury, escrow) if they don't exist.

        These accounts are required for topup and reservation operations.
        Idempotent - safe to call multiple times.
        """
        tb = self._get_tb_module()

        # Treasury account - source for topups, sink for usage charges
        # Uses CREDITS_MUST_NOT_EXCEED_DEBITS so it can have "negative" balance
        # (i.e., it can issue more credits than it receives)
        treasury = tb.Account(
            id=SYSTEM_TREASURY_TB_ID,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_TREASURY,
            flags=tb.AccountFlags.CREDITS_MUST_NOT_EXCEED_DEBITS,
        )

        # Escrow account - holds reserved credits during two-phase transfers
        escrow = tb.Account(
            id=ESCROW_ACCOUNT_TB_ID,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_ESCROW,
            flags=0,  # No balance constraints - just holds funds temporarily
        )

        errors = await self._client.create_accounts([treasury, escrow])
        # Ignore "exists" errors - idempotent operation
        # TigerBeetle CreateAccountResult.EXISTS = 21
        for error in errors:
            if error.result not in (0, 21):  # OK or EXISTS
                # Log but don't fail - system might still work
                pass

    def _to_tb_id(self, agent_id: str, zone_id: str = "default") -> int:
        """Convert agent_id to TigerBeetle account ID."""
        return make_tb_account_id(zone_id, agent_id)

    def _generate_transfer_id(self, idempotency_key: str | None = None) -> int:
        """Generate transfer ID, using idempotency key if provided.

        Best Practice: Use deterministic IDs from idempotency keys for retry safety.
        """
        if idempotency_key:
            # Hash the key to get deterministic ID
            hash_bytes = hashlib.sha256(idempotency_key.encode()).digest()
            return int.from_bytes(hash_bytes[:16], byteorder="big") % (2**127)
        else:
            # Generate random ID using tb.id() style
            return int(uuid.uuid4().int % (2**127))

    # =========================================================================
    # Balance Operations
    # =========================================================================

    async def get_balance(self, agent_id: str, zone_id: str = "default") -> Decimal:
        """Get available balance (credits_posted - debits_posted).

        Args:
            agent_id: Agent identifier.
            zone_id: Zone identifier for multi-tenancy.

        Returns:
            Available balance in credits.
        """
        if not self._enabled:
            return self.DISABLED_UNLIMITED_BALANCE

        client = await self._get_client()
        tb_id = self._to_tb_id(agent_id, zone_id)

        accounts = await client.lookup_accounts([tb_id])
        if not accounts:
            return Decimal("0")

        account = accounts[0]
        micro_balance = account.credits_posted - account.debits_posted
        return Decimal(str(micro_to_credits(micro_balance)))

    async def get_balance_with_reserved(
        self, agent_id: str, zone_id: str = "default"
    ) -> tuple[Decimal, Decimal]:
        """Get available balance and reserved (pending) amount.

        Args:
            agent_id: Agent identifier.
            zone_id: Zone identifier.

        Returns:
            Tuple of (available_balance, reserved_amount).
        """
        if not self._enabled:
            return self.DISABLED_UNLIMITED_BALANCE, Decimal("0")

        client = await self._get_client()
        tb_id = self._to_tb_id(agent_id, zone_id)

        accounts = await client.lookup_accounts([tb_id])
        if not accounts:
            return Decimal("0"), Decimal("0")

        account = accounts[0]
        available = Decimal(str(micro_to_credits(account.credits_posted - account.debits_posted)))
        reserved = Decimal(str(micro_to_credits(account.debits_pending)))
        return available, reserved

    # =========================================================================
    # Transfer Operations
    # =========================================================================

    async def transfer(
        self,
        from_id: str,
        to_id: str,
        amount: Decimal,
        *,
        memo: str = "",  # noqa: ARG002 - stored in PostgreSQL, not TigerBeetle
        idempotency_key: str | None = None,
        zone_id: str = "default",
    ) -> str:
        """Execute atomic credit transfer between agents.

        Best Practice: Use idempotency_key for retry-safe transfers.

        Args:
            from_id: Source agent ID.
            to_id: Destination agent ID.
            amount: Amount in credits.
            memo: Optional description (stored in PostgreSQL, not TigerBeetle).
            idempotency_key: Optional key for deduplication.
            zone_id: Zone identifier.

        Returns:
            Transfer ID as string.

        Raises:
            InsufficientCreditsError: If sender has insufficient balance.
        """
        if not self._enabled:
            return str(self._generate_transfer_id(idempotency_key))

        tb = self._get_tb_module()
        client = await self._get_client()

        transfer_id = self._generate_transfer_id(idempotency_key)
        micro_amount = credits_to_micro(amount)

        transfer = tb.Transfer(
            id=transfer_id,
            debit_account_id=self._to_tb_id(from_id, zone_id),
            credit_account_id=self._to_tb_id(to_id, zone_id),
            amount=micro_amount,
            ledger=LEDGER_CREDITS,
            code=TRANSFER_CODE_PAYMENT,
        )

        errors = await client.create_transfers([transfer])
        if errors:
            error = errors[0]
            # TigerBeetle CreateTransferResult error codes:
            # EXISTS = 46 (idempotent success)
            # EXCEEDS_CREDITS = 54 (insufficient balance)
            if error.result == 46:  # EXISTS - idempotent, transfer already done
                return str(transfer_id)
            if error.result == 54:  # EXCEEDS_CREDITS
                raise InsufficientCreditsError(
                    f"Insufficient balance for transfer of {amount} credits"
                )
            raise CreditsError(f"Transfer failed: {error.result}")

        return str(transfer_id)

    async def topup(
        self,
        agent_id: str,
        amount: Decimal,
        source: str,  # noqa: ARG002 - stored in PostgreSQL metadata
        *,
        external_tx_id: str | None = None,
        zone_id: str = "default",
    ) -> str:
        """Add credits from external source (treasury -> agent).

        Args:
            agent_id: Agent to receive credits.
            amount: Amount in credits.
            source: Source identifier ("admin", "stripe", "x402").
            external_tx_id: External transaction reference.
            zone_id: Zone identifier.

        Returns:
            Transfer ID as string.
        """
        if not self._enabled:
            return str(self._generate_transfer_id(external_tx_id))

        tb = self._get_tb_module()
        client = await self._get_client()

        transfer_id = self._generate_transfer_id(external_tx_id)
        micro_amount = credits_to_micro(amount)

        transfer = tb.Transfer(
            id=transfer_id,
            debit_account_id=SYSTEM_TREASURY_TB_ID,
            credit_account_id=self._to_tb_id(agent_id, zone_id),
            amount=micro_amount,
            ledger=LEDGER_CREDITS,
            code=TRANSFER_CODE_TOPUP,
        )

        errors = await client.create_transfers([transfer])
        if errors:
            raise CreditsError(f"Topup failed: {errors[0].result}")

        return str(transfer_id)

    # =========================================================================
    # Reservation (Two-Phase) Operations
    # =========================================================================

    async def reserve(
        self,
        agent_id: str,
        amount: Decimal,
        timeout_seconds: int = 300,
        *,
        zone_id: str = "default",
    ) -> str:
        """Reserve credits for a pending operation.

        Uses TigerBeetle's native two-phase transfers with auto-timeout.
        Reserved credits are held in debits_pending until committed or released.

        Best Practice: Auto-releases if not committed within timeout.

        Args:
            agent_id: Agent whose credits to reserve.
            amount: Amount in credits to reserve.
            timeout_seconds: Auto-release timeout (default 5 minutes).
            zone_id: Zone identifier.

        Returns:
            Reservation ID as string.

        Raises:
            InsufficientCreditsError: If insufficient balance.
        """
        if not self._enabled:
            return str(self._generate_transfer_id())

        tb = self._get_tb_module()
        client = await self._get_client()

        reservation_id = self._generate_transfer_id()
        micro_amount = credits_to_micro(amount)

        transfer = tb.Transfer(
            id=reservation_id,
            debit_account_id=self._to_tb_id(agent_id, zone_id),
            credit_account_id=ESCROW_ACCOUNT_TB_ID,
            amount=micro_amount,
            ledger=LEDGER_CREDITS,
            code=TRANSFER_CODE_RESERVATION,
            flags=tb.TransferFlags.PENDING,
            timeout=timeout_seconds,
        )

        errors = await client.create_transfers([transfer])
        if errors:
            error = errors[0]
            # TigerBeetle CreateTransferResult.EXCEEDS_CREDITS = 54
            if error.result == 54:
                raise InsufficientCreditsError(f"Insufficient balance to reserve {amount} credits")
            raise ReservationError(f"Reservation failed: {error.result}")

        return str(reservation_id)

    async def commit_reservation(
        self,
        reservation_id: str,
        actual_amount: Decimal | None = None,
    ) -> None:
        """Commit a pending reservation (post the transfer).

        If actual_amount < reserved, the difference is auto-refunded.
        Use amount_max to commit the full reserved amount.

        Args:
            reservation_id: ID from reserve() call.
            actual_amount: Actual amount to transfer (None = full amount).

        Raises:
            CreditsError: If commit fails.
        """
        if not self._enabled:
            return

        tb = self._get_tb_module()
        client = await self._get_client()

        post_id = self._generate_transfer_id()
        pending_id = int(reservation_id)

        # If actual_amount specified, use it; otherwise use amount_max for full
        amount = credits_to_micro(actual_amount) if actual_amount else tb.amount_max

        post_transfer = tb.Transfer(
            id=post_id,
            pending_id=pending_id,
            amount=amount,
            flags=tb.TransferFlags.POST_PENDING_TRANSFER,
        )

        errors = await client.create_transfers([post_transfer])
        if errors:
            raise CreditsError(f"Commit failed: {errors[0].result}")

    async def release_reservation(self, reservation_id: str) -> None:
        """Void a pending reservation (full refund).

        Args:
            reservation_id: ID from reserve() call.

        Raises:
            CreditsError: If release fails.
        """
        if not self._enabled:
            return

        tb = self._get_tb_module()
        client = await self._get_client()

        void_id = self._generate_transfer_id()
        pending_id = int(reservation_id)

        void_transfer = tb.Transfer(
            id=void_id,
            pending_id=pending_id,
            amount=0,  # TigerBeetle auto-fills from pending
            flags=tb.TransferFlags.VOID_PENDING_TRANSFER,
        )

        errors = await client.create_transfers([void_transfer])
        if errors:
            raise CreditsError(f"Release failed: {errors[0].result}")

    # =========================================================================
    # Fast Metering Operations
    # =========================================================================

    async def deduct_fast(
        self,
        agent_id: str,
        amount: Decimal,
        *,
        code: int = TRANSFER_CODE_API_USAGE,
        zone_id: str = "default",
    ) -> bool:
        """Fast credit deduction for API metering / rate limiting.

        Best Practice: Use for high-throughput operations where failure
        should be silent (returns False instead of raising).

        Args:
            agent_id: Agent to deduct from.
            amount: Amount in credits.
            code: Transfer code for categorization.
            zone_id: Zone identifier.

        Returns:
            True if successful, False if insufficient balance.
        """
        if not self._enabled:
            return True

        tb = self._get_tb_module()
        client = await self._get_client()

        transfer_id = self._generate_transfer_id()
        micro_amount = credits_to_micro(amount)

        transfer = tb.Transfer(
            id=transfer_id,
            debit_account_id=self._to_tb_id(agent_id, zone_id),
            credit_account_id=SYSTEM_TREASURY_TB_ID,
            amount=micro_amount,
            ledger=LEDGER_CREDITS,
            code=code,
        )

        errors = await client.create_transfers([transfer])
        return len(errors) == 0

    # =========================================================================
    # Batch Operations
    # =========================================================================

    async def transfer_batch(
        self,
        transfers: list[TransferRequest],
        *,
        zone_id: str = "default",
    ) -> list[str]:
        """Execute atomic batch transfer - all succeed or all fail.

        Best Practice: Uses linked transfers for atomicity.
        TigerBeetle supports up to 8,189 transfers per batch.

        Args:
            transfers: List of transfer requests.
            zone_id: Zone identifier.

        Returns:
            List of transfer IDs.

        Raises:
            CreditsError: If any transfer fails (entire batch rolls back).
        """
        if not transfers:
            return []

        if not self._enabled:
            return [str(self._generate_transfer_id()) for _ in transfers]

        tb = self._get_tb_module()
        client = await self._get_client()

        tb_transfers = []
        for i, t in enumerate(transfers):
            is_last = i == len(transfers) - 1
            flags = 0 if is_last else tb.TransferFlags.LINKED

            tb_transfers.append(
                tb.Transfer(
                    id=self._generate_transfer_id(),
                    debit_account_id=self._to_tb_id(t.from_id, zone_id),
                    credit_account_id=self._to_tb_id(t.to_id, zone_id),
                    amount=credits_to_micro(t.amount),
                    ledger=LEDGER_CREDITS,
                    code=TRANSFER_CODE_PAYMENT,
                    flags=flags,
                )
            )

        errors = await client.create_transfers(tb_transfers)
        if errors:
            raise CreditsError(f"Batch transfer failed: {errors}")

        return [str(t.id) for t in tb_transfers]

    # =========================================================================
    # Wallet Provisioning
    # =========================================================================

    async def provision_wallet(
        self,
        agent_id: str,
        zone_id: str = "default",
    ) -> None:
        """Create TigerBeetle account for a new agent.

        Best Practice: Idempotent - safe to call multiple times.

        Args:
            agent_id: Agent identifier.
            zone_id: Zone identifier.
        """
        if not self._enabled:
            return

        tb = self._get_tb_module()
        client = await self._get_client()

        tb_id = self._to_tb_id(agent_id, zone_id)

        account = tb.Account(
            id=tb_id,
            ledger=LEDGER_CREDITS,
            code=ACCOUNT_CODE_WALLET,
            flags=tb.AccountFlags.DEBITS_MUST_NOT_EXCEED_CREDITS,
        )

        errors = await client.create_accounts([account])
        # Ignore "exists" error - idempotent operation
        # TigerBeetle CreateAccountResult.EXISTS = 21
        if errors and errors[0].result not in (0, 21):  # OK or EXISTS
            raise CreditsError(f"Failed to create wallet: {errors[0].result}")

    # =========================================================================
    # Budget Operations
    # =========================================================================

    async def check_budget(
        self,
        agent_id: str,
        amount: Decimal,
        *,
        zone_id: str = "default",
    ) -> bool:
        """Check if agent has sufficient balance for an amount.

        Note: This is a point-in-time check. For guaranteed atomicity,
        use reserve() instead.

        Args:
            agent_id: Agent identifier.
            amount: Amount to check against.
            zone_id: Zone identifier.

        Returns:
            True if sufficient balance, False otherwise.
        """
        if not self._enabled:
            return True

        available, reserved = await self.get_balance_with_reserved(agent_id, zone_id)
        effective_balance = available - reserved
        return effective_balance >= amount


# =============================================================================
# Module-level convenience
# =============================================================================

__all__ = [
    "CreditsService",
    "CreditsError",
    "InsufficientCreditsError",
    "WalletNotFoundError",
    "ReservationError",
    "TransferRequest",
]
