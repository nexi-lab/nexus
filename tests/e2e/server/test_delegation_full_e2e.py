"""Full end-to-end test for delegation API with real server (Issue #1271, #1618).

Uses real NexusFS components, ReBAC permissions enabled,
and validates the complete delegation flow including #1618 features:
1. Coordinator agent authenticates
2. Coordinator creates files and gets grants
3. POST /api/v2/agents/delegate (copy mode)
4. Worker authenticates with returned API key
5. Worker can read files within granted namespace
6. Worker cannot read files outside namespace
7. DELETE to revoke delegation
8. GET to list delegations with pagination
9. Sub-delegation chain (A→B→C)
10. Soft-delete revocation pattern

Requires real EnhancedReBACManager + NamespaceManager + EntityRegistry.
"""

import pytest

from nexus.bricks.delegation.errors import DelegationChainError, EscalationError
from nexus.bricks.delegation.models import DelegationMode, DelegationStatus
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.agents.agent_registry import AgentRegistry
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
    return record_store.engine


@pytest.fixture()
def session_factory(record_store):
    return record_store.session_factory


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
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store):
    """Create a real AgentRegistry backed by SQLite."""
    return AgentRegistry(record_store=record_store)


@pytest.fixture()
def delegation_service(record_store, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        record_store=record_store,
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
                "zone_id": "root",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/project/src/utils.py"),
                "zone_id": "root",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_viewer",
                "object": ("file", "/workspace/project/docs/readme.md"),
                "zone_id": "root",
            },
            {
                "subject": ("agent", "coordinator_agent"),
                "relation": "direct_editor",
                "object": ("file", "/workspace/secret/credentials.json"),
                "zone_id": "root",
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
            zone_id="root",
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
            zone_id="root",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Worker should have access to /workspace/project/* files
        assert "/workspace/project/src/main.py" in worker_read_ids
        assert "/workspace/project/src/utils.py" in worker_read_ids
        assert "/workspace/project/docs/readme.md" in worker_read_ids

        # Worker should NOT have access to /workspace/secret/
        assert "/workspace/secret/credentials.json" not in worker_read_ids

        # Step 3: List delegations (#1618: returns tuple with pagination)
        delegations, total = delegation_service.list_delegations("coordinator_agent")
        assert total == 1
        assert len(delegations) == 1
        assert delegations[0].agent_id == "worker_copy_1"
        assert delegations[0].scope_prefix == "/workspace/project"
        assert delegations[0].status == DelegationStatus.ACTIVE

        # Step 4: Revoke (soft-delete-first pattern, #1618)
        delegation_service.revoke_delegation(result.delegation_id)

        # Step 5: Verify worker no longer has grants
        worker_read_after = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_copy_1"),
            permission="read",
            object_type="file",
            zone_id="root",
        )
        assert len(worker_read_after) == 0

        # Step 6: get_delegation returns None (ACTIVE filter)
        assert delegation_service.get_delegation("worker_copy_1") is None

        # Step 7: get_delegation_by_id shows REVOKED status (audit trail)
        record = delegation_service.get_delegation_by_id(result.delegation_id)
        assert record is not None
        assert record.status == DelegationStatus.REVOKED

    def test_copy_mode_with_readonly_downgrade(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """Delegate with readonly_paths → worker can read but not write those files."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_readonly",
            worker_name="Readonly Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="root",
            scope_prefix="/workspace/project",
            readonly_paths=["/workspace/project/src/main.py"],
        )

        # Worker can read main.py
        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_readonly"),
            permission="read",
            object_type="file",
            zone_id="root",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}
        assert "/workspace/project/src/main.py" in worker_read_ids

        # Worker cannot write main.py (downgraded to viewer)
        worker_write = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_readonly"),
            permission="write",
            object_type="file",
            zone_id="root",
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
    ):
        """Clean mode: only explicitly added grants from parent."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_clean_1",
            worker_name="Clean Worker",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="root",
            add_grants=["/workspace/project/src/main.py"],
        )

        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_clean_1"),
            permission="read",
            object_type="file",
            zone_id="root",
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
    ):
        """Shared mode: worker gets same view as coordinator."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_shared_1",
            worker_name="Shared Worker",
            delegation_mode=DelegationMode.SHARED,
            zone_id="root",
        )

        # Coordinator's grants
        coord_read = rebac_manager.rebac_list_objects(
            subject=("agent", "coordinator_agent"),
            permission="read",
            object_type="file",
            zone_id="root",
        )
        coord_read_ids = {obj_id for _, obj_id in coord_read}

        # Worker's grants
        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_shared_1"),
            permission="read",
            object_type="file",
            zone_id="root",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Worker should see the same files as coordinator
        assert worker_read_ids == coord_read_ids

    def test_delegation_chain_blocked(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """A delegated worker cannot create further delegations (default)."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Create worker via delegation (can_sub_delegate=False by default)
        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_chain_a",
            worker_name="Chain A",
            delegation_mode=DelegationMode.COPY,
            zone_id="root",
        )

        # Worker tries to delegate → must fail
        with pytest.raises(DelegationChainError):
            delegation_service.delegate(
                coordinator_agent_id="worker_chain_a",
                coordinator_owner_id="alice",
                worker_id="worker_chain_b",
                worker_name="Chain B",
                delegation_mode=DelegationMode.COPY,
                zone_id="root",
            )

    def test_delegation_chain_allowed_with_sub_delegate(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """#1618: Sub-delegation chain allowed when can_sub_delegate=True."""
        _setup_coordinator(entity_registry, rebac_manager)

        # A → B with can_sub_delegate=True
        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_sub_b",
            worker_name="Sub Worker B",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
            can_sub_delegate=True,
            intent="Coordinate sub-tasks",
        )

        # B → C
        result_c = delegation_service.delegate(
            coordinator_agent_id="worker_sub_b",
            coordinator_owner_id="alice",
            worker_id="worker_sub_c",
            worker_name="Sub Worker C",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
            intent="Execute specific task",
        )

        # Verify chain
        chain = delegation_service.get_delegation_chain(result_c.delegation_id)
        assert len(chain) == 2
        assert chain[0].agent_id == "worker_sub_c"
        assert chain[0].depth == 1
        assert chain[1].agent_id == "worker_sub_b"
        assert chain[1].depth == 0

    def test_escalation_blocked(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
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
                zone_id="root",
                add_grants=["/admin/supersecret.txt"],  # coordinator doesn't have this
            )

    def test_multiple_delegations_from_same_coordinator(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """Coordinator can create multiple delegations."""
        _setup_coordinator(entity_registry, rebac_manager)

        r1 = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_multi_1",
            worker_name="Worker 1",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="root",
            add_grants=["/workspace/project/src/main.py"],
        )

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_multi_2",
            worker_name="Worker 2",
            delegation_mode=DelegationMode.CLEAN,
            zone_id="root",
            add_grants=["/workspace/project/docs/readme.md"],
        )

        # Both delegations exist
        delegations, total = delegation_service.list_delegations("coordinator_agent")
        assert total == 2
        worker_ids = {d.agent_id for d in delegations}
        assert worker_ids == {"worker_multi_1", "worker_multi_2"}

        # Revoke one
        delegation_service.revoke_delegation(r1.delegation_id)

        # Filter by ACTIVE status — only 1 remains
        active_delegations, active_total = delegation_service.list_delegations(
            "coordinator_agent", status_filter=DelegationStatus.ACTIVE
        )
        assert active_total == 1
        assert active_delegations[0].agent_id == "worker_multi_2"

        # Without filter — both still visible (audit trail)
        all_delegations, all_total = delegation_service.list_delegations("coordinator_agent")
        assert all_total == 2

    def test_copy_mode_remove_grants(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """Copy mode with remove_grants excludes specified paths."""
        _setup_coordinator(entity_registry, rebac_manager)

        delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_remove",
            worker_name="Remove Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id="root",
            remove_grants=["/workspace/secret/credentials.json"],
        )

        worker_read = rebac_manager.rebac_list_objects(
            subject=("agent", "worker_remove"),
            permission="read",
            object_type="file",
            zone_id="root",
        )
        worker_read_ids = {obj_id for _, obj_id in worker_read}

        # Removed path is gone
        assert "/workspace/secret/credentials.json" not in worker_read_ids
        # Others still there
        assert "/workspace/project/src/main.py" in worker_read_ids

    def test_delegation_with_intent(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """#1618: Intent is persisted and retrievable."""
        _setup_coordinator(entity_registry, rebac_manager)

        result = delegation_service.delegate(
            coordinator_agent_id="coordinator_agent",
            coordinator_owner_id="alice",
            worker_id="worker_intent",
            worker_name="Intent Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ROOT_ZONE_ID,
            intent="Run security scan on project files",
        )

        record = delegation_service.get_delegation_by_id(result.delegation_id)
        assert record is not None
        assert record.intent == "Run security scan on project files"

    def test_pagination(
        self,
        delegation_service,
        entity_registry,
        rebac_manager,
    ):
        """#1618: Pagination with limit/offset."""
        _setup_coordinator(entity_registry, rebac_manager)

        # Create 5 delegations
        for i in range(5):
            delegation_service.delegate(
                coordinator_agent_id="coordinator_agent",
                coordinator_owner_id="alice",
                worker_id=f"worker_page_{i}",
                worker_name=f"Page Worker {i}",
                delegation_mode=DelegationMode.COPY,
                zone_id=ROOT_ZONE_ID,
            )

        # Get first page
        page1, total = delegation_service.list_delegations("coordinator_agent", limit=2, offset=0)
        assert total == 5
        assert len(page1) == 2

        # Get second page
        page2, total2 = delegation_service.list_delegations("coordinator_agent", limit=2, offset=2)
        assert total2 == 5
        assert len(page2) == 2

        # Pages should not overlap
        page1_ids = {d.delegation_id for d in page1}
        page2_ids = {d.delegation_id for d in page2}
        assert page1_ids.isdisjoint(page2_ids)
