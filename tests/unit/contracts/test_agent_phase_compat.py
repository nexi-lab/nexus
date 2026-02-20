"""Phase/state compatibility matrix tests (Issue #2169).

Ensures every AgentState maps to a valid AgentPhase and
the mapping is stable across refactors.
"""

from __future__ import annotations

import pytest

from nexus.contracts.agent_types import (
    AGENT_STATE_TO_PHASE,
    AgentPhase,
    AgentState,
    derive_phase,
)


@pytest.mark.parametrize(
    "state,expected_phase",
    [
        (AgentState.UNKNOWN, AgentPhase.WARMING),
        (AgentState.CONNECTED, AgentPhase.ACTIVE),
        (AgentState.IDLE, AgentPhase.IDLE),
        (AgentState.SUSPENDED, AgentPhase.SUSPENDED),
    ],
)
def test_phase_derived_from_state(state: AgentState, expected_phase: AgentPhase) -> None:
    """Each AgentState maps to its expected default AgentPhase."""
    assert derive_phase(state) == expected_phase
    assert AGENT_STATE_TO_PHASE[state] == expected_phase


def test_all_states_have_phase_mapping() -> None:
    """Every AgentState value has an entry in AGENT_STATE_TO_PHASE."""
    for state in AgentState:
        assert state in AGENT_STATE_TO_PHASE, f"{state} missing from AGENT_STATE_TO_PHASE"


def test_all_mapped_phases_are_valid() -> None:
    """Every phase in the mapping is a valid AgentPhase member."""
    for phase in AGENT_STATE_TO_PHASE.values():
        assert isinstance(phase, AgentPhase)
