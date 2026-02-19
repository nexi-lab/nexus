"""Agent domain types shared across tiers (Issue #2032).

Pure value objects for agent identity and lifecycle. These types have zero
runtime dependencies on kernel, services, or bricks — only stdlib imports.

Originally in ``nexus.services.agents.agent_record``; moved here so bricks
can import them without violating the zero-core-imports rule.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, StrEnum
from typing import Any


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
        context_manifest: Serialized context sources for deterministic pre-execution
            (Issue #1341). Stored as tuple of dicts to keep kernel free of Pydantic.
            Deserialized into ContextSource models at resolution time.
        created_at: When the agent was first registered
        updated_at: When the agent record was last modified
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
    context_manifest: tuple[dict[str, Any], ...] = ()

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
