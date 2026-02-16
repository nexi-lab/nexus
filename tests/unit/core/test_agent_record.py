"""Unit tests for AgentRecord and AgentState state machine (Issue #1240).

Tests cover:
- AgentState enum: all 4 states exist and are distinct
- VALID_TRANSITIONS: strict allowlist covering all 16 state pairs
- validate_transition(): parametrized 16-cell matrix (Decision #9A)
- AgentRecord: frozen dataclass immutability, defaults, field access
- Edge cases: self-transitions, exhaustive coverage of transition table
"""

from __future__ import annotations

import types
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from nexus.services.agents.agent_record import (
    VALID_TRANSITIONS,
    AgentRecord,
    AgentState,
    validate_transition,
)

# ---------------------------------------------------------------------------
# AgentState enum tests
# ---------------------------------------------------------------------------


class TestAgentState:
    """Tests for the AgentState enum."""

    def test_has_four_states(self):
        """AgentState enum must have exactly 4 members."""
        assert len(AgentState) == 4

    def test_state_values(self):
        """Each state has the expected string value."""
        assert AgentState.UNKNOWN.value == "UNKNOWN"
        assert AgentState.CONNECTED.value == "CONNECTED"
        assert AgentState.IDLE.value == "IDLE"
        assert AgentState.SUSPENDED.value == "SUSPENDED"

    def test_states_are_distinct(self):
        """All states are distinct enum members."""
        states = list(AgentState)
        assert len(states) == len(set(states))

    def test_from_string(self):
        """AgentState can be constructed from string value."""
        assert AgentState("UNKNOWN") is AgentState.UNKNOWN
        assert AgentState("CONNECTED") is AgentState.CONNECTED
        assert AgentState("IDLE") is AgentState.IDLE
        assert AgentState("SUSPENDED") is AgentState.SUSPENDED

    def test_invalid_string_raises(self):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            AgentState("INVALID")


# ---------------------------------------------------------------------------
# VALID_TRANSITIONS allowlist tests
# ---------------------------------------------------------------------------


class TestValidTransitions:
    """Tests for the VALID_TRANSITIONS strict allowlist (Decision #8A)."""

    def test_covers_all_states_as_keys(self):
        """Every AgentState must appear as a key in VALID_TRANSITIONS."""
        for state in AgentState:
            assert state in VALID_TRANSITIONS, f"{state} missing from VALID_TRANSITIONS"

    def test_no_extra_keys(self):
        """VALID_TRANSITIONS should not have keys that aren't AgentState members."""
        for key in VALID_TRANSITIONS:
            assert isinstance(key, AgentState), f"Unexpected key: {key}"

    def test_values_are_frozensets_of_agent_state(self):
        """Each value must be a frozenset of AgentState members."""
        for state, targets in VALID_TRANSITIONS.items():
            assert isinstance(targets, frozenset), f"{state} targets not frozenset"
            for target in targets:
                assert isinstance(target, AgentState), (
                    f"{state} has non-AgentState target: {target}"
                )

    def test_no_self_transitions(self):
        """No state should be able to transition to itself."""
        for state, targets in VALID_TRANSITIONS.items():
            assert state not in targets, f"{state} allows self-transition"

    def test_unknown_transitions(self):
        """UNKNOWN can only transition to CONNECTED."""
        assert VALID_TRANSITIONS[AgentState.UNKNOWN] == frozenset({AgentState.CONNECTED})

    def test_connected_transitions(self):
        """CONNECTED can transition to IDLE or SUSPENDED."""
        assert VALID_TRANSITIONS[AgentState.CONNECTED] == frozenset(
            {AgentState.IDLE, AgentState.SUSPENDED}
        )

    def test_idle_transitions(self):
        """IDLE can transition to CONNECTED or SUSPENDED."""
        assert VALID_TRANSITIONS[AgentState.IDLE] == frozenset(
            {AgentState.CONNECTED, AgentState.SUSPENDED}
        )

    def test_suspended_transitions(self):
        """SUSPENDED can only transition to CONNECTED."""
        assert VALID_TRANSITIONS[AgentState.SUSPENDED] == frozenset({AgentState.CONNECTED})


# ---------------------------------------------------------------------------
# validate_transition() parametrized 16-cell matrix (Decision #9A)
# ---------------------------------------------------------------------------

_U = AgentState.UNKNOWN
_C = AgentState.CONNECTED
_I = AgentState.IDLE
_S = AgentState.SUSPENDED


class TestValidateTransition:
    """Parametrized 16-cell state transition matrix (Decision #9A)."""

    @pytest.mark.parametrize(
        "current,target,valid",
        [
            # From UNKNOWN
            (_U, _U, False),
            (_U, _C, True),
            (_U, _I, False),
            (_U, _S, False),
            # From CONNECTED
            (_C, _U, False),
            (_C, _C, False),
            (_C, _I, True),
            (_C, _S, True),
            # From IDLE
            (_I, _U, False),
            (_I, _C, True),
            (_I, _I, False),
            (_I, _S, True),
            # From SUSPENDED
            (_S, _U, False),
            (_S, _C, True),
            (_S, _I, False),
            (_S, _S, False),
        ],
        ids=[
            "UNKNOWN->UNKNOWN",
            "UNKNOWN->CONNECTED",
            "UNKNOWN->IDLE",
            "UNKNOWN->SUSPENDED",
            "CONNECTED->UNKNOWN",
            "CONNECTED->CONNECTED",
            "CONNECTED->IDLE",
            "CONNECTED->SUSPENDED",
            "IDLE->UNKNOWN",
            "IDLE->CONNECTED",
            "IDLE->IDLE",
            "IDLE->SUSPENDED",
            "SUSPENDED->UNKNOWN",
            "SUSPENDED->CONNECTED",
            "SUSPENDED->IDLE",
            "SUSPENDED->SUSPENDED",
        ],
    )
    def test_state_transition_matrix(self, current, target, valid):
        """Each cell in the 4x4 transition matrix returns expected validity."""
        assert validate_transition(current, target) == valid


# ---------------------------------------------------------------------------
# AgentRecord frozen dataclass tests
# ---------------------------------------------------------------------------


class TestAgentRecord:
    """Tests for the AgentRecord frozen dataclass."""

    @pytest.fixture
    def now(self):
        return datetime.now(UTC)

    @pytest.fixture
    def record(self, now):
        return AgentRecord(
            agent_id="agent-1",
            owner_id="alice",
            zone_id="default",
            name="Test Agent",
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )

    def test_is_frozen(self, record):
        """AgentRecord is immutable (frozen dataclass)."""
        with pytest.raises(FrozenInstanceError):
            record.state = AgentState.CONNECTED  # type: ignore[misc]

    def test_field_access(self, record):
        """All fields are accessible."""
        assert record.agent_id == "agent-1"
        assert record.owner_id == "alice"
        assert record.zone_id == "default"
        assert record.name == "Test Agent"
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0
        assert record.last_heartbeat is None
        assert record.metadata == {}

    def test_defaults(self, now):
        """AgentRecord has expected defaults for state and generation."""
        record = AgentRecord(
            agent_id="agent-2",
            owner_id="bob",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0

    def test_equality(self, now):
        """Two records with same fields are equal."""
        r1 = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        r2 = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert r1 == r2

    def test_inequality_on_generation(self, now):
        """Records with different generation are not equal."""
        r1 = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        r2 = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.CONNECTED,
            generation=1,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert r1 != r2

    def test_metadata_is_dict(self, now):
        """Metadata field stores arbitrary dict data."""
        record = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType(
                {"platform": "langgraph", "endpoint_url": "http://localhost:2024"}
            ),
            created_at=now,
            updated_at=now,
        )
        assert record.metadata["platform"] == "langgraph"

    def test_zone_id_nullable(self, now):
        """zone_id can be None."""
        record = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.zone_id is None

    def test_last_heartbeat_nullable(self, now):
        """last_heartbeat starts as None for a new agent."""
        record = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.UNKNOWN,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.last_heartbeat is None

    def test_last_heartbeat_with_value(self, now):
        """last_heartbeat accepts datetime values."""
        record = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.CONNECTED,
            generation=1,
            last_heartbeat=now,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.last_heartbeat == now
