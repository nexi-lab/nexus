"""Request classification policy (Issue #1274, #2360).

Pure functions that map (tier, request_state, cost) → PriorityClass.
No I/O, no side effects — suitable for unit testing without mocks.

Classification rules:
1. Base mapping: TIER_TO_CLASS[tier]
2. Cost demotion: INTERACTIVE → BATCH if accumulated_cost > threshold
3. IO promotion: BACKGROUND → BATCH if request_state == IO_WAIT
4. Starvation promotion: BACKGROUND → BATCH if wait > threshold

``classify_agent_request()`` is the single source of truth for
AgentRequest → PriorityClass conversion, shared by both
SchedulerService and InMemoryScheduler (DRY).

``parse_request_enums()`` centralises the try/except fallback
parsing of AgentRequest.priority and AgentRequest.request_state.
"""

from typing import TYPE_CHECKING

from nexus.services.scheduler.constants import (
    STARVATION_PROMOTION_THRESHOLD_SECS,
    TIER_TO_CLASS,
    PriorityClass,
    PriorityTier,
    RequestState,
)

if TYPE_CHECKING:
    from nexus.contracts.protocols.scheduler import AgentRequest


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


def parse_request_enums(
    request: "AgentRequest",
) -> tuple[PriorityTier, RequestState]:
    """Parse AgentRequest.priority and .request_state into typed enums.

    Falls back to NORMAL / PENDING for unknown values.
    Single source of truth for this parsing — used by both
    ``classify_agent_request`` and ``SchedulerService.submit()``.
    """
    try:
        tier = PriorityTier(request.priority)
    except ValueError:
        tier = PriorityTier.NORMAL

    try:
        req_state = RequestState(request.request_state)
    except ValueError:
        req_state = RequestState.PENDING

    return tier, req_state


def classify_agent_request(request: "AgentRequest") -> str:
    """Classify an AgentRequest into a PriorityClass string.

    Single source of truth for AgentRequest → PriorityClass conversion.
    Delegates to ``classify_request()`` for the actual tier/state logic,
    handling AgentRequest field parsing (tier, request_state) in one place.

    Used by both ``InMemoryScheduler.classify()`` and
    ``SchedulerService.classify()`` to avoid duplicated logic.
    """
    tier, req_state = parse_request_enums(request)
    return classify_request(tier, req_state)


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
