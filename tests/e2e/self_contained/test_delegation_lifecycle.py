"""Integration tests for delegation lifecycle (Issue #1271, #1618).

Tests with real EnhancedReBACManager + NamespaceManager backed by
SQLite in-memory. Covers edge cases specified in the plan.
"""

import pytest

from nexus.bricks.delegation.derivation import derive_grants
from nexus.bricks.delegation.errors import (
    DelegationChainError,
    DelegationError,
    DelegationNotFoundError,
    EscalationError,
    TooManyGrantsError,
)
from nexus.bricks.delegation.models import DelegationMode, DelegationStatus
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.system_services.agents.agent_registry import AgentRegistry
from tests.helpers.in_memory_record_store import InMemoryRecordStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    """Shared in-memory RecordStore for all components."""
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def engine(record_store):
    """SQLite in-memory engine from RecordStore."""
    return record_store.engine


@pytest.fixture()
def session_factory(record_store):
    """Session factory from RecordStore."""
    return record_store.session_factory


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
def entity_registry(record_store):
    """Create a real EntityRegistry backed by SQLite."""
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store):
    """Create a real AgentRegistry backed by SQLite."""
    return AgentRegistry(record_store=record_store)


@pytest.fixture()
def delegation_service(record_store, rebac_manager, entity_registry, agent_registry):
    """Create a DelegationService with real dependencies (no namespace manager)."""
    return DelegationService(
        record_store=record_store,
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
                "zone_id": zone_id or "root",
            },
            {
                "subject": ("agent", agent_id),
                "relation": "direct_editor",
                "object": ("file", "/workspace/proj/utils.py"),
                "zone_id": zone_id or "root",
            },
            {
                "subject": ("agent", agent_id),
                "relation": "direct_viewer",
                "object": ("file", "/workspace/docs/readme.md"),
                "zone_id": zone_id or "root",
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
    """EC6: Delegation chain tests."""

    def test_chain_rejected_without_sub_delegate(
        self, delegation_service, entity_registry, rebac_manager
    ):
        """B→C blocked when B has can_sub_delegate=False (default)."""
        _register_coordinator(entity_registry, rebac_manager)

        # A delegates to B (can_sub_delegate=False by default)
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_b",
            worker_name="Worker B",
            delegation_mode=DelegationMode.COPY,
        )
        assert result.worker_agent_id == "worker_b"

        # Register worker_b in entity_registry so it can be found as a coordinator
        entity_registry.register_entity(
            entity_type="agent",
            entity_id="worker_b",
            parent_type="user",
            parent_id="alice",
        )

        # B tries to delegate to C → should fail
        with pytest.raises(DelegationChainError, match="cannot sub-delegate"):
            delegation_service.delegate(
                coordinator_agent_id="worker_b",
                coordinator_owner_id="alice",
                worker_id="worker_c",
                worker_name="Worker C",
                delegation_mode=DelegationMode.COPY,
            )

    def test_chain_allowed_with_sub_delegate(
        self, delegation_service, entity_registry, rebac_manager
    ):
        """B→C allowed when B has can_sub_delegate=True (#1618)."""
        _register_coordinator(entity_registry, rebac_manager)

        # A delegates to B with can_sub_delegate=True
        result_b = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_chain_b",
            worker_name="Worker Chain B",
            delegation_mode=DelegationMode.COPY,
            can_sub_delegate=True,
        )
        assert result_b.worker_agent_id == "worker_chain_b"

        # Register worker_chain_b in entity_registry so it can be found as a coordinator
        entity_registry.register_entity(
            entity_type="agent",
            entity_id="worker_chain_b",
            parent_type="user",
            parent_id="alice",
        )

        # B delegates to C → should succeed
        result_c = delegation_service.delegate(
            coordinator_agent_id="worker_chain_b",
            coordinator_owner_id="alice",
            worker_id="worker_chain_c",
            worker_name="Worker Chain C",
            delegation_mode=DelegationMode.COPY,
        )
        assert result_c.worker_agent_id == "worker_chain_c"

        # Verify depth tracking
        record_c = delegation_service.get_delegation("worker_chain_c")
        assert record_c is not None
        assert record_c.depth == 1
        assert record_c.parent_delegation_id == result_b.delegation_id


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

        # List delegations (#1618: returns tuple)
        delegations, total = delegation_service.list_delegations("coordinator_1")
        assert total == 1
        assert len(delegations) == 1
        assert delegations[0].agent_id == "worker_lifecycle"
        assert delegations[0].status == DelegationStatus.ACTIVE

        # Get delegation by worker ID
        record = delegation_service.get_delegation("worker_lifecycle")
        assert record is not None
        assert record.parent_agent_id == "coordinator_1"

        # Revoke (soft-delete-first, #1618)
        revoked = delegation_service.revoke_delegation(result.delegation_id)
        assert revoked is True

        # get_delegation returns None (filters by ACTIVE only)
        record = delegation_service.get_delegation("worker_lifecycle")
        assert record is None

        # But get_delegation_by_id still shows it with REVOKED status
        record_by_id = delegation_service.get_delegation_by_id(result.delegation_id)
        assert record_by_id is not None
        assert record_by_id.status == DelegationStatus.REVOKED

    def test_revoke_nonexistent_raises(self, delegation_service):
        """Revoking a non-existent delegation raises DelegationNotFoundError."""
        with pytest.raises(DelegationNotFoundError):
            delegation_service.revoke_delegation("nonexistent_id")

    def test_revoke_already_revoked_raises(
        self, delegation_service, entity_registry, rebac_manager
    ):
        """Revoking an already-revoked delegation raises DelegationError."""
        _register_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_double_revoke",
            worker_name="Worker Double Revoke",
            delegation_mode=DelegationMode.COPY,
        )

        delegation_service.revoke_delegation(result.delegation_id)

        # Second revoke should fail
        with pytest.raises(DelegationError, match="not active"):
            delegation_service.revoke_delegation(result.delegation_id)


class TestDelegationIntent:
    """#1618: Intent tracking on delegations."""

    def test_intent_stored(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_intent",
            worker_name="Worker Intent",
            delegation_mode=DelegationMode.COPY,
            intent="Run unit tests on project",
        )

        record = delegation_service.get_delegation("worker_intent")
        assert record is not None
        assert record.intent == "Run unit tests on project"

    def test_intent_defaults_empty(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="worker_no_intent",
            worker_name="Worker No Intent",
            delegation_mode=DelegationMode.COPY,
        )

        record = delegation_service.get_delegation("worker_no_intent")
        assert record is not None
        assert record.intent == ""


class TestDelegationChainTrace:
    """#1618: get_delegation_chain traces from child to root."""

    def test_chain_trace(self, delegation_service, entity_registry, rebac_manager):
        _register_coordinator(entity_registry, rebac_manager)

        # A → B (can sub-delegate) → C
        result_b = delegation_service.delegate(
            coordinator_agent_id="coordinator_1",
            coordinator_owner_id="alice",
            worker_id="chain_worker_b",
            worker_name="Chain B",
            delegation_mode=DelegationMode.COPY,
            can_sub_delegate=True,
        )

        # Register chain_worker_b in entity_registry so it can be found as a coordinator
        entity_registry.register_entity(
            entity_type="agent",
            entity_id="chain_worker_b",
            parent_type="user",
            parent_id="alice",
        )

        result_c = delegation_service.delegate(
            coordinator_agent_id="chain_worker_b",
            coordinator_owner_id="alice",
            worker_id="chain_worker_c",
            worker_name="Chain C",
            delegation_mode=DelegationMode.COPY,
        )

        # Trace from C to root
        chain = delegation_service.get_delegation_chain(result_c.delegation_id)
        assert len(chain) == 2
        assert chain[0].delegation_id == result_c.delegation_id  # C first
        assert chain[1].delegation_id == result_b.delegation_id  # B second
        assert chain[0].depth == 1
        assert chain[1].depth == 0


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
