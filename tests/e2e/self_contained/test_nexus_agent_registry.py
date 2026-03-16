"""Integration tests for AgentRegistry with EntityRegistry bridge (Issue #1588).

Tests that register/unregister flows write to both AgentRegistry (DB) and
EntityRegistry (bridge) consistently. Validates the single-source-of-truth
consolidation after deleting agents.py.
"""

import pytest

from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.contracts.agent_types import AgentState
from nexus.system_services.agents.agent_registry import AgentRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore


@pytest.fixture()
def record_store():
    """Shared in-memory RecordStore for all components."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def session_factory(record_store):
    """Create a session factory."""
    return record_store.session_factory


@pytest.fixture()
def entity_registry(record_store):
    """Create EntityRegistry backed by SQLite."""
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store, entity_registry):
    """Create AgentRegistry with entity_registry bridge."""
    return AgentRegistry(
        record_store=record_store,
        entity_registry=entity_registry,
    )


class TestRegisterWritesBothStores:
    """Registration writes to both AgentRegistry and EntityRegistry."""

    def test_register_creates_in_both(self, agent_registry, entity_registry):
        """Register writes agent to AgentRegistry DB and EntityRegistry bridge."""
        entity_registry.register_entity("user", "alice")

        record = agent_registry.register("agent-1", "alice", name="Test")

        # AgentRegistry has it
        assert record.agent_id == "agent-1"
        assert record.owner_id == "alice"
        assert agent_registry.get("agent-1") is not None

        # EntityRegistry also has it (bridge)
        entity = entity_registry.get_entity("agent", "agent-1")
        assert entity is not None
        assert entity.parent_id == "alice"


class TestUnregisterRemovesBothStores:
    """Unregistration removes from both AgentRegistry and EntityRegistry."""

    def test_unregister_removes_from_both(self, agent_registry, entity_registry):
        """Unregister removes from AgentRegistry and EntityRegistry bridge."""
        entity_registry.register_entity("user", "alice")
        agent_registry.register("agent-1", "alice")

        result = agent_registry.unregister("agent-1")
        assert result is True

        # Gone from AgentRegistry
        assert agent_registry.get("agent-1") is None

        # Gone from EntityRegistry (bridge)
        assert entity_registry.get_entity("agent", "agent-1") is None


class TestFullLifecycleIntegration:
    """Full lifecycle: register → transition → heartbeat → unregister."""

    def test_full_lifecycle(self, agent_registry, entity_registry):
        """Register → connect → heartbeat → idle → unregister → verify gone."""
        entity_registry.register_entity("user", "alice")

        # Register
        record = agent_registry.register("agent-lc", "alice", zone_id="root", name="LC Agent")
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0

        # Connect (new session)
        record = agent_registry.transition("agent-lc", AgentState.CONNECTED, expected_generation=0)
        assert record.generation == 1

        # Heartbeat
        agent_registry.heartbeat("agent-lc")
        flushed = agent_registry.flush_heartbeats()
        assert flushed >= 1

        # Idle
        record = agent_registry.transition("agent-lc", AgentState.IDLE, expected_generation=1)
        assert record.state is AgentState.IDLE

        # Validate ownership
        assert agent_registry.validate_ownership("agent-lc", "alice") is True
        assert agent_registry.validate_ownership("agent-lc", "bob") is False

        # to_dict() backward compat
        d = record.to_dict()
        assert d["user_id"] == "alice"
        assert d["agent_id"] == "agent-lc"

        # Unregister
        assert agent_registry.unregister("agent-lc") is True
        assert agent_registry.get("agent-lc") is None
        assert entity_registry.get_entity("agent", "agent-lc") is None
