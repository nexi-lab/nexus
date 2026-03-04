"""Pay data types and conversion utilities (contracts tier).

Pure data classes and conversion functions shared between bricks/pay
and storage/repositories. Moved here so storage tier can import without
violating the tier boundary (storage cannot import from bricks).

Issue #2189: Spending policy data types.
Issue #1199: Micro-credit conversion constants.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

# =============================================================================
# Amount Conversion
# =============================================================================
# Credits are stored as integers in micro-units (6 decimal places)
# Example: 1.0 credit = 1_000_000 micro-credits
MICRO_UNIT_SCALE = 1_000_000


def credits_to_micro(credits: Decimal | float | int) -> int:
    """Convert credits to micro-credits (internal storage format).

    Uses Decimal arithmetic internally to avoid float precision loss.

    Args:
        credits: Amount in credits (e.g., Decimal("1.5") or 1.5)

    Returns:
        Amount in micro-credits (e.g., 1_500_000)
    """
    return int(Decimal(str(credits)) * MICRO_UNIT_SCALE)


def micro_to_credits(micro: int) -> Decimal:
    """Convert micro-credits to credits (display format).

    Args:
        micro: Amount in micro-credits

    Returns:
        Amount in credits as Decimal.
    """
    return Decimal(str(micro)) / MICRO_UNIT_SCALE


# =============================================================================
# Data Classes
# =============================================================================


@dataclass(frozen=True)
class SpendingPolicy:
    """Declarative spending policy for an agent or zone.

    Resolution: agent-specific (highest priority) -> zone default.
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
