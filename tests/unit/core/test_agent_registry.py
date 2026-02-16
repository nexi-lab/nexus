"""Unit + integration tests for AgentRegistry (Issue #1240).

Tests cover:
- Registration: new agent, idempotent re-registration, validation
- State transitions: lifecycle, invalid transitions, generation counter semantics
- Optimistic locking: concurrent transitions, stale generation detection
- Heartbeat: in-memory buffer, batch flush, concurrent heartbeats (thread safety)
- Queries: list_by_zone, list_by_owner, detect_stale
- Unregistration: removal, not-found case
- Full lifecycle integration test (Decision #10B)
- Thread-based concurrent heartbeat test (Decision #11A)
"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_record import AgentState
from nexus.core.agent_registry import (
    AgentRegistry,
    InvalidTransitionError,
    StaleAgentError,
)
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """Create in-memory SQLite database for testing (thread-safe)."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def session_factory(engine):
    """Create a session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def registry(session_factory):
    """Create an AgentRegistry for testing."""
    return AgentRegistry(session_factory=session_factory, flush_interval=60)


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    """Tests for agent registration."""

    def test_register_new_agent(self, registry):
        """Registering a new agent returns AgentRecord with UNKNOWN state and generation 0."""
        record = registry.register("agent-1", "alice", zone_id="default", name="Test Agent")
        assert record.agent_id == "agent-1"
        assert record.owner_id == "alice"
        assert record.zone_id == "default"
        assert record.name == "Test Agent"
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0
        assert record.last_heartbeat is None

    def test_register_minimal(self, registry):
        """Registration works with only required fields."""
        record = registry.register("agent-2", "bob")
        assert record.agent_id == "agent-2"
        assert record.owner_id == "bob"
        assert record.zone_id is None
        assert record.name is None

    def test_register_with_metadata(self, registry):
        """Registration preserves metadata."""
        meta = {"platform": "langgraph", "endpoint_url": "http://localhost:2024"}
        record = registry.register("agent-3", "alice", metadata=meta)
        assert record.metadata["platform"] == "langgraph"

    def test_register_idempotent(self, registry):
        """Registering the same agent_id twice returns the existing record."""
        r1 = registry.register("agent-1", "alice")
        r2 = registry.register("agent-1", "alice")
        assert r1.agent_id == r2.agent_id
        assert r1.generation == r2.generation

    def test_register_requires_agent_id(self, registry):
        """Empty agent_id raises ValueError."""
        with pytest.raises(ValueError, match="agent_id"):
            registry.register("", "alice")

    def test_register_requires_owner_id(self, registry):
        """Empty owner_id raises ValueError."""
        with pytest.raises(ValueError, match="owner_id"):
            registry.register("agent-1", "")


# ---------------------------------------------------------------------------
# Get tests
# ---------------------------------------------------------------------------


class TestGet:
    """Tests for getting agent records."""

    def test_get_existing(self, registry):
        """Getting an existing agent returns its record."""
        registry.register("agent-1", "alice")
        record = registry.get("agent-1")
        assert record is not None
        assert record.agent_id == "agent-1"

    def test_get_nonexistent(self, registry):
        """Getting a nonexistent agent returns None."""
        assert registry.get("no-such-agent") is None


# ---------------------------------------------------------------------------
# State transition tests
# ---------------------------------------------------------------------------


class TestStateTransition:
    """Tests for state transitions with generation counter semantics."""

    def test_unknown_to_connected(self, registry):
        """UNKNOWN -> CONNECTED increments generation (new session)."""
        registry.register("agent-1", "alice")
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 1

    def test_connected_to_idle(self, registry):
        """CONNECTED -> IDLE does NOT increment generation."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        record = registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        assert record.state is AgentState.IDLE
        assert record.generation == 1  # No increment

    def test_idle_to_connected(self, registry):
        """IDLE -> CONNECTED increments generation (new session)."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 2  # New session!

    def test_connected_to_suspended(self, registry):
        """CONNECTED -> SUSPENDED does NOT increment generation."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        record = registry.transition("agent-1", AgentState.SUSPENDED, expected_generation=1)
        assert record.state is AgentState.SUSPENDED
        assert record.generation == 1

    def test_suspended_to_connected(self, registry):
        """SUSPENDED -> CONNECTED increments generation (reactivation)."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.transition("agent-1", AgentState.SUSPENDED, expected_generation=1)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 2

    def test_idle_to_suspended(self, registry):
        """IDLE -> SUSPENDED does NOT increment generation."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        record = registry.transition("agent-1", AgentState.SUSPENDED, expected_generation=1)
        assert record.state is AgentState.SUSPENDED
        assert record.generation == 1

    def test_invalid_transition_raises(self, registry):
        """UNKNOWN -> IDLE raises InvalidTransitionError."""
        registry.register("agent-1", "alice")
        with pytest.raises(InvalidTransitionError):
            registry.transition("agent-1", AgentState.IDLE, expected_generation=0)

    def test_transition_nonexistent_agent_raises(self, registry):
        """Transitioning a nonexistent agent raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            registry.transition("no-such", AgentState.CONNECTED, expected_generation=0)

    def test_generation_only_increments_on_new_session(self, registry):
        """Generation increments only on transitions TO CONNECTED (Decision #2A)."""
        registry.register("agent-1", "alice")

        # UNKNOWN -> CONNECTED: gen 0 -> 1
        r = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        assert r.generation == 1

        # CONNECTED -> IDLE: gen stays 1
        r = registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        assert r.generation == 1

        # IDLE -> SUSPENDED: gen stays 1
        r = registry.transition("agent-1", AgentState.SUSPENDED, expected_generation=1)
        assert r.generation == 1

        # SUSPENDED -> CONNECTED: gen 1 -> 2
        r = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)
        assert r.generation == 2


# ---------------------------------------------------------------------------
# Optimistic locking tests (Decision #16B)
# ---------------------------------------------------------------------------


class TestOptimisticLocking:
    """Tests for optimistic locking via generation counter."""

    def test_stale_generation_raises(self, registry):
        """Transition with wrong expected_generation raises StaleAgentError."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        # Try to transition with stale generation (0 instead of 1)
        with pytest.raises(StaleAgentError):
            registry.transition("agent-1", AgentState.IDLE, expected_generation=0)

    def test_concurrent_transitions_one_wins(self, registry):
        """Two transitions with same expected_generation: first wins, second fails."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        # gen=1, state=CONNECTED
        registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        # gen=1, state=IDLE

        # First: IDLE->CONNECTED increments gen to 2
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)

        # Second with stale expected_generation=1 fails (gen is now 2)
        with pytest.raises(StaleAgentError):
            registry.transition("agent-1", AgentState.IDLE, expected_generation=1)

    def test_none_expected_generation_skips_check(self, registry):
        """When expected_generation is None, optimistic locking is skipped."""
        registry.register("agent-1", "alice")
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=None)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 1


# ---------------------------------------------------------------------------
# Heartbeat tests (Decision #13A)
# ---------------------------------------------------------------------------


class TestHeartbeat:
    """Tests for in-memory heartbeat buffer with batch flush."""

    def test_heartbeat_updates_buffer(self, registry):
        """Heartbeat writes to in-memory buffer."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.heartbeat("agent-1")
        # Buffer should contain the agent
        assert "agent-1" in registry._heartbeat_buffer

    def test_flush_heartbeats(self, registry):
        """flush_heartbeats writes buffer to DB and clears it."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.heartbeat("agent-1")
        flushed = registry.flush_heartbeats()
        assert flushed >= 1
        assert len(registry._heartbeat_buffer) == 0

    def test_heartbeat_persists_after_flush(self, registry):
        """After flush, get() returns agent with updated last_heartbeat."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        registry.heartbeat("agent-1")
        registry.flush_heartbeats()
        record = registry.get("agent-1")
        assert record is not None
        assert record.last_heartbeat is not None

    def test_flush_interval_respected(self, session_factory):
        """Heartbeats don't auto-flush before flush_interval."""
        reg = AgentRegistry(session_factory=session_factory, flush_interval=9999)
        reg.register("agent-1", "alice")
        reg.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        reg.heartbeat("agent-1")
        # Buffer should NOT have been auto-flushed
        assert "agent-1" in reg._heartbeat_buffer

    def test_auto_flush_on_interval(self, session_factory):
        """Heartbeats auto-flush when flush_interval elapses."""
        reg = AgentRegistry(session_factory=session_factory, flush_interval=0)
        reg.register("agent-1", "alice")
        reg.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        reg.heartbeat("agent-1")
        # With flush_interval=0, should auto-flush immediately
        assert len(reg._heartbeat_buffer) == 0

    def test_heartbeat_nonexistent_agent(self, registry):
        """Heartbeat for nonexistent agent raises ValueError."""
        with pytest.raises(ValueError, match="not found"):
            registry.heartbeat("no-such-agent")


# ---------------------------------------------------------------------------
# Concurrent heartbeat test (Decision #11A)
# ---------------------------------------------------------------------------


class TestConcurrentHeartbeat:
    """Thread-based concurrent heartbeat test."""

    def test_concurrent_heartbeats_no_corruption(self, registry):
        """10 threads x 100 heartbeats -> no data corruption."""
        registry.register("agent-1", "alice")
        registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)

        errors: list[Exception] = []

        def heartbeat_worker():
            for _ in range(100):
                try:
                    registry.heartbeat("agent-1")
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=heartbeat_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        flushed = registry.flush_heartbeats()
        assert flushed >= 1


# ---------------------------------------------------------------------------
# Full lifecycle integration test (Decision #10B)
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    """Real-time lifecycle integration test."""

    def test_full_lifecycle(self, registry):
        """UNKNOWN -> CONNECTED (gen 0->1) -> IDLE -> CONNECTED (gen 1->2) -> SUSPENDED -> CONNECTED (gen 2->3)."""
        # Register
        record = registry.register("agent-1", "alice", zone_id="default")
        assert record.state is AgentState.UNKNOWN
        assert record.generation == 0

        # First connection (new session)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 1

        # Go idle
        record = registry.transition("agent-1", AgentState.IDLE, expected_generation=1)
        assert record.state is AgentState.IDLE
        assert record.generation == 1  # No increment

        # Reconnect (new session)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=1)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 2  # New session!

        # Suspend
        record = registry.transition("agent-1", AgentState.SUSPENDED, expected_generation=2)
        assert record.state is AgentState.SUSPENDED
        assert record.generation == 2  # No increment

        # Reactivate (new session)
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=2)
        assert record.state is AgentState.CONNECTED
        assert record.generation == 3  # Reactivation!


# ---------------------------------------------------------------------------
# Query tests
# ---------------------------------------------------------------------------


class TestQueries:
    """Tests for list and query operations."""

    def test_list_by_zone(self, registry):
        """list_by_zone returns agents in the specified zone."""
        registry.register("a1", "alice", zone_id="zone-1")
        registry.register("a2", "bob", zone_id="zone-1")
        registry.register("a3", "charlie", zone_id="zone-2")

        agents = registry.list_by_zone("zone-1")
        agent_ids = {a.agent_id for a in agents}
        assert agent_ids == {"a1", "a2"}

    def test_list_by_zone_with_state_filter(self, registry):
        """list_by_zone with state filter returns only matching agents."""
        registry.register("a1", "alice", zone_id="zone-1")
        registry.register("a2", "bob", zone_id="zone-1")
        registry.transition("a1", AgentState.CONNECTED, expected_generation=0)

        connected = registry.list_by_zone("zone-1", state=AgentState.CONNECTED)
        assert len(connected) == 1
        assert connected[0].agent_id == "a1"

    def test_list_by_owner(self, registry):
        """list_by_owner returns agents owned by the specified user."""
        registry.register("a1", "alice")
        registry.register("a2", "alice")
        registry.register("a3", "bob")

        agents = registry.list_by_owner("alice")
        agent_ids = {a.agent_id for a in agents}
        assert agent_ids == {"a1", "a2"}

    def test_list_by_zone_empty(self, registry):
        """list_by_zone returns empty list for zone with no agents."""
        assert registry.list_by_zone("no-zone") == []

    def test_detect_stale(self, registry):
        """detect_stale finds CONNECTED agents with old heartbeats."""
        registry.register("a1", "alice")
        registry.transition("a1", AgentState.CONNECTED, expected_generation=0)
        # Manually set an old heartbeat via flush
        registry.heartbeat("a1")
        registry.flush_heartbeats()

        # With a 0-second threshold, the just-heartbeated agent should be stale
        stale = registry.detect_stale(threshold_seconds=0)
        assert len(stale) >= 1
        assert stale[0].agent_id == "a1"


# ---------------------------------------------------------------------------
# Unregistration tests
# ---------------------------------------------------------------------------


class TestUnregistration:
    """Tests for agent unregistration."""

    def test_unregister_existing(self, registry):
        """Unregistering an existing agent returns True."""
        registry.register("agent-1", "alice")
        assert registry.unregister("agent-1") is True
        assert registry.get("agent-1") is None

    def test_unregister_nonexistent(self, registry):
        """Unregistering a nonexistent agent returns False."""
        assert registry.unregister("no-such") is False


# ---------------------------------------------------------------------------
# Ownership validation tests
# ---------------------------------------------------------------------------


class TestOwnershipValidation:
    """Tests for validate_ownership."""

    def test_valid_ownership(self, registry):
        """validate_ownership returns True for correct owner."""
        registry.register("agent-1", "alice")
        assert registry.validate_ownership("agent-1", "alice") is True

    def test_invalid_ownership(self, registry):
        """validate_ownership returns False for wrong owner."""
        registry.register("agent-1", "alice")
        assert registry.validate_ownership("agent-1", "bob") is False

    def test_nonexistent_agent_ownership(self, registry):
        """validate_ownership returns False for nonexistent agent."""
        assert registry.validate_ownership("no-such", "alice") is False


# ---------------------------------------------------------------------------
# to_dict() tests (Decision #4A)
# ---------------------------------------------------------------------------


class TestToDict:
    """Tests for AgentRecord.to_dict() backward-compat method."""

    def test_basic_keys(self, registry):
        """to_dict() returns all expected keys with correct values."""
        record = registry.register("agent-1", "alice", zone_id="default", name="Test")
        d = record.to_dict()
        assert d["agent_id"] == "agent-1"
        assert d["user_id"] == "alice"  # alias for owner_id
        assert d["name"] == "Test"
        assert d["zone_id"] == "default"
        assert d["state"] == "UNKNOWN"
        assert d["generation"] == 0
        assert isinstance(d["created_at"], str)
        assert isinstance(d["metadata"], dict)

    def test_metadata_is_mutable_copy(self, registry):
        """to_dict() metadata is a mutable dict (not MappingProxyType)."""
        record = registry.register("agent-1", "alice", metadata={"k": "v"})
        d = record.to_dict()
        # Should be a regular dict, not a MappingProxyType
        d["metadata"]["new_key"] = "new_val"
        # Original record metadata should be unaffected (immutable)
        assert "new_key" not in record.metadata

    def test_includes_state_and_generation(self, registry):
        """to_dict() includes state and generation after transitions."""
        registry.register("agent-1", "alice")
        record = registry.transition("agent-1", AgentState.CONNECTED, expected_generation=0)
        d = record.to_dict()
        assert d["state"] == "CONNECTED"
        assert d["generation"] == 1


# ---------------------------------------------------------------------------
# Bridge reliability tests (Decision #8A)
# ---------------------------------------------------------------------------


class TestBridgeReliability:
    """Tests for entity_registry bridge error handling."""

    def test_bridge_success(self, session_factory):
        """Bridge registers in entity_registry on successful register()."""
        from nexus.services.permissions.entity_registry import EntityRegistry

        entity_reg = EntityRegistry(session_factory)
        entity_reg.register_entity("user", "alice")
        reg = AgentRegistry(session_factory=session_factory, entity_registry=entity_reg)

        reg.register("agent-1", "alice", name="Test")
        entity = entity_reg.get_entity("agent", "agent-1")
        assert entity is not None
        assert entity.parent_id == "alice"

    def test_bridge_failure_raises(self, session_factory):
        """Bridge failure raises exception instead of swallowing."""

        class FailingRegistry:
            def register_entity(self, **kwargs):
                raise RuntimeError("DB connection lost")

        reg = AgentRegistry(session_factory=session_factory, entity_registry=FailingRegistry())
        with pytest.raises(RuntimeError, match="DB connection lost"):
            reg.register("agent-1", "alice")

    def test_no_bridge_when_none(self, registry):
        """No bridge call when entity_registry is None (default)."""
        # Should succeed without any bridge call
        record = registry.register("agent-1", "alice")
        assert record.agent_id == "agent-1"

    def test_unregister_bridge_failure_raises(self, session_factory):
        """Unregister bridge failure raises exception."""
        from nexus.services.permissions.entity_registry import EntityRegistry

        entity_reg = EntityRegistry(session_factory)
        entity_reg.register_entity("user", "alice")
        reg = AgentRegistry(session_factory=session_factory, entity_registry=entity_reg)
        reg.register("agent-1", "alice")

        # Now make entity_registry fail on delete
        class FailingDelete:
            def delete_entity(self, *args, **kwargs):
                raise RuntimeError("Delete failed")

        reg._entity_registry = FailingDelete()
        with pytest.raises(RuntimeError, match="Delete failed"):
            reg.unregister("agent-1")


# ---------------------------------------------------------------------------
# Heartbeat capacity warning tests (Decision #15A)
# ---------------------------------------------------------------------------


class TestHeartbeatCapacityWarning:
    """Tests for heartbeat buffer 80% capacity warning."""

    def test_warns_at_80_percent(self, session_factory, caplog):
        """Warning is emitted when heartbeat buffer reaches 80% capacity."""
        import logging

        # Small buffer (max 10) so 80% = 8
        reg = AgentRegistry(
            session_factory=session_factory, flush_interval=9999, max_buffer_size=10
        )
        # Register 9 agents
        for i in range(9):
            reg.register(f"agent-{i}", "alice")
            reg.transition(f"agent-{i}", AgentState.CONNECTED, expected_generation=0)

        # Heartbeat 7 agents (below threshold)
        with caplog.at_level(logging.WARNING, logger="nexus.core.agent_registry"):
            caplog.clear()
            for i in range(7):
                reg.heartbeat(f"agent-{i}")
            assert "capacity" not in caplog.text

        # Heartbeat the 8th agent (hits 80%)
        with caplog.at_level(logging.WARNING, logger="nexus.core.agent_registry"):
            caplog.clear()
            reg.heartbeat("agent-7")
            assert "capacity" in caplog.text
            assert "80%" in caplog.text


# ---------------------------------------------------------------------------
# Migrated from test_agents.py: Registration with bridge (Decision #9A)
# ---------------------------------------------------------------------------


class TestRegistrationWithBridge:
    """Tests for registration with EntityRegistry bridge (migrated from test_agents.py)."""

    def test_entity_registry_creation(self, session_factory):
        """Registration creates entity in EntityRegistry via bridge."""
        from nexus.services.permissions.entity_registry import EntityRegistry

        entity_reg = EntityRegistry(session_factory)
        entity_reg.register_entity("user", "alice")
        reg = AgentRegistry(session_factory=session_factory, entity_registry=entity_reg)

        reg.register("agent_test", "alice", name="Test Agent")

        entity = entity_reg.get_entity("agent", "agent_test")
        assert entity is not None
        assert entity.entity_type == "agent"
        assert entity.entity_id == "agent_test"
        assert entity.parent_type == "user"
        assert entity.parent_id == "alice"

    def test_multi_agent_same_user(self, session_factory):
        """Multiple agents for same user are all tracked."""
        from nexus.services.permissions.entity_registry import EntityRegistry

        entity_reg = EntityRegistry(session_factory)
        entity_reg.register_entity("user", "alice")
        reg = AgentRegistry(session_factory=session_factory, entity_registry=entity_reg)

        reg.register("agent1", "alice", name="Agent 1")
        reg.register("agent2", "alice", name="Agent 2")

        children = entity_reg.get_children("user", "alice")
        assert len(children) == 2
        agent_ids = {c.entity_id for c in children}
        assert agent_ids == {"agent1", "agent2"}

    def test_unregister_preserves_others(self, session_factory):
        """Unregistering one agent doesn't affect others."""
        from nexus.services.permissions.entity_registry import EntityRegistry

        entity_reg = EntityRegistry(session_factory)
        entity_reg.register_entity("user", "alice")
        reg = AgentRegistry(session_factory=session_factory, entity_registry=entity_reg)

        reg.register("agent1", "alice")
        reg.register("agent2", "alice")

        reg.unregister("agent1")

        # agent2 still exists in both registries
        assert reg.get("agent2") is not None
        assert entity_reg.get_entity("agent", "agent2") is not None
        # agent1 is gone from both
        assert reg.get("agent1") is None
        assert entity_reg.get_entity("agent", "agent1") is None


# ---------------------------------------------------------------------------
# Migrated from test_agents.py: Multi-zone isolation
# ---------------------------------------------------------------------------


class TestMultiZoneIsolation:
    """Tests for cross-zone ownership isolation (migrated from test_agents.py)."""

    def test_cross_zone_ownership(self, registry):
        """Agents in different zones have independent ownership."""
        registry.register("agent_acme", "alice", zone_id="acme")
        registry.register("agent_initech", "bob", zone_id="initech")

        assert registry.validate_ownership("agent_acme", "alice") is True
        assert registry.validate_ownership("agent_initech", "bob") is True
        assert registry.validate_ownership("agent_acme", "bob") is False
        assert registry.validate_ownership("agent_initech", "alice") is False

    def test_list_by_zone_isolation(self, registry):
        """list_by_zone only returns agents from the specified zone."""
        registry.register("a1", "alice", zone_id="acme")
        registry.register("a2", "bob", zone_id="initech")

        acme_agents = registry.list_by_zone("acme")
        initech_agents = registry.list_by_zone("initech")

        assert len(acme_agents) == 1
        assert acme_agents[0].agent_id == "a1"
        assert len(initech_agents) == 1
        assert initech_agents[0].agent_id == "a2"


# ---------------------------------------------------------------------------
# Migrated from test_agents.py: Agent lifecycle integration
# ---------------------------------------------------------------------------


class TestAgentLifecycleIntegration:
    """Full register → validate → unregister → verify lifecycle (migrated from test_agents.py)."""

    def test_complete_lifecycle(self, registry):
        """Register → validate ownership → unregister → verify gone."""
        registry.register("agent_lifecycle", "alice", zone_id="default")
        assert registry.validate_ownership("agent_lifecycle", "alice") is True

        registry.unregister("agent_lifecycle")
        assert registry.validate_ownership("agent_lifecycle", "alice") is False
        assert registry.get("agent_lifecycle") is None
