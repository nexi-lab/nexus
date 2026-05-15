"""Phase/state compatibility matrix tests (Issue #2169, #1800).

Ensures every AgentState maps to a valid AgentPhase and
the mapping is stable across refactors.

Updated for unified AgentState (Issue #1800):
- REGISTERED/WARMING_UP -> WARMING
- READY -> READY
- BUSY -> ACTIVE
- SUSPENDED -> SUSPENDED
- TERMINATED -> EVICTED
"""

import pytest

from nexus.contracts.agent_types import (
    AGENT_STATE_TO_PHASE,
    AgentPhase,
    derive_phase,
)
from nexus.contracts.process_types import AgentState


@pytest.mark.parametrize(
    "state,expected_phase",
    [
        (AgentState.REGISTERED, AgentPhase.WARMING),
        (AgentState.WARMING_UP, AgentPhase.WARMING),
        (AgentState.READY, AgentPhase.READY),
        (AgentState.BUSY, AgentPhase.ACTIVE),
        (AgentState.SUSPENDED, AgentPhase.SUSPENDED),
        (AgentState.TERMINATED, AgentPhase.EVICTED),
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
