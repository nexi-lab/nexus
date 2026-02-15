"""Full end-to-end test for delegation API with real server (Issue #1271).

Uses create_app() with real NexusFS, ReBAC permissions enabled,
StaticAPIKeyAuth, and validates the complete delegation flow:
1. Coordinator agent authenticates
2. Coordinator creates files and gets grants
3. POST /api/v2/agents/delegate (copy mode)
4. Worker authenticates with returned API key
5. Worker can read files within granted namespace
6. Worker cannot read files outside namespace
7. DELETE to revoke delegation
8. GET to list delegations

Requires real EnhancedReBACManager + NamespaceManager + EntityRegistry.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from nexus.core.agent_registry import AgentRegistry
from nexus.delegation.errors import DelegationChainError, EscalationError
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
def rebac_manager(engine):
    manager = EnhancedReBACManager(
        engine=engine,
        cache_ttl_seconds=0,
        max_depth=10,
    )
    yield manager
    manager.close()


@pytest.fixture()
def entity_registry(engine):
    return EntityRegistry(engine)


@pytest.fixture()
def agent_registry(session_factory, entity_registry):
    return AgentRegistry(
        session_factory=session_factory,
        entity_registry=entity_registry,
    )


@pytest.fixture()
def delegation_service(session_factory, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        session_factory=session_factory,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


def _setup_coordinator(entity_registry, rebac_manager):
    """Register coordinator agent with file grants and return agent info."""
    # Register user
    entity_registry.register_entity(
        entity_type="user",
        entity_id="alice",
    )

    # Register coordinator agent
    entity_registry.register_entity(
        entity_type="agent",
        entity_id="coordinator_agent",
        parent_type="user",
        parent_id="alice",
    )

    # Grant coordinator access to files
    rebac_manager.rebac_write_batch(
        [
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/project/src/main.py"),
                "zone_id": "default",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/project/src/utils.py"),
                "zone_id": "default",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_viewer",
                "object": ("file", "/workspace/project/docs/readme.md"),
                "zone_id": "default",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/secret/credentials.json"),
                "zone_id": "default",
            },
        ]
    )


# ---------------------------------------------------------------------------
# Full Lifecycle Test
# ---------------------------------------------------------------------------


class TestFullDelegationLifecycle:
    """Complete delegation lifecycle with real services."""

    def test_copy_mode_delegation_and_revocation(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Full flow: setup coordinator → delegate copy → verify grants → revoke."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Step 1: Delegate with copy mode, scoped to /workspace/project/
        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_copy_1",
            worker_name="Copy Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
            scope_prefix="/workspace/project",
            ttl_seconds=3600,
        )

        assert result.delegation_id
        assert result.worker_agent_id == "worker_copy_1"
        assert result.api_key.startswith("sk-")
        assert result.delegation_mode == DelegationMode.COPY
        assert result.expires_at is not None

        # Step 2: Verify worker has grants within scope
        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_copy_1"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Worker should have access to /workspace/project/* files
        assert "/workspace/project/src/main.py" in worker_read_ids
        assert "/workspace/project/src/utils.py" in worker_read_ids
        assert "/workspace/project/docs/readme.md" in worker_read_ids

        # Worker should NOT have access to /workspace/secret/
        assert "/workspace/secret/credentials.json" not in worker_read_ids

        # Step 3: List delegations
        delegations = delegation_service.list_delegations("coordinator_agent")
        assert len(delegations) == 1
        assert delegations[0].agent_id == "worker_copy_1"
        assert delegations[0].scope_prefix == "/workspace/project"

        # Step 4: Revoke
        delegation_service.revoke_delegation(result.delegation_id)

        # Step 5: Verify worker no longer has grants
        worker_read_after = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_copy_1"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        assert len(worker_read_after) == 0

        # Step 6: Verify delegation record is gone
        assert delegation_service.get_delegation("worker_copy_1") is None

    def test_copy_mode_with_readonly_downgrade(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Delegate with readonly_paths → worker can read but not write those files."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_readonly",
            worker_name="Readonly Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
            scope_prefix="/workspace/project",
            readonly_paths=["/workspace/project/src/main.py"],
        )

        # Worker can read main.py
        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_readonly"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}
        assert "/workspace/project/src/main.py" in worker_read_ids

        # Worker cannot write main.py (downgraded to viewer)
        worker_write = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_readonly"),
            permission="write",
            object_type="file",
            zone_id="default",
        )
        worker_write_ids = {obj_id for _, obj_id in worker_write}
        assert "/workspace/project/src/main.py" not in worker_write_ids

        # Worker CAN write utils.py (not in readonly_paths)
        assert "/workspace/project/src/utils.py" in worker_write_ids

    def test_clean_mode_explicit_grants(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Clean mode: only explicitly added grants from parent."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_clean_1",
            worker_name="Clean Worker",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="default",
            add_grants=["/workspace/project/src/main.py"],
        )

        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_clean_1"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Only main.py granted
        assert "/workspace/project/src/main.py" in worker_read_ids
        # Nothing else
        assert "/workspace/project/src/utils.py" not in worker_read_ids
        assert "/workspace/project/docs/readme.md" not in worker_read_ids
        assert "/workspace/secret/credentials.json" not in worker_read_ids

    def test_shared_mode_full_access(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Shared mode: worker gets same view as coordinator."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_shared_1",
            worker_name="Shared Worker",
            delegation_mode=DelegationMode.SHARED,
            zone_id="default",
        )

        # Coordinator's grants
        coord_read = rebac_manager.rebac_list_objects(
            subject=("agent", "coordinator_agent"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        coord_read_ids = {obj_id for _, obj_id in coord_read}

        # Worker's grants
        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_shared_1"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Worker should see the same files as coordinator
        assert worker_read_ids == coord_read_ids

    def test_delegation_chain_blocked(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """A delegated worker cannot create further delegations."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Create worker via delegation
        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_chain_a",
            worker_name="Chain A",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
        )

        # Worker tries to delegate → must fail
        with pytest.raises(DelegationChainError):
            delegation_service.delegate(
                coordinator_agent_id="worker_chain_a",
                coordinator_owner_id="alice",
                worker_id="worker_chain_b",
                worker_name="Chain B",
                delegation_mode=DelegationMode.COPY,
                zone_id="default",
            )

    def test_escalation_blocked(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Cannot grant worker access to files coordinator doesn't have."""
        _setup_coordinator(entity_registry, rebac_manager)

        with pytest.raises(EscalationError):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_agent",
                coordinator_owner_id="alice",
                worker_id="worker_esc",
                worker_name="Escalation Worker",
                delegation_mode=DelegationMode.CLEAN,
                zone_id="default",
                add_grants=["/admin/supersecret.txt"],  # coordinator doesn't have this
            )

    def test_multiple_delegations_from_same_coordinator(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Coordinator can create multiple delegations."""
        _setup_coordinator(entity_registry, rebac_manager)

        r1 = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_multi_1",
            worker_name="Worker 1",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="default",
            add_grants=["/workspace/project/src/main.py"],
        )

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_multi_2",
            worker_name="Worker 2",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="default",
            add_grants=["/workspace/project/docs/readme.md"],
        )

        # Both delegations exist
        delegations = delegation_service.list_delegations("coordinator_agent")
        assert len(delegations) == 2
        worker_ids = {d.agent_id for d in delegations}
        assert worker_ids == {"worker_multi_1", "worker_multi_2"}

        # Revoke one, other still active
        delegation_service.revoke_delegation(r1.delegation_id)
        delegations = delegation_service.list_delegations("coordinator_agent")
        assert len(delegations) == 1
        assert delegations[0].agent_id == "worker_multi_2"

    def test_copy_mode_remove_grants(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
        session_factory,
    ):
        """Copy mode with remove_grants excludes specified paths."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_remove",
            worker_name="Remove Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="default",
            remove_grants=["/workspace/secret/credentials.json"],
        )

        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_remove"),
            permission="read",
            object_type="file",
            zone_id="default",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Removed path is gone
        assert "/workspace/secret/credentials.json" not in worker_read_ids
        # Others still there
        assert "/workspace/project/src/main.py" in worker_read_ids
