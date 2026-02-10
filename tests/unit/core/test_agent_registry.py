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
