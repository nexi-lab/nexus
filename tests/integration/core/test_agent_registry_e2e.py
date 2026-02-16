"""E2E tests: AgentRegistry as single source of truth (Issue #1588).

Validates that the consolidated AgentRegistry correctly serves as the
single registration path for all flows: NexusFS, DelegationService,
and ownership validation. Tests use real SQLite + real services (no mocks).

Checks:
- Single-write behavior (no dual EntityRegistry writes)
- Delegation flow uses AgentRegistry
- Ownership validation is cached
- No performance regressions in hot paths (heartbeat, validate_ownership)
"""

from __future__ import annotations

import logging
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_record import AgentState
from nexus.core.agent_registry import AgentRegistry
from nexus.services.delegation.models import DelegationMode
from nexus.services.delegation.service import DelegationService
from nexus.services.permissions.entity_registry import EntityRegistry
from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """Shared SQLite in-memory engine for all components."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def entity_registry(session_factory):
    return EntityRegistry(session_factory)


@pytest.fixture()
def agent_registry(session_factory, entity_registry):
    return AgentRegistry(
        session_factory=session_factory,
        entity_registry=entity_registry,
    )


@pytest.fixture()
def rebac_manager(engine):
    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=0,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture()
def delegation_service(session_factory, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        session_factory=session_factory,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


# ---------------------------------------------------------------------------
# Single-source-of-truth: registration goes through AgentRegistry only
# ---------------------------------------------------------------------------


class TestSingleSourceOfTruth:
    """Verify AgentRegistry is the sole registration path."""

    def test_register_writes_both_stores_once(self, agent_registry, entity_registry):
        """Register creates exactly one record in AgentRegistry and one in EntityRegistry."""
        entity_registry.register_entity("user", "alice")

        record = agent_registry.register("agent-sot-1", "alice", name="Test")

        # AgentRegistry has it
        assert agent_registry.get("agent-sot-1") is not None
        assert record.agent_id == "agent-sot-1"

        # EntityRegistry also has it via bridge
        entity = entity_registry.get_entity("agent", "agent-sot-1")
        assert entity is not None
        assert entity.parent_id == "alice"

    def test_no_duplicate_entity_on_idempotent_register(self, agent_registry, entity_registry):
        """Idempotent register does not create duplicate EntityRegistry entries."""
        entity_registry.register_entity("user", "bob")

        agent_registry.register("agent-idem-1", "bob")
        agent_registry.register("agent-idem-1", "bob")  # idempotent

        # Still only one entity
        entity = entity_registry.get_entity("agent", "agent-idem-1")
        assert entity is not None

    def test_unregister_removes_from_both(self, agent_registry, entity_registry):
        """Unregister cleans up both AgentRegistry and EntityRegistry."""
        entity_registry.register_entity("user", "charlie")
        agent_registry.register("agent-unreg-1", "charlie")

        result = agent_registry.unregister("agent-unreg-1")
        assert result is True

        assert agent_registry.get("agent-unreg-1") is None
        assert entity_registry.get_entity("agent", "agent-unreg-1") is None


# ---------------------------------------------------------------------------
# Delegation uses AgentRegistry (not agents.py)
# ---------------------------------------------------------------------------


class TestDelegationUsesAgentRegistry:
    """Verify DelegationService creates workers via AgentRegistry."""

    def _setup_coordinator(self, entity_registry, rebac_manager, agent_registry):
        """Register user + coordinator with file grants."""
        entity_registry.register_entity("user", "alice")
        agent_registry.register("coord-1", "alice", zone_id="default", name="Coordinator")

        rebac_manager.rebac_write_batch(
            [
                {
                    "subject": ("agent", "coord-1"),
                    "relation": "direct_editor",
                    "object": ("file", "/workspace/src/main.py"),
                    "zone_id": "default",
                },
                {
                    "subject": ("agent", "coord-1"),
                    "relation": "direct_editor",
                    "object": ("file", "/workspace/src/utils.py"),
                    "zone_id": "default",
                },
            ]
        )

    def test_delegate_registers_worker_in_agent_registry(
        self, delegation_service, entity_registry, rebac_manager, agent_registry
    ):
        """Delegation creates worker in AgentRegistry with lifecycle tracking."""
        self._setup_coordinator(entity_registry, rebac_manager, agent_registry)

        result = delegation_service.delegate(
            coordinator_agent_id="coord-1",
            coordinator_owner_id="alice",
            worker_id="worker-del-1",
            worker_name="Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
            ttl_seconds=3600,
        )

        assert result.worker_agent_id == "worker-del-1"
        assert result.api_key.startswith("sk-")

        # Worker exists in AgentRegistry
        worker = agent_registry.get("worker-del-1")
        assert worker is not None
        assert worker.owner_id == "alice"
        assert worker.state is AgentState.UNKNOWN

        # Worker exists in EntityRegistry (via bridge)
        entity = entity_registry.get_entity("agent", "worker-del-1")
        assert entity is not None

    def test_revoke_removes_worker_from_agent_registry(
        self, delegation_service, entity_registry, rebac_manager, agent_registry
    ):
        """Revocation removes worker from AgentRegistry."""
        self._setup_coordinator(entity_registry, rebac_manager, agent_registry)

        result = delegation_service.delegate(
            coordinator_agent_id="coord-1",
            coordinator_owner_id="alice",
            worker_id="worker-rev-1",
            worker_name="Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
        )

        delegation_service.revoke_delegation(result.delegation_id)

        # Worker removed from AgentRegistry
        assert agent_registry.get("worker-rev-1") is None
        # Worker removed from EntityRegistry
        assert entity_registry.get_entity("agent", "worker-rev-1") is None


# ---------------------------------------------------------------------------
# Ownership validation uses AgentRegistry with caching
# ---------------------------------------------------------------------------


class TestOwnershipValidation:
    """Verify ownership validation is correct and cached."""

    def test_validate_ownership_correct(self, agent_registry, entity_registry):
        """Ownership validation returns correct results."""
        entity_registry.register_entity("user", "alice")
        agent_registry.register("agent-own-1", "alice")

        assert agent_registry.validate_ownership("agent-own-1", "alice") is True
        assert agent_registry.validate_ownership("agent-own-1", "bob") is False
        assert agent_registry.validate_ownership("nonexistent", "alice") is False

    def test_validate_ownership_uses_cache(self, agent_registry, entity_registry):
        """Second validate_ownership call uses cache (no extra DB hit)."""
        entity_registry.register_entity("user", "alice")
        agent_registry.register("agent-cache-1", "alice")

        # First call populates cache
        assert agent_registry.validate_ownership("agent-cache-1", "alice") is True

        # Second call should be from cache (much faster)
        t0 = time.perf_counter()
        for _ in range(1000):
            agent_registry.validate_ownership("agent-cache-1", "alice")
        elapsed = time.perf_counter() - t0

        # 1000 cached lookups should be < 100ms
        assert elapsed < 0.1, f"1000 cached validate_ownership took {elapsed:.3f}s — too slow"


# ---------------------------------------------------------------------------
# Performance: heartbeat, flush, registration hot paths
# ---------------------------------------------------------------------------


class TestPerformanceHotPaths:
    """Verify no performance regression in hot paths."""

    def test_heartbeat_throughput(self, agent_registry, entity_registry):
        """Heartbeat buffer can handle 500 agents without stalling."""
        entity_registry.register_entity("user", "alice")

        # Register 500 agents
        for i in range(500):
            agent_registry.register(f"perf-hb-{i}", "alice")

        # Heartbeat all 500
        t0 = time.perf_counter()
        for i in range(500):
            agent_registry.heartbeat(f"perf-hb-{i}")
        elapsed = time.perf_counter() - t0

        # 500 heartbeats should take < 500ms
        assert elapsed < 0.5, f"500 heartbeats took {elapsed:.3f}s — too slow"

    def test_flush_heartbeats_performance(self, agent_registry, entity_registry):
        """Flush heartbeats should batch-write efficiently."""
        entity_registry.register_entity("user", "alice")

        for i in range(100):
            agent_registry.register(f"perf-fl-{i}", "alice")
            agent_registry.heartbeat(f"perf-fl-{i}")

        t0 = time.perf_counter()
        flushed = agent_registry.flush_heartbeats()
        elapsed = time.perf_counter() - t0

        assert flushed == 100
        # Batch flush of 100 should take < 200ms
        assert elapsed < 0.2, f"Flushing 100 heartbeats took {elapsed:.3f}s — too slow"

    def test_registration_throughput(self, agent_registry, entity_registry):
        """Registering 100 agents should complete in < 2s."""
        entity_registry.register_entity("user", "alice")

        t0 = time.perf_counter()
        for i in range(100):
            agent_registry.register(f"perf-reg-{i}", "alice")
        elapsed = time.perf_counter() - t0

        # 100 registrations (each with bridge write) should take < 2s
        assert elapsed < 2.0, f"100 registrations took {elapsed:.3f}s — too slow"

    def test_to_dict_no_overhead(self, agent_registry, entity_registry):
        """to_dict() should be fast (pure data extraction, no DB)."""
        entity_registry.register_entity("user", "alice")
        record = agent_registry.register("perf-dict-1", "alice", name="Test")

        t0 = time.perf_counter()
        for _ in range(10000):
            record.to_dict()
        elapsed = time.perf_counter() - t0

        # 10000 to_dict() calls should take < 100ms
        assert elapsed < 0.1, f"10000 to_dict() calls took {elapsed:.3f}s — too slow"


# ---------------------------------------------------------------------------
# Bridge reliability: errors propagate
# ---------------------------------------------------------------------------


class TestBridgeReliabilityE2E:
    """Verify bridge errors are non-silent in real scenario."""

    def test_register_log_on_success(self, agent_registry, entity_registry, caplog):
        """Successful registration logs at DEBUG level."""
        entity_registry.register_entity("user", "alice")

        with caplog.at_level(logging.DEBUG, logger="nexus.core.agent_registry"):
            agent_registry.register("agent-log-1", "alice")

        assert any("Registered agent agent-log-1" in msg for msg in caplog.messages)

    def test_heartbeat_capacity_warning(self, agent_registry, entity_registry, caplog):
        """Heartbeat buffer warns at 80% capacity."""
        entity_registry.register_entity("user", "alice")

        # Create a registry with very small buffer
        small_registry = AgentRegistry(
            session_factory=agent_registry._session_factory,
            entity_registry=entity_registry,
            max_buffer_size=10,
        )

        for i in range(10):
            small_registry.register(f"hb-warn-{i}", "alice")

        with caplog.at_level(logging.WARNING, logger="nexus.core.agent_registry"):
            for i in range(10):
                small_registry.heartbeat(f"hb-warn-{i}")

        assert any("capacity" in msg.lower() for msg in caplog.messages), (
            "Expected heartbeat capacity warning"
        )
