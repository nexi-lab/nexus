"""Request classification policy (Issue #1274).

Pure functions that map (tier, request_state, cost) → PriorityClass.
No I/O, no side effects — suitable for unit testing without mocks.

Classification rules:
1. Base mapping: TIER_TO_CLASS[tier]
2. Cost demotion: INTERACTIVE → BATCH if accumulated_cost > threshold
3. IO promotion: BACKGROUND → BATCH if request_state == IO_WAIT
4. Starvation promotion: BACKGROUND → BATCH if wait > threshold
"""

from __future__ import annotations

from nexus.services.scheduler.constants import (
    STARVATION_PROMOTION_THRESHOLD_SECS,
    TIER_TO_CLASS,
    PriorityClass,
    PriorityTier,
    RequestState,
)


def classify_request(
    tier: PriorityTier,
    request_state: RequestState | str = RequestState.PENDING,
    accumulated_cost: float = 0.0,
    cost_threshold: float = 100.0,
) -> PriorityClass:
    """Classify a request into a PriorityClass.

    Args:
        tier: Base priority tier from submission.
        request_state: Current execution state of the request.
        accumulated_cost: Cumulative cost for the submitting agent.
        cost_threshold: Cost threshold for INTERACTIVE → BATCH demotion.

    Returns:
        Computed PriorityClass.
    """
    base_class = TIER_TO_CLASS.get(tier, PriorityClass.BATCH)

    # Cost demotion: INTERACTIVE → BATCH if agent is too expensive
    if base_class == PriorityClass.INTERACTIVE and accumulated_cost > cost_threshold:
        base_class = PriorityClass.BATCH

    # IO promotion: BACKGROUND → BATCH if waiting on I/O
    state_str = request_state if isinstance(request_state, str) else request_state.value
    if base_class == PriorityClass.BACKGROUND and state_str == RequestState.IO_WAIT:
        base_class = PriorityClass.BATCH

    return base_class


def should_promote_for_starvation(
    wait_seconds: float,
    current_class: PriorityClass | str,
    threshold: float = STARVATION_PROMOTION_THRESHOLD_SECS,
) -> PriorityClass:
    """Check if a task should be promoted due to starvation.

    Args:
        wait_seconds: How long the task has been waiting.
        current_class: Current priority class of the task.
        threshold: Seconds before BACKGROUND is promoted.

    Returns:
        Promoted PriorityClass (or unchanged if no promotion needed).
    """
    cls_str = current_class if isinstance(current_class, str) else current_class.value
    if cls_str == PriorityClass.BACKGROUND and wait_seconds > threshold:
        return PriorityClass.BATCH
    return PriorityClass(cls_str)
