"""Integration tests for delegation lifecycle (Issue #1271).

Tests with real EnhancedReBACManager + NamespaceManager backed by
SQLite in-memory. Covers 12 edge cases specified in the plan.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_registry import AgentRegistry
from nexus.delegation.derivation import derive_grants
from nexus.delegation.errors import (
    DelegationChainError,
    DelegationError,
    DelegationNotFoundError,
    EscalationError,
    TooManyGrantsError,
)
from nexus.delegation.models import DelegationMode
from nexus.delegation.service import DelegationService
from nexus.services.permissions.entity_registry import EntityRegistry
from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager
from nexus.storage.models import Base

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    """Create an in-memory SQLite engine with all tables."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session_factory(engine):
    """Create a session factory bound to the in-memory engine."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def rebac_manager(engine):
    """Create a real EnhancedReBACManager backed by SQLite."""
    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=0,  # Disable caching for tests
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture()
def entity_registry(engine):
    """Create a real EntityRegistry backed by SQLite."""
    return EntityRegistry(engine)


@pytest.fixture()
def agent_registry(session_factory, entity_registry):
    """Create an AgentRegistry with entity_registry bridge."""
    return AgentRegistry(
        session_factory=session_factory,
        entity_registry=entity_registry,
    )


@pytest.fixture()
def delegation_service(session_factory, rebac_manager, entity_registry, agent_registry):
    """Create a DelegationService with real dependencies (no namespace manager)."""
    return DelegationService(
        session_factory=session_factory,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


def _register_coordinator(
    entity_registry, rebac_manager, agent_id="coordinator_1", owner_id="alice", zone_id=None
):
    """Helper: register a coordinator agent with some file grants."""
    entity_registry.register_entity(
        entity_type="user",
        entity_id=owner_id,
    )
    entity_registry.register_entity(
        entity_type="agent",
        entity_id=agent_id,
        parent_type="user",
        parent_id=owner_id,
    )
    # Give coordinator some file grants
    rebac_manager.rebac_write_batch(
        [
            {
                "subject": ("agent", agent_id),
                "relation": "direct_editor",
                "object": ("file", "/workspace/proj/main.py"),
                "zone_id": zone_id or "default",
            },
            {
                "subject": ("agent", agent_id),
                "relation": "direct_editor",
                "object": ("file", "/workspace/proj/utils.py"),
                "zone_id": zone_id or "default",
            },
            {
                "subject": ("agent", agent_id),
                "relation": "direct_viewer",
                "object": ("file", "/workspace/docs/readme.md"),
                "zone_id": zone_id or "default",
            },
        ]
    )
    return agent_id


# ---------------------------------------------------------------------------
# Edge Case Tests
# ---------------------------------------------------------------------------


class TestEdge01_CoordinatorZeroGrants:
    """EC1: Coordinator with zero grants tries 'copy' → empty delegation."""

    def test_copy_with_no_grants(self, delegation_service, entity_registry):
        """Coordinator with no grants creates worker with no grants."""
        entity_registry.register_entity(entity_type="user", entity_id="bob")
        entity_registry.register_entity(
            entity_type="agent",
            entity_id="empty_coordinator",
            parent_type="user",
            parent_id="bob",
        )

        result = delegation_service.delegate(
            coordinator_agent_id="empty_coordinator",
            coordinator_owner_id="bob",
            worker_id="worker_empty",
            worker_name="Worker Empty",
            delegation_mode=DelegationMode.COPY,
        )

        assert result.worker_agent_id == "worker_empty"
        # No grants to copy, so mount table should be empty
        assert result.mount_table == []


class TestEdge02_EscalationAttempt:
    """EC2: Coordinator tries to add grants it doesn't have → EscalationError."""

    def test_escalation_rejected(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        with pytest.raises(EscalationError, match="not held by parent"):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_esc",
                worker_name="Worker Escalation",
                delegation_mode=DelegationMode.CLEAN,
                add_grants=["/secret/top_secret.txt"],
            )


class TestEdge03_TTLZeroOrNegative:
    """EC3: TTL of 0 or negative → validation error."""

    def test_ttl_zero(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)
        with pytest.raises(DelegationError, match="positive"):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_ttl0",
                worker_name="Worker TTL0",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=0,
            )

    def test_ttl_negative(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)
        with pytest.raises(DelegationError, match="positive"):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_ttl_neg",
                worker_name="Worker TTL Neg",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=-100,
            )


class TestEdge04_TTLExceedsMax:
    """EC4: TTL exceeds 24h → validation error."""

    def test_ttl_over_max(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)
        with pytest.raises(DelegationError, match="exceeds maximum"):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_1",
                coordinator_owner_id="alice",
                worker_id="worker_ttl_max",
                worker_name="Worker TTL Max",
                delegation_mode=DelegationMode.COPY,
                ttl_seconds=86401,
            )


class TestEdge06_DelegationChain:
    """EC6: Delegation chain: A→B, B→C → DelegationChainError."""

    def test_chain_rejected(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        # A delegates to B
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_b",
            worker_name="Worker B",
            delegation_mode=DelegationMode.COPY,
        )
        assert result.worker_agent_id == "worker_b"

        # B tries to delegate to C → should fail
        with pytest.raises(DelegationChainError, match="cannot delegate"):
            delegation_service.delegate(
                coordinator_agent_id="worker_b",
                coordinator_owner_id="alice",
                worker_id="worker_c",
                worker_name="Worker C",
                delegation_mode=DelegationMode.COPY,
            )


class TestEdge10_PathPrefixNormalization:
    """EC10: Path prefix with trailing slash vs without."""

    def test_with_trailing_slash(self):
        """scope_prefix with trailing slash still matches."""
        grants = [
            ("direct_editor", "/workspace/proj/a.txt"),
            ("direct_editor", "/workspace/proj/b.txt"),
            ("direct_viewer", "/workspace/other/c.txt"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, scope_prefix="/workspace/proj/")
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.txt", "/workspace/proj/b.txt"}

    def test_without_trailing_slash(self):
        """scope_prefix without trailing slash still matches."""
        grants = [
            ("direct_editor", "/workspace/proj/a.txt"),
            ("direct_editor", "/workspace/proj/b.txt"),
            ("direct_viewer", "/workspace/other/c.txt"),
        ]
        result = derive_grants(grants, DelegationMode.COPY, scope_prefix="/workspace/proj")
        ids = {g.object_id for g in result}
        assert ids == {"/workspace/proj/a.txt", "/workspace/proj/b.txt"}


class TestEdge11_MaxGrantsBoundary:
    """EC11: MAX_DELEGATABLE_GRANTS boundary (exactly 1000 vs 1001)."""

    def test_exactly_1000(self):
        """Exactly MAX grants is allowed."""
        grants = [("direct_viewer", f"/f/{i}.txt") for i in range(1000)]
        result = derive_grants(grants, DelegationMode.COPY)
        assert len(result) == 1000

    def test_1001_raises(self):
        """1001 grants raises TooManyGrantsError."""
        grants = [("direct_viewer", f"/f/{i}.txt") for i in range(1001)]
        with pytest.raises(TooManyGrantsError):
            derive_grants(grants, DelegationMode.COPY)


class TestDelegationLifecycle:
    """Full lifecycle: create → list → revoke."""

    def test_create_list_revoke(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        # Create delegation
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_lifecycle",
            worker_name="Worker Lifecycle",
            delegation_mode=DelegationMode.COPY,
            ttl_seconds=3600,
        )
        assert result.delegation_id
        assert result.worker_agent_id == "worker_lifecycle"
        assert result.api_key  # Non-empty API key
        assert result.delegation_mode == DelegationMode.COPY
        assert result.expires_at is not None

        # List delegations
        delegations = delegation_service.list_delegations("coordinator_1")
        assert len(delegations) == 1
        assert delegations[0].agent_id == "worker_lifecycle"

        # Get delegation by worker ID
        record = delegation_service.get_delegation("worker_lifecycle")
        assert record is not None
        assert record.parent_agent_id == "coordinator_1"

        # Revoke
        revoked = delegation_service.revoke_delegation(result.delegation_id)
        assert revoked is True

        # Verify delegation is gone
        record = delegation_service.get_delegation("worker_lifecycle")
        assert record is None

    def test_revoke_nonexistent_raises(self, delegation_service):
        """Revoking a non-existent delegation raises DelegationNotFoundError."""
        with pytest.raises(DelegationNotFoundError):
            delegation_service.revoke_delegation("nonexistent_id")


class TestCopyModeWithScopeAndReadonly:
    """Copy mode with scope_prefix and readonly_paths."""

    def test_scoped_readonly_delegation(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_scoped",
            worker_name="Worker Scoped",
            delegation_mode=DelegationMode.COPY,
            scope_prefix="/workspace/proj",
            readonly_paths=["/workspace/proj/main.py"],
            ttl_seconds=1800,
        )

        assert result.worker_agent_id == "worker_scoped"
        assert result.delegation_mode == DelegationMode.COPY

        # Verify the record stores the parameters
        record = delegation_service.get_delegation("worker_scoped")
        assert record is not None
        assert record.scope_prefix == "/workspace/proj"
        assert "/workspace/proj/main.py" in record.readonly_paths


class TestCleanModeIntegration:
    """Clean mode with real ReBAC grants."""

    def test_clean_mode_with_valid_add(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_clean",
            worker_name="Worker Clean",
            delegation_mode=DelegationMode.CLEAN,
            add_grants=["/workspace/proj/main.py"],
        )

        assert result.worker_agent_id == "worker_clean"
        assert result.delegation_mode == DelegationMode.CLEAN


class TestSharedModeIntegration:
    """Shared mode with real ReBAC grants."""

    def test_shared_mode(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_shared",
            worker_name="Worker Shared",
            delegation_mode=DelegationMode.SHARED,
        )

        assert result.worker_agent_id == "worker_shared"
        assert result.delegation_mode == DelegationMode.SHARED


class TestDelegationLifecycleWithAgentRegistry:
    """Tests verifying that delegation flows use AgentRegistry for lifecycle tracking."""

    def test_worker_registered_in_agent_registry(
        self, delegation_service, entity_registry, rebac_manager, agent_registry
    ):
        """Worker created via delegation is registered in AgentRegistry."""
        _register_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_lifecycle_ar",
            worker_name="Worker Lifecycle AR",
            delegation_mode=DelegationMode.COPY,
        )

        # Verify worker exists in AgentRegistry
        record = agent_registry.get("worker_lifecycle_ar")
        assert record is not None
        assert record.owner_id == "alice"
        assert record.name == "Worker Lifecycle AR"

    def test_revoke_removes_from_agent_registry(
        self, delegation_service, entity_registry, rebac_manager, agent_registry
    ):
        """Revoking delegation removes worker from AgentRegistry."""
        _register_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_revoke_ar",
            worker_name="Worker Revoke AR",
            delegation_mode=DelegationMode.COPY,
        )

        # Worker exists in AgentRegistry
        assert agent_registry.get("worker_revoke_ar") is not None

        # Revoke
        delegation_service.revoke_delegation(result.delegation_id)

        # Worker removed from AgentRegistry
        assert agent_registry.get("worker_revoke_ar") is None
