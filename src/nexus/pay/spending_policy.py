"""Spending Policy Engine — data models and exceptions.

Issue #1358: Budget policies, approval workflows, rate limits, and policy DSL.

This module defines the core data structures for spending policies:
- SpendingPolicy: declarative budget limits per agent/zone
- SpendingLedgerEntry: period-based spending counters
- PolicyEvaluation: result of evaluating a transaction against policies
- SpendingApproval: approval workflow records (Phase 2)

Architecture:
    PolicyEnforcedPayment (wrapper) → SpendingPolicyService → SpendingPolicy
    Follows Lego Mechanism 2: recursive wrapping on PaymentProtocol.
    Zero imports from nexus.core — this is a self-contained brick.

Default behavior: open by default (no policy = allow all transactions).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from nexus.pay.sdk import NexusPayError

# =============================================================================
# Exceptions
# =============================================================================


class PolicyError(NexusPayError):
    """Base exception for all spending policy violations."""


class PolicyDeniedError(PolicyError):
    """Transaction denied by a spending policy rule.

    Attributes:
        policy_id: The policy that denied the transaction.
        denied_reason: Human-readable explanation.
    """

    def __init__(self, message: str, *, policy_id: str | None = None) -> None:
        super().__init__(message)
        self.policy_id = policy_id
        self.denied_reason = message


class ApprovalRequiredError(PolicyError):
    """Transaction requires approval before execution (Phase 2).

    Raised when the amount exceeds auto_approve_threshold.
    The caller must create an approval request and retry with approval_id.

    Attributes:
        approval_id: ID of the pending approval request.
        policy_id: The policy that requires approval.
    """

    def __init__(
        self,
        message: str,
        *,
        approval_id: str | None = None,
        policy_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.approval_id = approval_id
        self.policy_id = policy_id


class SpendingRateLimitError(PolicyError):
    """Transaction rate limit exceeded (Phase 3).

    Raised when the transaction count exceeds per-hour or per-day limits.

    Attributes:
        policy_id: The policy that enforces the rate limit.
        limit_type: "hourly" or "daily".
    """

    def __init__(
        self,
        message: str,
        *,
        policy_id: str | None = None,
        limit_type: str | None = None,
    ) -> None:
        super().__init__(message)
        self.policy_id = policy_id
        self.limit_type = limit_type


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class SpendingPolicy:
    """Declarative spending policy for an agent or zone.

    Resolution: agent-specific (highest priority) → zone default.
    All limit amounts are in credits (Decimal), not micro-credits.

    Fields with None mean "no limit for this period."
    """

    policy_id: str
    zone_id: str
    agent_id: str | None = None  # None = zone-level default
    daily_limit: Decimal | None = None
    weekly_limit: Decimal | None = None
    monthly_limit: Decimal | None = None
    per_tx_limit: Decimal | None = None
    auto_approve_threshold: Decimal | None = None  # Phase 2: approval workflows
    max_tx_per_hour: int | None = None  # Phase 3: rate controls
    max_tx_per_day: int | None = None  # Phase 3: rate controls
    rules: list[dict[str, Any]] | None = None  # Phase 4: policy DSL
    priority: int = 0  # Higher value overrides lower
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class SpendingLedgerEntry:
    """Period-based spending counter for an agent.

    One entry per (agent_id, zone_id, period_type, period_start).
    Updated atomically via PostgreSQL UPSERT on each successful transfer.
    """

    agent_id: str
    zone_id: str
    period_type: str  # "daily" | "weekly" | "monthly"
    period_start: date
    amount_spent: Decimal = Decimal("0")
    tx_count: int = 0


@dataclass(frozen=True)
class SpendingApproval:
    """A pending or resolved approval for a transaction (Phase 2).

    Created when a transaction exceeds the auto_approve_threshold.
    An admin approves/rejects, then the agent retries with the approval_id.
    """

    approval_id: str
    policy_id: str
    agent_id: str
    zone_id: str
    amount: Decimal
    to: str
    memo: str = ""
    status: str = "pending"  # pending | approved | rejected | expired
    requested_at: datetime | None = None
    decided_at: datetime | None = None
    decided_by: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class PolicyEvaluation:
    """Result of evaluating a transaction against spending policies.

    On denial, provides the specific reason and policy that blocked it.
    On approval, optionally provides remaining budget information.
    """

    allowed: bool
    denied_reason: str | None = None
    policy_id: str | None = None
    remaining_budget: dict[str, Decimal] = field(default_factory=dict)
    requires_approval: bool = False  # Phase 2: needs admin approval
    approval_id: str | None = None  # Phase 2: pending approval ID
