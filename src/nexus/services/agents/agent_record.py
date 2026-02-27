"""Agent record and lifecycle state machine (Agent OS Phase 1, Issue #1240).

Re-exports from ``nexus.contracts.agent_types`` — the canonical home for these
pure domain types.  This module preserves backward compatibility for existing
``from nexus.services.agents.agent_record import ...`` imports.

See ``nexus.contracts.agent_types`` for full documentation.
"""

from nexus.contracts.agent_types import (
    VALID_TRANSITIONS,
    AgentRecord,
    AgentState,
    is_new_session,
    validate_transition,
)

__all__ = [
    "AgentState",
    "VALID_TRANSITIONS",
    "AgentRecord",
    "validate_transition",
    "is_new_session",
]
