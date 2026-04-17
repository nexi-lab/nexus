"""Unit tests for AgentRecord with unified AgentState (Issue #1240, #1800).

Tests cover:
- AgentState: unified enum from process_types (REGISTERED, WARMING_UP, READY, BUSY, SUSPENDED, TERMINATED)
- AgentRecord: frozen dataclass immutability, defaults, field access
- AgentRecord.state uses the unified AgentState (no more old UNKNOWN/CONNECTED/IDLE)

Note: The old AgentState (UNKNOWN, CONNECTED, IDLE, SUSPENDED), VALID_TRANSITIONS,
validate_transition(), and is_new_session() have been deleted (Issue #1800).
State machine validation is handled by the kernel AgentRegistry (VALID_AGENT_TRANSITIONS).
"""

import types
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from nexus.contracts.agent_types import AgentRecord
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.process_types import AgentState

# ---------------------------------------------------------------------------
# AgentState — unified from process_types
# ---------------------------------------------------------------------------


class TestAgentState:
    """Tests for the unified AgentState enum (from process_types)."""

    def test_has_six_states(self):
        """Unified AgentState enum must have exactly 6 members."""
        assert len(AgentState) == 6

    def test_state_values(self):
        """Each state has the expected string value."""
        assert AgentState.REGISTERED.value == "registered"
        assert AgentState.WARMING_UP.value == "warming_up"
        assert AgentState.READY.value == "ready"
        assert AgentState.BUSY.value == "busy"
        assert AgentState.SUSPENDED.value == "suspended"
        assert AgentState.TERMINATED.value == "terminated"

    def test_states_are_distinct(self):
        """All states are distinct enum members."""
        states = list(AgentState)
        assert len(states) == len(set(states))

    def test_from_string(self):
        """AgentState can be constructed from string value."""
        assert AgentState("registered") is AgentState.REGISTERED
        assert AgentState("ready") is AgentState.READY
        assert AgentState("busy") is AgentState.BUSY
        assert AgentState("suspended") is AgentState.SUSPENDED

    def test_invalid_string_raises(self):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            AgentState("INVALID")


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
            zone_id=ROOT_ZONE_ID,
            name="Test Agent",
            state=AgentState.REGISTERED,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )

    def test_is_frozen(self, record):
        """AgentRecord is immutable (frozen dataclass).

        Tests str field (agent_id) because Enum field assignment
        may not raise FrozenInstanceError on Python 3.13+ (cpython#118033).
        """
        with pytest.raises(FrozenInstanceError):
            record.agent_id = "changed"

    def test_field_access(self, record):
        """All fields are accessible."""
        assert record.agent_id == "agent-1"
        assert record.owner_id == "alice"
        assert record.zone_id == ROOT_ZONE_ID
        assert record.name == "Test Agent"
        assert record.state is AgentState.REGISTERED
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
            state=AgentState.REGISTERED,
            generation=0,
            last_heartbeat=None,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.state is AgentState.REGISTERED
        assert record.generation == 0

    def test_equality(self, now):
        """Two records with same fields are equal."""
        r1 = AgentRecord(
            agent_id="a",
            owner_id="u",
            zone_id=None,
            name=None,
            state=AgentState.REGISTERED,
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
            state=AgentState.REGISTERED,
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
            state=AgentState.REGISTERED,
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
            state=AgentState.BUSY,
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
            state=AgentState.REGISTERED,
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
            state=AgentState.REGISTERED,
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
            state=AgentState.REGISTERED,
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
            state=AgentState.BUSY,
            generation=1,
            last_heartbeat=now,
            metadata=types.MappingProxyType({}),
            created_at=now,
            updated_at=now,
        )
        assert record.last_heartbeat == now
