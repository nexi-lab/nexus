"""Constants for the Nexus Scheduler priority system.

Defines priority tiers, aging configuration, and boost limits.

Related: Issue #1212
"""

from __future__ import annotations

from decimal import Decimal
from enum import IntEnum


class PriorityTier(IntEnum):
    """Fixed priority tiers (lower value = higher priority).

    Strict ordering: CRITICAL tasks always run before HIGH,
    HIGH before NORMAL, etc.
    """

    CRITICAL = 0  # System health, security
    HIGH = 1  # User-facing, urgent
    NORMAL = 2  # Standard (default)
    LOW = 3  # Background jobs
    BEST_EFFORT = 4  # Only when idle


# String aliases for API convenience
TIER_ALIASES: dict[str, PriorityTier] = {
    "critical": PriorityTier.CRITICAL,
    "high": PriorityTier.HIGH,
    "normal": PriorityTier.NORMAL,
    "low": PriorityTier.LOW,
    "best_effort": PriorityTier.BEST_EFFORT,
}

# =============================================================================
# Aging Configuration
# =============================================================================

# How often the aging sweep runs (seconds)
AGING_INTERVAL_SECONDS = 60

# Time before a task gains +1 tier boost from aging (seconds)
AGING_THRESHOLD_SECONDS = 120

# Auto-escalate to HIGH after this many seconds waiting
MAX_WAIT_SECONDS = 600

# =============================================================================
# Boost Configuration
# =============================================================================

# Maximum tier boost from price (e.g., LOW -> NORMAL -> HIGH = +2)
MAX_BOOST_TIERS = 2

# Cost per tier boost in credits
BOOST_COST_PER_TIER = Decimal("0.01")

# =============================================================================
# Task Status
# =============================================================================

TASK_STATUS_QUEUED = "queued"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_CANCELLED = "cancelled"

VALID_TASK_STATUSES = frozenset(
    {
        TASK_STATUS_QUEUED,
        TASK_STATUS_RUNNING,
        TASK_STATUS_COMPLETED,
        TASK_STATUS_FAILED,
        TASK_STATUS_CANCELLED,
    }
)
