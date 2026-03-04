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

from nexus.bricks.pay.sdk import NexusPayError

# Re-export pure data types from contracts tier so existing consumers
# (within bricks/ and above) continue to work unchanged.
from nexus.contracts.pay_types import (  # noqa: F401
    PolicyEvaluation,
    SpendingApproval,
    SpendingLedgerEntry,
    SpendingPolicy,
)

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


# Re-export __all__ so `from nexus.bricks.pay.spending_policy import *` works
__all__ = [
    "ApprovalRequiredError",
    "PolicyDeniedError",
    "PolicyError",
    "PolicyEvaluation",
    "SpendingApproval",
    "SpendingLedgerEntry",
    "SpendingPolicy",
    "SpendingRateLimitError",
]
