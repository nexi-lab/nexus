"""Priority computation for the hybrid scheduling system.

Implements the 4-layer priority model:
1. Base tier: Fixed priority tier from submission
2. Price boost: Capped at MAX_BOOST_TIERS (computed from boost_amount)
3. Aging: Tasks gain priority over time (1 tier per AGING_THRESHOLD_SECONDS)
4. Max-wait: Tasks exceeding MAX_WAIT_SECONDS escalate to HIGH

effective_tier = max(0, base_tier - boost_tiers - aging_tiers)
If wait > MAX_WAIT_SECONDS: effective_tier = min(effective_tier, HIGH)

Related: Issue #1212
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from nexus.scheduler.constants import (
    AGING_THRESHOLD_SECONDS,
    BOOST_COST_PER_TIER,
    MAX_BOOST_TIERS,
    MAX_WAIT_SECONDS,
    PriorityTier,
)
from nexus.scheduler.models import TaskSubmission


def compute_boost_tiers(boost_amount: Decimal) -> int:
    """Compute tier boost from payment amount.

    Args:
        boost_amount: Credits paid for priority boost.

    Returns:
        Number of tiers to boost (0 to MAX_BOOST_TIERS).
    """
    if boost_amount <= 0:
        return 0
    raw_tiers = int(boost_amount / BOOST_COST_PER_TIER)
    return min(raw_tiers, MAX_BOOST_TIERS)


def compute_effective_tier(
    task: TaskSubmission,
    enqueued_at: datetime,
    now: datetime,
) -> int:
    """Compute effective priority tier for scheduling.

    Combines all 4 priority layers into a single integer
    where lower = higher priority.

    Args:
        task: The task submission with priority signals.
        enqueued_at: When the task was enqueued.
        now: Current time (for aging calculation).

    Returns:
        Effective tier (0 = CRITICAL, 4 = BEST_EFFORT).
        Never negative.
    """
    base_tier = task.priority.value

    # Layer 2: Price boost
    boost = compute_boost_tiers(task.boost_amount)

    # Layer 3: Aging
    wait_seconds = max(0.0, (now - enqueued_at).total_seconds())
    aging_tiers = int(wait_seconds / AGING_THRESHOLD_SECONDS)

    # Compute effective tier
    effective = base_tier - boost - aging_tiers

    # Layer 4: Max-wait escalation
    if wait_seconds > MAX_WAIT_SECONDS:
        effective = min(effective, PriorityTier.HIGH)

    # Floor at 0 (CRITICAL)
    return max(0, effective)


def validate_submission(task: TaskSubmission) -> None:
    """Validate a task submission.

    Args:
        task: The task submission to validate.

    Raises:
        ValueError: If any field is invalid.
    """
    if not task.agent_id:
        raise ValueError("agent_id must not be empty")

    if not task.executor_id:
        raise ValueError("executor_id must not be empty")

    if not task.task_type:
        raise ValueError("task_type must not be empty")

    if task.boost_amount < 0:
        raise ValueError("boost amount must be non-negative")

    max_boost_cost = BOOST_COST_PER_TIER * MAX_BOOST_TIERS
    if task.boost_amount > max_boost_cost:
        raise ValueError(
            f"boost amount {task.boost_amount} exceeds maximum "
            f"of {max_boost_cost} ({MAX_BOOST_TIERS} tiers)"
        )

    if task.deadline is not None and task.deadline < datetime.now(UTC):
        raise ValueError("deadline must be in the future")
