"""Agent record and lifecycle state machine (Agent OS Phase 1, Issue #1240).

Defines the core domain objects for agent identity and lifecycle:
- AgentState: 4-state enum (UNKNOWN, CONNECTED, IDLE, SUSPENDED)
- VALID_TRANSITIONS: strict allowlist for valid state transitions
- AgentRecord: frozen dataclass for immutable agent snapshots
- validate_transition(): pure function for transition validation

The state machine follows external-agent philosophy (design doc Part 9):
agents are external HTTP clients, not managed processes. The generation
counter increments only on new session creation (UNKNOWN/IDLE -> CONNECTED).

Design decisions:
    - #1A: Design doc states (UNKNOWN, CONNECTED, IDLE, SUSPENDED)
    - #2A: Generation increments on new session only
    - #7A: Frozen dataclass + SQLAlchemy model pattern
    - #8A: Strict allowlist table for valid transitions

References:
    - AGENT-OS-DEEP-RESEARCH.md Part 11 (Final Architecture)
    - Issue #1240: AgentRecord with session generation counter and state machine
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


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

    @property
    def capabilities(self) -> list[str]:
        """Agent capabilities for discovery (stored in metadata).

        Returns:
            List of capability strings (e.g. ["search", "analyze", "code"]).
            Empty list if no capabilities are set.
        """
        caps = self.metadata.get("capabilities", [])
        return list(caps) if isinstance(caps, (list, tuple)) else []
