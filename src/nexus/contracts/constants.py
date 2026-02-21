"""Shared constants used across multiple bricks (Issue #2032).

Constants that are referenced by more than one brick belong here,
not in a brick-specific module.  This prevents cross-brick imports
that violate the LEGO architecture rule §3.3 ("zero imports from
other bricks").

See: NEXUS-LEGO-ARCHITECTURE.md §3.3, §5.4
"""

from enum import IntEnum


class PriorityTier(IntEnum):
    """Fixed priority tiers (lower value = higher priority).

    Strict ordering: CRITICAL tasks always run before HIGH,
    HIGH before NORMAL, etc.

    Originally in ``nexus.bricks.scheduler.constants``; moved to contracts
    because both the scheduler and pay bricks depend on it.
    """

    CRITICAL = 0  # System health, security
    HIGH = 1  # User-facing, urgent
    NORMAL = 2  # Standard (default)
    LOW = 3  # Background jobs
    BEST_EFFORT = 4  # Only when idle


# String aliases for API convenience — used by pay, scheduler, and server routers.
TIER_ALIASES: dict[str, PriorityTier] = {
    "critical": PriorityTier.CRITICAL,
    "high": PriorityTier.HIGH,
    "normal": PriorityTier.NORMAL,
    "low": PriorityTier.LOW,
    "best_effort": PriorityTier.BEST_EFFORT,
}
