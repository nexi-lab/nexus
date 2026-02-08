"""NexusPay - Unified payment SDK for agent transactions.

A Stripe-simple SDK that wraps TigerBeetle (internal credits) + x402 (external
blockchain payments), providing a clean Python API for agent payments.

Design Goals:
    1. One line to start: pay = NexusPay("nx_...")
    2. Automatic routing: x402 for external, TigerBeetle for internal
    3. Sensible defaults: Works out of the box
    4. Full control: Override method if needed
    5. Infrastructure tokens: Support queue bidding, API metering

Example:
    >>> from nexus.pay.sdk import NexusPay
    >>> pay = NexusPay("nx_live_myagent", credits_service=service)
    >>> balance = await pay.get_balance()
    >>> receipt = await pay.transfer(to="agent-bob", amount=0.05, memo="Task")

Related: Issue #1207
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps
from typing import TYPE_CHECKING, Any

from nexus.pay.x402 import validate_wallet_address

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nexus.pay.credits import CreditsService
    from nexus.pay.x402 import X402Client

F = Any  # Generic callable type for decorators

# =============================================================================
# Exceptions
# =============================================================================

_API_KEY_PATTERN = re.compile(r"^nx_(live|test)_(.+)$")


class NexusPayError(Exception):
    """Base exception for NexusPay SDK operations."""


class BudgetExceededError(NexusPayError):
    """Raised when an operation exceeds budget limits."""


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class Balance:
    """Agent balance information."""

    available: Decimal
    reserved: Decimal

    @property
    def total(self) -> Decimal:
        return self.available + self.reserved


@dataclass
class Receipt:
    """Receipt for a completed payment."""

    id: str
    method: str  # "credits" | "x402"
    amount: Decimal
    from_agent: str
    to_agent: str
    memo: str | None
    timestamp: datetime | None
    tx_hash: str | None  # For x402 payments


@dataclass
class Reservation:
    """A pending credit reservation."""

    id: str
    amount: Decimal
    purpose: str
    expires_at: datetime | None
    status: str  # "pending" | "committed" | "released"


@dataclass
class Quote:
    """A price quote for an external service call."""

    id: str
    service: str
    price: Decimal
    params: dict[str, Any] = field(default_factory=dict)
    nexuspay: NexusPay | None = field(default=None, repr=False)

    async def execute(self) -> Receipt:
        """Execute the quoted operation by paying via x402."""
        if not self.nexuspay:
            raise NexusPayError("Quote not bound to a NexusPay instance")
        if not self.nexuspay._x402:
            raise NexusPayError("x402 not enabled")

        x402_receipt = await self.nexuspay._x402.pay(
            to_address=self.params.get("address", ""),
            amount=self.price,
        )
        return Receipt(
            id=x402_receipt.tx_hash,
            method="x402",
            amount=self.price,
            from_agent=self.nexuspay.agent_id,
            to_agent=self.service,
            memo=f"Quote {self.id}",
            timestamp=x402_receipt.timestamp,
            tx_hash=x402_receipt.tx_hash,
        )


# =============================================================================
# Budget Context
# =============================================================================


class BudgetContext:
    """Budget-limited payment context.

    Tracks spending and enforces per-transaction and daily limits.
    """

    def __init__(self, nexuspay: NexusPay, daily: Decimal, per_tx: Decimal) -> None:
        self._nexuspay = nexuspay
        self._daily = daily
        self._per_tx = per_tx
        self._spent = Decimal("0")

    @property
    def spent(self) -> Decimal:
        return self._spent

    @property
    def remaining(self) -> Decimal:
        return self._daily - self._spent

    async def transfer(self, **kwargs: Any) -> Receipt:
        amount = Decimal(str(kwargs.get("amount", 0)))
        if amount > self._per_tx:
            raise BudgetExceededError(
                f"Amount {amount} exceeds per-transaction limit of {self._per_tx}"
            )
        if self._spent + amount > self._daily:
            raise BudgetExceededError(
                f"Amount {amount} would exceed daily budget "
                f"(spent: {self._spent}, limit: {self._daily})"
            )
        receipt = await self._nexuspay.transfer(**kwargs)
        self._spent += amount
        return receipt


# =============================================================================
# NexusPay SDK
# =============================================================================


class NexusPay:
    """Unified payment SDK for agent transactions.

    Wraps TigerBeetle (internal credits) + x402 (external blockchain),
    providing a clean, Stripe-simple Python API.
    """

    def __init__(
        self,
        api_key: str,
        *,
        credits_service: CreditsService | None = None,
        x402_client: X402Client | None = None,
        x402_enabled: bool = True,
        zone_id: str = "default",
    ) -> None:
        match = _API_KEY_PATTERN.match(api_key)
        if not match:
            raise NexusPayError(
                f"Invalid API key format: expected 'nx_live_<id>' or 'nx_test_<id>', "
                f"got '{api_key}'"
            )

        self.api_key = api_key
        self.agent_id = match.group(2)
        self._zone_id = zone_id
        self._credits = credits_service
        self._x402: X402Client | None = x402_client if x402_enabled else None

    # =========================================================================
    # Internal helpers
    # =========================================================================

    @staticmethod
    def _is_external(to: str) -> bool:
        """Check if a destination is an external wallet address."""
        return validate_wallet_address(to)

    @staticmethod
    def _to_decimal(amount: float | Decimal | int) -> Decimal:
        return Decimal(str(amount))

    def _validate_positive(self, amount: Decimal) -> None:
        if amount <= 0:
            raise NexusPayError("Amount must be positive")

    def _require_credits(self) -> CreditsService:
        """Return credits service or raise if not configured."""
        if self._credits is None:
            raise NexusPayError("CreditsService not configured")
        return self._credits

    # =========================================================================
    # Balance Operations
    # =========================================================================

    async def get_balance(self) -> Balance:
        """Get current balance with available and reserved breakdown."""
        credits = self._require_credits()
        available, reserved = await credits.get_balance_with_reserved(
            self.agent_id, self._zone_id
        )
        return Balance(available=available, reserved=reserved)

    async def can_afford(self, amount: float | Decimal) -> bool:
        """Check if agent can afford an amount."""
        credits = self._require_credits()
        return await credits.check_budget(
            self.agent_id, self._to_decimal(amount), zone_id=self._zone_id
        )

    # =========================================================================
    # Transfer Operations
    # =========================================================================

    async def transfer(
        self,
        to: str,
        amount: float | Decimal,
        memo: str = "",
        idempotency_key: str | None = None,
        method: str = "auto",
    ) -> Receipt:
        """Execute a payment. Auto-routes to credits or x402."""
        dec_amount = self._to_decimal(amount)
        self._validate_positive(dec_amount)

        if method == "auto":
            method = "x402" if self._is_external(to) else "credits"

        if method == "x402":
            if not self._x402:
                raise NexusPayError("x402 not enabled")
            x402_receipt = await self._x402.pay(to_address=to, amount=dec_amount)
            return Receipt(
                id=x402_receipt.tx_hash,
                method="x402",
                amount=dec_amount,
                from_agent=self.agent_id,
                to_agent=to,
                memo=memo,
                timestamp=x402_receipt.timestamp,
                tx_hash=x402_receipt.tx_hash,
            )
        else:
            credits = self._require_credits()
            tx_id = await credits.transfer(
                from_id=self.agent_id,
                to_id=to,
                amount=dec_amount,
                memo=memo,
                idempotency_key=idempotency_key,
                zone_id=self._zone_id,
            )
            return Receipt(
                id=tx_id,
                method="credits",
                amount=dec_amount,
                from_agent=self.agent_id,
                to_agent=to,
                memo=memo,
                timestamp=datetime.now(UTC),
                tx_hash=None,
            )

    async def transfer_batch(
        self,
        transfers: list[dict[str, Any]],
    ) -> list[Receipt]:
        """Execute atomic batch transfer."""
        if not transfers:
            return []

        from nexus.pay.credits import TransferRequest

        requests = []
        for t in transfers:
            requests.append(
                TransferRequest(
                    from_id=self.agent_id,
                    to_id=t["to"],
                    amount=self._to_decimal(t.get("amount", 0)),
                    memo=t.get("memo", ""),
                )
            )

        credits = self._require_credits()
        tx_ids = await credits.transfer_batch(requests, zone_id=self._zone_id)

        now = datetime.now(UTC)
        return [
            Receipt(
                id=tx_id,
                method="credits",
                amount=self._to_decimal(t.get("amount", 0)),
                from_agent=self.agent_id,
                to_agent=t["to"],
                memo=t.get("memo", ""),
                timestamp=now,
                tx_hash=None,
            )
            for tx_id, t in zip(tx_ids, transfers, strict=False)
        ]

    # =========================================================================
    # Reservation Operations (Two-Phase)
    # =========================================================================

    async def reserve(
        self,
        amount: float | Decimal,
        timeout: int = 300,
        purpose: str = "general",
        task_id: str | None = None,  # noqa: ARG002 - stored in metadata
    ) -> Reservation:
        """Reserve credits for a pending operation."""
        dec_amount = self._to_decimal(amount)
        self._validate_positive(dec_amount)

        credits = self._require_credits()
        reservation_id = await credits.reserve(
            agent_id=self.agent_id,
            amount=dec_amount,
            timeout_seconds=timeout,
            zone_id=self._zone_id,
        )
        return Reservation(
            id=reservation_id,
            amount=dec_amount,
            purpose=purpose,
            expires_at=None,
            status="pending",
        )

    async def commit(
        self,
        reservation_id: str,
        actual_amount: float | Decimal | None = None,
    ) -> None:
        """Commit a reservation (charge actual amount)."""
        credits = self._require_credits()
        dec_amount = self._to_decimal(actual_amount) if actual_amount is not None else None
        await credits.commit_reservation(reservation_id, actual_amount=dec_amount)

    async def release(self, reservation_id: str) -> None:
        """Release a reservation (full refund)."""
        credits = self._require_credits()
        await credits.release_reservation(reservation_id)

    # =========================================================================
    # Fast Metering Operations
    # =========================================================================

    async def meter(
        self,
        amount: float | Decimal,
        event_type: str = "api_call",  # noqa: ARG002 - stored in metadata
    ) -> bool:
        """Fast credit deduction for API metering."""
        credits = self._require_credits()
        dec_amount = self._to_decimal(amount)
        self._validate_positive(dec_amount)
        return await credits.deduct_fast(
            self.agent_id, dec_amount, zone_id=self._zone_id
        )

    async def check_rate_limit(self, cost: float | Decimal = 1) -> bool:
        """Check rate limit by attempting a deduction."""
        credits = self._require_credits()
        return await credits.deduct_fast(
            self.agent_id, self._to_decimal(cost), zone_id=self._zone_id
        )

    async def bid_priority(
        self,
        queue: str,  # noqa: ARG002 - stored in reservation metadata
        bid: float | Decimal,
        task_id: str = "",
        timeout: int = 300,
    ) -> Reservation:
        """Bid for priority in a queue."""
        return await self.reserve(
            amount=bid,
            timeout=timeout,
            purpose="priority_bid",
            task_id=task_id,
        )

    # =========================================================================
    # Decorators
    # =========================================================================

    def metered(self, price: float | Decimal) -> Callable[[F], F]:
        """Decorator that charges per invocation."""
        dec_price = self._to_decimal(price)

        def decorator(func: F) -> F:
            @wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                success = await self.meter(amount=dec_price)
                if not success:
                    raise NexusPayError(
                        f"Insufficient credits for metered call (price={dec_price})"
                    )
                return await func(*args, **kwargs)

            return wrapper

        return decorator

    def budget_limited(self, max_cost: float | Decimal) -> Callable[[F], F]:
        """Decorator that fails if agent can't afford max_cost."""
        dec_max = self._to_decimal(max_cost)

        def decorator(func: F) -> F:
            @wraps(func)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                credits = self._require_credits()
                can = await credits.check_budget(
                    self.agent_id, dec_max, zone_id=self._zone_id
                )
                if not can:
                    raise BudgetExceededError(
                        f"Cannot afford max cost {dec_max}"
                    )
                return await func(*args, **kwargs)

            return wrapper

        return decorator

    # =========================================================================
    # Budget Context Manager
    # =========================================================================

    @asynccontextmanager
    async def budget(
        self,
        daily: float | Decimal = Decimal("Infinity"),
        per_tx: float | Decimal = Decimal("Infinity"),
    ) -> AsyncIterator[BudgetContext]:
        """Context manager for budget-limited operations."""
        ctx = BudgetContext(
            nexuspay=self,
            daily=self._to_decimal(daily),
            per_tx=self._to_decimal(per_tx),
        )
        yield ctx

    # =========================================================================
    # Quote & Execute
    # =========================================================================

    async def quote(
        self,
        service: str,
        params: dict[str, Any] | None = None,
    ) -> Quote:
        """Get a price quote for an external service call."""
        return Quote(
            id=str(uuid.uuid4()),
            service=service,
            price=Decimal("0"),  # Placeholder until service responds
            params=params or {},
            nexuspay=self,
        )

    # =========================================================================
    # Topup
    # =========================================================================

    async def request_topup(self, amount: float | Decimal) -> dict[str, Any]:
        """Request a topup via x402."""
        dec_amount = self._to_decimal(amount)
        self._validate_positive(dec_amount)

        if not self._x402:
            raise NexusPayError("x402 not enabled for topup")

        return {
            "amount": dec_amount,
            "currency": "USDC",
            "address": self._x402.wallet_address,
            "network": self._x402.network,
            "agent_id": self.agent_id,
        }


__all__ = [
    "Balance",
    "BudgetContext",
    "BudgetExceededError",
    "NexusPay",
    "NexusPayError",
    "Quote",
    "Receipt",
    "Reservation",
]
