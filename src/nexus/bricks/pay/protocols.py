"""Protocol interfaces for Pay brick external dependencies.

Defines the contracts that the Pay brick requires from external systems.
Concrete implementations are wired by factory.py at boot time.

Issue #2189: Replace concrete nexus.storage imports with Protocol abstractions.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from nexus.bricks.pay.spending_policy import SpendingApproval, SpendingPolicy


class AuditLoggerProtocol(Protocol):
    """Protocol for transaction audit logging.

    The Pay brick only needs the ``record()`` method for fire-and-forget
    audit writes. Concrete implementation: ``ExchangeAuditLogger``.
    """

    def record(
        self,
        *,
        protocol: str,
        buyer_agent_id: str,
        seller_agent_id: str,
        amount: Decimal,
        currency: str,
        status: str,
        application: str,
        zone_id: str,
        trace_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        transfer_id: str | None = None,
    ) -> str:
        """Record an exchange transaction. Returns the record ID."""
        ...


class SpendingPolicyRepository(Protocol):
    """Repository for spending policy persistence (CRUD, ledger, approvals).

    Covers the SpendingPolicy aggregate: policies, spending ledger entries,
    and approval workflow records. All monetary amounts use Decimal (credits),
    not micro-credits — conversion happens inside the repository implementation.
    """

    # -- Policy CRUD --

    async def create_policy(
        self,
        *,
        zone_id: str,
        agent_id: str | None,
        daily_limit: Decimal | None,
        weekly_limit: Decimal | None,
        monthly_limit: Decimal | None,
        per_tx_limit: Decimal | None,
        auto_approve_threshold: Decimal | None,
        max_tx_per_hour: int | None,
        max_tx_per_day: int | None,
        rules: list[dict[str, Any]] | None,
        priority: int,
        enabled: bool,
    ) -> SpendingPolicy:
        """Create and persist a new spending policy."""
        ...

    async def get_policy(
        self,
        agent_id: str | None,
        zone_id: str,
    ) -> SpendingPolicy | None:
        """Get enabled policy by agent_id + zone_id."""
        ...

    async def update_policy(
        self,
        policy_id: str,
        **updates: Any,
    ) -> tuple[SpendingPolicy | None, tuple[str, str] | None]:
        """Update policy fields.

        Returns:
            Tuple of (updated_policy, cache_key) where cache_key is
            (agent_id_or_empty, zone_id) for cache invalidation.
            Returns (None, None) if not found.
        """
        ...

    async def delete_policy(
        self,
        policy_id: str,
    ) -> tuple[bool, tuple[str, str] | None]:
        """Delete policy by ID.

        Returns:
            Tuple of (deleted, cache_key) where cache_key is
            (agent_id_or_empty, zone_id) for cache invalidation.
        """
        ...

    async def list_policies(self, zone_id: str) -> list[SpendingPolicy]:
        """List all policies for a zone, ordered by priority descending."""
        ...

    async def resolve_policy(
        self,
        agent_id: str,
        zone_id: str,
    ) -> SpendingPolicy | None:
        """Resolve effective policy: agent-specific first, then zone default."""
        ...

    # -- Spending Ledger --

    async def record_spending(
        self,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
    ) -> None:
        """Atomically increment spending for daily, weekly, monthly periods."""
        ...

    async def get_spending(
        self,
        agent_id: str,
        zone_id: str,
    ) -> dict[str, Decimal]:
        """Get current spending for all active periods.

        Returns dict like {"daily": Decimal("42.50"), "weekly": ...}.
        Missing periods default to Decimal("0").
        """
        ...

    async def get_daily_tx_count(
        self,
        agent_id: str,
        zone_id: str,
    ) -> int:
        """Get today's transaction count from the ledger."""
        ...

    # -- Approvals --

    async def create_approval(
        self,
        *,
        policy_id: str,
        agent_id: str,
        zone_id: str,
        amount: Decimal,
        to: str,
        memo: str,
        expires_at: datetime,
    ) -> SpendingApproval:
        """Create a pending approval request."""
        ...

    async def check_approval(
        self,
        approval_id: str,
        agent_id: str,
        amount: Decimal,
    ) -> SpendingApproval | None:
        """Check if approval is valid (approved, not expired, matches agent/amount)."""
        ...

    async def decide_approval(
        self,
        approval_id: str,
        decision: str,
        decided_by: str,
    ) -> SpendingApproval | None:
        """Approve or reject a pending approval. Returns None if not found/not pending."""
        ...

    async def list_pending_approvals(
        self,
        zone_id: str,
    ) -> list[SpendingApproval]:
        """List pending approvals for a zone (not expired)."""
        ...
