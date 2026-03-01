"""Constants for the Nexus Scheduler priority system.

Defines aging configuration, boost limits, and scheduler-specific enums.

PriorityTier and TIER_ALIASES live in ``nexus.contracts.constants``
because they are shared across bricks (pay, scheduler, server).

Related: Issue #1212
"""

from decimal import Decimal
from enum import StrEnum

from nexus.contracts.constants import TIER_ALIASES, PriorityTier

# Re-exported from contracts for convenience (canonical location: nexus.contracts.constants).
__all__ = ["PriorityTier", "TIER_ALIASES"]

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

# =============================================================================
# Astraea-Style Enums (Issue #1274)
# =============================================================================


class RequestState(StrEnum):
    """Request execution state for Astraea-style classification."""

    IO_WAIT = "io_wait"
    COMPUTE = "compute"
    TOOL_CALL = "tool_call"
    IDLE = "idle"
    PENDING = "pending"


class PriorityClass(StrEnum):
    """Scheduling class derived from tier + runtime signals."""

    INTERACTIVE = "interactive"
    BATCH = "batch"
    BACKGROUND = "background"


# Maps PriorityTier → PriorityClass (base mapping before runtime adjustments)
TIER_TO_CLASS: dict[PriorityTier, PriorityClass] = {
    PriorityTier.CRITICAL: PriorityClass.INTERACTIVE,
    PriorityTier.HIGH: PriorityClass.INTERACTIVE,
    PriorityTier.NORMAL: PriorityClass.BATCH,
    PriorityTier.LOW: PriorityClass.BACKGROUND,
    PriorityTier.BEST_EFFORT: PriorityClass.BACKGROUND,
}

# =============================================================================
# HRRN Constants
# =============================================================================

DEFAULT_EST_SERVICE_TIME_SECS: float = 30.0
STARVATION_PROMOTION_THRESHOLD_SECS: float = 900.0

# =============================================================================
# Hook Phase Constants
# =============================================================================

HOOK_PRE_CLASSIFY = "pre_classify"
HOOK_PRE_DEQUEUE = "pre_dequeue"
HOOK_PRE_ADMIT = "pre_admit"
