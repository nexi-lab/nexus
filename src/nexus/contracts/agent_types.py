"""Agent domain types shared across tiers (Issue #2032).

Pure value objects for agent identity and lifecycle. These types have zero
runtime dependencies on kernel, services, or bricks — only stdlib imports.

Originally in ``nexus.system_services.agents.agent_record``; moved here so bricks
can import them without violating the zero-core-imports rule.

Issue #2169: Added AgentSpec/AgentStatus for declarative agent management
with drift detection (Kubernetes-inspired spec/status separation).
"""

import types
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any

from nexus.contracts.qos import AgentQoS


class EvictionReason(StrEnum):
    """Reason for agent eviction (Issue #2170).

    Used in EvictionResult to distinguish why an eviction cycle ran.
    """

    NORMAL_PRESSURE = "normal_pressure"
    PRESSURE_WARNING = "pressure_warning"
    PRESSURE_CRITICAL = "pressure_critical"
    OVER_AGENT_CAP = "over_agent_cap"
    MANUAL = "manual"
    COOLDOWN = "cooldown"
    NO_CANDIDATES = "no_candidates"
    CHECKPOINT_TIMEOUT = "checkpoint_timeout"


class AgentState(Enum):
    """Agent lifecycle states (external-agent philosophy).

    State machine:
        UNKNOWN -----> CONNECTED -----> IDLE
           ^              |               |
           |              v               v
           +---------- SUSPENDED <--------+
                          |
                          +-----> CONNECTED (reactivation)

    UNKNOWN: Agent registered but never connected (initial state)
    CONNECTED: Agent has an active session (generation incremented)
    IDLE: Agent session ended normally, can reconnect
    SUSPENDED: Agent temporarily disabled (admin action or policy)
    """

    UNKNOWN = "UNKNOWN"
    CONNECTED = "CONNECTED"
    IDLE = "IDLE"
    SUSPENDED = "SUSPENDED"


# Strict allowlist for valid state transitions (Decision #8A).
# Any transition not in this table is invalid. No self-transitions allowed.
VALID_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.UNKNOWN: frozenset({AgentState.CONNECTED}),
    AgentState.CONNECTED: frozenset({AgentState.IDLE, AgentState.SUSPENDED}),
    AgentState.IDLE: frozenset({AgentState.CONNECTED, AgentState.SUSPENDED}),
    AgentState.SUSPENDED: frozenset({AgentState.CONNECTED}),
}

# States that trigger generation increment when transitioning TO CONNECTED
_NEW_SESSION_SOURCES = frozenset({AgentState.UNKNOWN, AgentState.IDLE, AgentState.SUSPENDED})


def validate_transition(current: AgentState, target: AgentState) -> bool:
    """Check if a state transition is valid according to the allowlist.

    Pure function with no side effects. Does not modify any state.

    Args:
        current: Current agent state.
        target: Desired target state.

    Returns:
        True if the transition is valid, False otherwise.

    Examples:
        >>> validate_transition(AgentState.UNKNOWN, AgentState.CONNECTED)
        True
        >>> validate_transition(AgentState.UNKNOWN, AgentState.IDLE)
        False
        >>> validate_transition(AgentState.CONNECTED, AgentState.CONNECTED)
        False
    """
    allowed = VALID_TRANSITIONS.get(current, frozenset())
    return target in allowed


def is_new_session(current: AgentState, target: AgentState) -> bool:
    """Check if a transition represents a new session (generation should increment).

    A new session occurs when transitioning TO CONNECTED from any non-CONNECTED
    state. This is the only time the generation counter increments (Decision #2A).

    Args:
        current: Current agent state.
        target: Desired target state.

    Returns:
        True if this transition starts a new session.
    """
    return target is AgentState.CONNECTED and current in _NEW_SESSION_SOURCES


@dataclass(frozen=True)
class AgentRecord:
    """Immutable snapshot of an agent's identity and lifecycle state.

    Follows the frozen dataclass + SQLAlchemy model pattern (Decision #7A):
    this domain object is separate from the mutable persistence layer
    (AgentRecordModel in models.py). The registry always returns new
    AgentRecord instances, never mutates existing ones.

    Attributes:
        agent_id: Unique agent identifier (e.g., "alice,ImpersonatedUser")
        owner_id: User ID who owns this agent
        zone_id: Zone/organization ID for multi-zone isolation
        name: Human-readable display name
        state: Current lifecycle state (AgentState enum)
        generation: Session generation counter (increments on new session only)
        last_heartbeat: Timestamp of last heartbeat (None if never heartbeated)
        metadata: Arbitrary agent metadata (platform, endpoint_url, etc.)
        created_at: When the agent was first registered
        updated_at: When the agent record was last modified
        qos: Agent QoS assignment with scheduling/eviction class and optional
            overrides (Issue #2171). Defaults to standard/standard.
    """

    agent_id: str
    owner_id: str
    zone_id: str | None
    name: str | None
    state: AgentState
    generation: int
    last_heartbeat: datetime | None
    metadata: types.MappingProxyType[str, Any]
    created_at: datetime
    updated_at: datetime
    qos: AgentQoS = field(default_factory=AgentQoS)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict matching the legacy agents.register_agent() return format.

        Returns a mutable dict with keys: agent_id, user_id (alias for owner_id),
        name, zone_id, metadata (mutable copy), created_at (ISO string), state, generation.

        Returns:
            Dict with backward-compatible keys.
        """
        return {
            "agent_id": self.agent_id,
            "user_id": self.owner_id,
            "name": self.name,
            "zone_id": self.zone_id,
            "metadata": dict(self.metadata),
            "created_at": self.created_at.isoformat(),
            "state": self.state.value,
            "generation": self.generation,
            "qos": self.qos.to_dict(),
        }

    @property
    def capabilities(self) -> list[str]:
        """Agent capabilities for discovery (stored in metadata).

        Returns:
            List of capability strings (e.g. ["search", "analyze", "code"]).
            Empty list if no capabilities are set.
        """
        caps = self.metadata.get("capabilities", [])
        return list(caps) if isinstance(caps, list | tuple) else []


# ---------------------------------------------------------------------------
# AgentSpec/AgentStatus types (Issue #2169)
# ---------------------------------------------------------------------------


class QoSClass(StrEnum):
    """Quality-of-service tier for agent resource management.

    Determines eviction priority: spot < standard < premium.
    """

    PREMIUM = "premium"
    STANDARD = "standard"
    SPOT = "spot"


class AgentPhase(StrEnum):
    """External-facing agent lifecycle phase.

    Maps from internal ``AgentState`` plus conditions to a richer
    phase model suitable for API consumers and dashboards.
    """

    WARMING = "warming"
    READY = "ready"
    ACTIVE = "active"
    THINKING = "thinking"
    IDLE = "idle"
    SUSPENDED = "suspended"
    EVICTED = "evicted"


# Mapping from internal state to external phase (default, before condition overrides).
AGENT_STATE_TO_PHASE: dict[AgentState, AgentPhase] = {
    AgentState.UNKNOWN: AgentPhase.WARMING,
    AgentState.CONNECTED: AgentPhase.ACTIVE,
    AgentState.IDLE: AgentPhase.IDLE,
    AgentState.SUSPENDED: AgentPhase.SUSPENDED,
}


@dataclass(frozen=True, slots=True)
class AgentResources:
    """Resource requests or limits for an agent (spec side).

    None values mean "unlimited" (no constraint).
    """

    token_budget: int | None = None
    token_request: int | None = None
    storage_limit_mb: int | None = None
    context_limit: int | None = None


@dataclass(frozen=True, slots=True)
class AgentResourceUsage:
    """Observed resource consumption (status side)."""

    tokens_used: int = 0
    storage_used_mb: float = 0.0
    context_usage_pct: float = 0.0


@dataclass(frozen=True, slots=True)
class AgentCondition:
    """A single condition describing an aspect of agent health.

    Follows Kubernetes-style condition semantics with open string
    type/status to allow extension without enum changes.

    Attributes:
        type: Condition kind (e.g. "Ready", "ModelConnected", "Scheduled").
        status: Tri-state string: "True", "False", or "Unknown".
        reason: Machine-readable camelCase reason (e.g. "HeartbeatTimeout").
        message: Human-readable description.
        last_transition: When this condition last changed status.
        observed_generation: The spec_generation this condition was evaluated against.
    """

    type: str
    status: str
    reason: str
    message: str
    last_transition: datetime
    observed_generation: int


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """Desired state declaration for an agent (Issue #2169).

    Immutable value object stored as JSON in the ``agent_spec`` column.
    Changes bump ``spec_generation`` for drift detection.

    Attributes:
        agent_type: Logical agent type (e.g. "analyst", "coder").
        capabilities: Set of capability strings.
        resource_requests: Minimum resources the agent needs.
        resource_limits: Maximum resources the agent may consume.
        qos_class: Quality-of-service tier (eviction priority).
        zone_affinity: Preferred zone for scheduling.
        spec_generation: Monotonic counter incremented on spec changes.
    """

    agent_type: str
    capabilities: frozenset[str]
    resource_requests: AgentResources
    resource_limits: AgentResources
    qos_class: QoSClass = QoSClass.STANDARD
    zone_affinity: str | None = None
    spec_generation: int = 1


@dataclass(frozen=True, slots=True)
class AgentStatus:
    """Observed state of an agent, computed on read (Issue #2169).

    Not persisted directly — derived from ``AgentRecord`` fields,
    heartbeat buffer, and the stored ``AgentSpec``.

    Attributes:
        phase: External lifecycle phase (derived from state + conditions).
        observed_generation: Last spec_generation the system acted on.
        conditions: Tuple of health/readiness conditions.
        resource_usage: Current resource consumption.
        last_heartbeat: Most recent heartbeat timestamp.
        last_activity: Most recent activity timestamp.
        inbox_depth: Number of pending messages (future use).
        context_usage_pct: Context window usage percentage.
    """

    phase: AgentPhase
    observed_generation: int
    conditions: tuple[AgentCondition, ...]
    resource_usage: AgentResourceUsage
    last_heartbeat: datetime | None
    last_activity: datetime | None
    inbox_depth: int = 0
    context_usage_pct: float = 0.0


def derive_phase(
    state: AgentState,
    conditions: tuple[AgentCondition, ...] = (),
) -> AgentPhase:
    """Map internal state + conditions to an external phase.

    The base mapping comes from ``AGENT_STATE_TO_PHASE``. Conditions
    can override the phase (e.g. a "Ready" condition on a CONNECTED
    agent promotes it from ACTIVE to READY).

    Args:
        state: Current internal agent state.
        conditions: Tuple of active conditions.

    Returns:
        The derived AgentPhase.
    """
    base_phase = AGENT_STATE_TO_PHASE.get(state, AgentPhase.WARMING)

    # Condition overrides
    for cond in conditions:
        if cond.type == "Ready" and cond.status == "True" and base_phase == AgentPhase.ACTIVE:
            return AgentPhase.READY
        if cond.type == "Evicted" and cond.status == "True":
            return AgentPhase.EVICTED
        if cond.type == "Thinking" and cond.status == "True" and base_phase == AgentPhase.ACTIVE:
            return AgentPhase.THINKING

    return base_phase


def detect_drift(spec: AgentSpec, status: AgentStatus) -> bool:
    """Check if the observed state has drifted from the desired spec.

    Drift is detected when the spec's generation counter differs from
    the status's observed generation, indicating the system has not
    yet reconciled to the latest spec.

    Args:
        spec: Desired agent state.
        status: Observed agent state.

    Returns:
        True if drift is detected (spec has changed since last reconciliation).
    """
    return spec.spec_generation != status.observed_generation
