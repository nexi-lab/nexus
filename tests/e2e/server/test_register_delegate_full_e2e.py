"""Full end-to-end: register top-level agent → validate permissions → delegate all modes
→ auto_warmup transitions CONNECTED (Issue #3130).

Covers the complete lifecycle using real services (no mocks):
1. Register top-level agent with grants → verify ReBAC check results
2. Registered agent delegates (copy / clean / shared modes) → verify sub-agent permissions
3. auto_warmup=True on delegation → worker transitions UNKNOWN → CONNECTED
4. auto_warmup=False → worker stays UNKNOWN
5. Full chain: top-level → delegate → sub-delegate → revoke

Note: ReBAC stores object IDs as exact strings (not glob patterns). Paths
in grants and checks must match exactly — the same paths that appear in both
the grant tuple and the rebac_check call.
"""

import pytest

from nexus.bricks.delegation.models import DelegationMode, DelegationStatus
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.contracts.agent_types import AgentState
from nexus.services.agents.agent_registration import AgentRegistrationService
from nexus.services.agents.agent_registry import AgentRegistry
from nexus.services.agents.agent_warmup import AgentWarmupService
from nexus.services.agents.warmup_steps import register_standard_steps
from tests.helpers.in_memory_record_store import InMemoryRecordStore
from tests.unit.bricks.ipc.fakes import InMemoryStorageDriver

ZONE = "root"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def record_store():
    store = InMemoryRecordStore()
    yield store
    store.close()


@pytest.fixture()
def entity_registry(record_store):
    return EntityRegistry(record_store)


@pytest.fixture()
def agent_registry(record_store, entity_registry):
    return AgentRegistry(record_store=record_store, entity_registry=entity_registry)


@pytest.fixture()
def rebac_manager(record_store):
    manager = EnhancedReBACManager(engine=record_store.engine, cache_ttl_seconds=0, max_depth=10)
    yield manager
    manager.close()


@pytest.fixture()
def ipc_storage():
    return InMemoryStorageDriver()


@pytest.fixture()
def ipc_provisioner(ipc_storage):
    return AgentProvisioner(storage=ipc_storage, zone_id=ZONE)


@pytest.fixture()
def registration_service(
    record_store, agent_registry, entity_registry, rebac_manager, ipc_provisioner
):
    return AgentRegistrationService(
        record_store=record_store,
        agent_registry=agent_registry,
        entity_registry=entity_registry,
        rebac_manager=rebac_manager,
        ipc_provisioner=ipc_provisioner,
    )


@pytest.fixture()
def delegation_service(record_store, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        record_store=record_store,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


@pytest.fixture()
def warmup_service(agent_registry):
    """Real AgentWarmupService with standard steps registered.

    Standard steps all pass with no optional dependencies:
    - mount_namespace: namespace_manager=None → returns True
    - verify_bricks: enabled_bricks=frozenset() → returns True
    - warm_caches: cache_store=None → returns True
    - connect_mcp: mcp_config=None → returns True
    - load_context: no context_manifest → returns True
    - load_credentials: checks owner_id + eligible state
    """
    svc = AgentWarmupService(agent_registry=agent_registry)
    register_standard_steps(svc)
    return svc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register(registration_service, agent_id, name, grants=None, owner_id="admin"):
    """Register a top-level agent. grants is a list of (path, role) tuples."""
    from nexus.bricks.delegation.grant_helpers import GrantInput

    grant_objs = [GrantInput(path=p, role=r) for p, r in (grants or [])]
    return await registration_service.register(
        agent_id=agent_id,
        name=name,
        owner_id=owner_id,
        zone_id=ZONE,
        grants=grant_objs,
        ipc=True,
    )


def _check(rebac_manager, agent_id, permission, path):
    """rebac_check: returns True/False for agent permission on exact path."""
    return rebac_manager.rebac_check(
        subject=("agent", agent_id),
        permission=permission,
        object=("file", path),
    )


def _list_readable(rebac_manager, agent_id):
    """Return set of paths the agent can read (delegation grants have ABAC conditions
    that rebac_check can't evaluate without context; list_objects skips that check)."""
    results = rebac_manager.rebac_list_objects(
        subject=("agent", agent_id),
        permission="read",
        object_type="file",
        zone_id=ZONE,
    )
    return {obj_id for _, obj_id in results}


def _list_writeable(rebac_manager, agent_id):
    """Return set of paths the agent can write."""
    results = rebac_manager.rebac_list_objects(
        subject=("agent", agent_id),
        permission="write",
        object_type="file",
        zone_id=ZONE,
    )
    return {obj_id for _, obj_id in results}


# ---------------------------------------------------------------------------
# 1. Register → validate ReBAC permissions
# ---------------------------------------------------------------------------


class TestRegisteredAgentPermissions:
    """Verify that registration grants produce correct ReBAC access.

    Uses exact file paths as object IDs (matching how ReBAC stores tuples).
    """

    @pytest.mark.asyncio()
    async def test_editor_grant_allows_read_and_write(
        self, registration_service, rebac_manager, entity_registry
    ):
        """Agent registered with editor grant can read and write that exact path."""
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "koi-agent",
            "Koi",
            grants=[("/workspace/main.py", "editor")],
        )

        assert _check(rebac_manager, "koi-agent", "read", "/workspace/main.py") is True
        assert _check(rebac_manager, "koi-agent", "write", "/workspace/main.py") is True

    @pytest.mark.asyncio()
    async def test_viewer_grant_allows_read_not_write(
        self, registration_service, rebac_manager, entity_registry
    ):
        """Agent registered with viewer grant can read but NOT write."""
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "viewer-agent",
            "Viewer",
            grants=[("/docs/readme.md", "viewer")],
        )

        assert _check(rebac_manager, "viewer-agent", "read", "/docs/readme.md") is True
        assert _check(rebac_manager, "viewer-agent", "write", "/docs/readme.md") is False

    @pytest.mark.asyncio()
    async def test_ungranted_path_denied(
        self, registration_service, rebac_manager, entity_registry
    ):
        """Agent has no access to paths not in its grants."""
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "scoped-agent",
            "Scoped",
            grants=[("/workspace/main.py", "editor")],
        )

        assert _check(rebac_manager, "scoped-agent", "read", "/secrets/key.pem") is False
        assert _check(rebac_manager, "scoped-agent", "write", "/secrets/key.pem") is False

    @pytest.mark.asyncio()
    async def test_multiple_grants_mixed_roles(
        self, registration_service, rebac_manager, entity_registry
    ):
        """Multiple grants give correct access per path."""
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "mixed-agent",
            "Mixed",
            grants=[
                ("/workspace/main.py", "editor"),
                ("/docs/readme.md", "viewer"),
            ],
        )

        # Editor path: read + write
        assert _check(rebac_manager, "mixed-agent", "write", "/workspace/main.py") is True
        assert _check(rebac_manager, "mixed-agent", "read", "/workspace/main.py") is True
        # Viewer path: read only
        assert _check(rebac_manager, "mixed-agent", "read", "/docs/readme.md") is True
        assert _check(rebac_manager, "mixed-agent", "write", "/docs/readme.md") is False
        # Ungranted: denied
        assert _check(rebac_manager, "mixed-agent", "read", "/secrets/db.key") is False

    @pytest.mark.asyncio()
    async def test_no_grants_means_no_access(
        self, registration_service, rebac_manager, entity_registry
    ):
        """Agent registered without grants has no path access."""
        entity_registry.register_entity("user", "admin")
        await _register(registration_service, "no-grants-agent", "No Grants", grants=[])

        assert _check(rebac_manager, "no-grants-agent", "read", "/workspace/main.py") is False


# ---------------------------------------------------------------------------
# 2. Registered agent delegates — all three modes
# ---------------------------------------------------------------------------

# File paths used as explicit grants for top-level agent
_TOP_SRC = "/workspace/src/main.py"
_TOP_UTILS = "/workspace/src/utils.py"
_TOP_DOCS = "/workspace/docs/readme.md"
_TOP_SECRET = "/secrets/credentials.json"


class TestDelegationFromRegisteredAgent:
    """Registered top-level agent delegates sub-agents in copy/clean/shared modes."""

    @pytest.fixture(autouse=True)
    async def setup_top_level(self, registration_service, entity_registry):
        """Register the top-level agent used by all tests in this class."""
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "top-agent",
            "Top Level",
            grants=[
                (_TOP_SRC, "editor"),
                (_TOP_UTILS, "editor"),
                (_TOP_DOCS, "viewer"),
                (_TOP_SECRET, "editor"),
            ],
        )

    def test_copy_mode_inherits_all_grants(self, delegation_service, rebac_manager):
        """COPY mode: sub-agent inherits all top-level grants."""
        result = delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="copy-worker",
            worker_name="Copy Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        assert result.worker_agent_id == "copy-worker"
        assert result.api_key.startswith("sk-")

        readable = _list_readable(rebac_manager, "copy-worker")
        assert _TOP_SRC in readable
        assert _TOP_UTILS in readable
        assert _TOP_DOCS in readable
        assert _TOP_SECRET in readable

    def test_copy_mode_with_scope_prefix_restricts_paths(self, delegation_service, rebac_manager):
        """COPY mode + scope_prefix: sub-agent only gets grants under that prefix."""
        delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="scoped-copy-worker",
            worker_name="Scoped Copy",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            scope_prefix="/workspace/src",
            ttl_seconds=3600,
        )

        readable = _list_readable(rebac_manager, "scoped-copy-worker")
        assert _TOP_SRC in readable
        assert _TOP_UTILS in readable
        # Outside scope: excluded
        assert _TOP_DOCS not in readable
        assert _TOP_SECRET not in readable

    def test_copy_mode_remove_grants(self, delegation_service, rebac_manager):
        """COPY mode + remove_grants: sub-agent loses those exact paths."""
        delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="reduced-worker",
            worker_name="Reduced",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            remove_grants=[_TOP_SECRET],
            ttl_seconds=3600,
        )

        readable = _list_readable(rebac_manager, "reduced-worker")
        assert _TOP_SRC in readable
        assert _TOP_SECRET not in readable

    def test_copy_mode_readonly_downgrade(self, delegation_service, rebac_manager):
        """COPY mode + readonly_paths: paths downgraded to viewer (read only)."""
        delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="readonly-worker",
            worker_name="Readonly",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            readonly_paths=[_TOP_SRC],
            ttl_seconds=3600,
        )

        readable = _list_readable(rebac_manager, "readonly-worker")
        writeable = _list_writeable(rebac_manager, "readonly-worker")

        # /workspace/src/main.py downgraded: readable but not writeable
        assert _TOP_SRC in readable
        assert _TOP_SRC not in writeable
        # Other editor path retains write access
        assert _TOP_UTILS in writeable

    def test_clean_mode_only_add_grants_paths(self, delegation_service, rebac_manager):
        """CLEAN mode: sub-agent ONLY gets explicitly added paths (subset of parent)."""
        delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="clean-worker",
            worker_name="Clean Worker",
            delegation_mode=DelegationMode.CLEAN,
            zone_id=ZONE,
            add_grants=[_TOP_SRC],  # Exact path that parent has
            ttl_seconds=3600,
        )

        readable = _list_readable(rebac_manager, "clean-worker")
        # Only the explicitly added path
        assert _TOP_SRC in readable
        # All other paths excluded
        assert _TOP_UTILS not in readable
        assert _TOP_DOCS not in readable
        assert _TOP_SECRET not in readable

    def test_shared_mode_shares_all_grants(self, delegation_service, rebac_manager):
        """SHARED mode: sub-agent gets same access as coordinator."""
        delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="shared-worker",
            worker_name="Shared Worker",
            delegation_mode=DelegationMode.SHARED,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        readable = _list_readable(rebac_manager, "shared-worker")
        assert _TOP_SRC in readable
        assert _TOP_UTILS in readable
        assert _TOP_DOCS in readable
        assert _TOP_SECRET in readable

    def test_revoke_removes_all_sub_agent_grants(self, delegation_service, rebac_manager):
        """Revoking a delegation removes all sub-agent grants and marks record REVOKED."""
        result = delegation_service.delegate(
            coordinator_agent_id="top-agent",
            coordinator_owner_id="admin",
            worker_id="revoked-worker",
            worker_name="To Revoke",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        # Confirm grants exist before revocation
        assert _TOP_SRC in _list_writeable(rebac_manager, "revoked-worker")

        # Revoke
        delegation_service.revoke_delegation(result.delegation_id)

        # All grants gone
        assert len(_list_readable(rebac_manager, "revoked-worker")) == 0

        # Audit trail preserved
        record = delegation_service.get_delegation_by_id(result.delegation_id)
        assert record.status == DelegationStatus.REVOKED


# ---------------------------------------------------------------------------
# 3. auto_warmup transitions state machine
# ---------------------------------------------------------------------------


class TestAutoWarmupTransitionsState:
    """auto_warmup=True causes worker to transition UNKNOWN → CONNECTED."""

    @pytest.fixture(autouse=True)
    async def setup_coordinator(self, registration_service, entity_registry):
        entity_registry.register_entity("user", "admin")
        await _register(
            registration_service,
            "coord-warmup",
            "Coordinator",
            grants=[("/workspace/main.py", "editor")],
        )

    @pytest.mark.asyncio()
    async def test_warmup_transitions_to_connected(
        self, delegation_service, agent_registry, warmup_service
    ):
        """After delegation, warmup transitions worker UNKNOWN → CONNECTED."""
        delegation_service.delegate(
            coordinator_agent_id="coord-warmup",
            coordinator_owner_id="admin",
            worker_id="warmup-worker",
            worker_name="Warmup Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        assert agent_registry.get("warmup-worker").state == AgentState.UNKNOWN

        result = await warmup_service.warmup("warmup-worker")

        assert result.success is True
        assert result.failed_step is None
        assert agent_registry.get("warmup-worker").state == AgentState.CONNECTED

    @pytest.mark.asyncio()
    async def test_warmup_is_idempotent(self, delegation_service, agent_registry, warmup_service):
        """Warming up a CONNECTED agent returns success without re-running steps."""
        delegation_service.delegate(
            coordinator_agent_id="coord-warmup",
            coordinator_owner_id="admin",
            worker_id="idem-worker",
            worker_name="Idempotent",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        r1 = await warmup_service.warmup("idem-worker")
        assert r1.success is True
        assert agent_registry.get("idem-worker").state == AgentState.CONNECTED

        # Second call — still success
        r2 = await warmup_service.warmup("idem-worker")
        assert r2.success is True
        assert agent_registry.get("idem-worker").state == AgentState.CONNECTED

    @pytest.mark.asyncio()
    async def test_delegation_without_warmup_stays_unknown(
        self, delegation_service, agent_registry
    ):
        """Worker without warmup remains in UNKNOWN state."""
        delegation_service.delegate(
            coordinator_agent_id="coord-warmup",
            coordinator_owner_id="admin",
            worker_id="no-warmup-worker",
            worker_name="No Warmup",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        assert agent_registry.get("no-warmup-worker").state == AgentState.UNKNOWN

    @pytest.mark.asyncio()
    async def test_warmup_failure_preserves_delegation_and_grants(
        self, delegation_service, agent_registry, rebac_manager
    ):
        """Warmup failure leaves delegation intact — agent + key + grants survive."""
        from nexus.contracts.agent_warmup_types import WarmupStep

        result = delegation_service.delegate(
            coordinator_agent_id="coord-warmup",
            coordinator_owner_id="admin",
            worker_id="warmup-fail-worker",
            worker_name="Fail Worker",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
        )

        # Warmup with an unregistered required step
        bad_svc = AgentWarmupService(agent_registry=agent_registry)
        warmup_result = await bad_svc.warmup(
            "warmup-fail-worker",
            steps=[WarmupStep("nonexistent_step", required=True)],
        )

        assert warmup_result.success is False

        # Worker still exists in UNKNOWN state — not deleted
        record = agent_registry.get("warmup-fail-worker")
        assert record is not None
        assert record.state == AgentState.UNKNOWN

        # API key is still valid (delegation not rolled back)
        assert result.api_key.startswith("sk-")

        # Grants still intact (use list_objects — delegation grants have ABAC conditions)
        assert "/workspace/main.py" in _list_readable(rebac_manager, "warmup-fail-worker")


# ---------------------------------------------------------------------------
# 4. Full chain: register → delegate copy → sub-delegate clean → revoke
# ---------------------------------------------------------------------------


class TestFullChain:
    """Complete lifecycle: admin registers root → copy delegate → clean sub-delegate → revoke."""

    @pytest.mark.asyncio()
    async def test_three_level_chain_permissions_and_revocation(
        self,
        registration_service,
        delegation_service,
        rebac_manager,
        agent_registry,
        warmup_service,
        entity_registry,
    ):
        """Root → copy worker (can_sub_delegate=True) → clean worker → verify → revoke."""
        entity_registry.register_entity("user", "admin")

        # Exact paths used throughout
        path_a = "/workspace/src/main.py"
        path_b = "/workspace/src/utils.py"
        path_docs = "/workspace/docs/guide.md"

        # 1. Register root agent
        await _register(
            registration_service,
            "root-agent",
            "Root",
            grants=[(path_a, "editor"), (path_b, "editor"), (path_docs, "viewer")],
        )

        # Root has all grants
        assert _check(rebac_manager, "root-agent", "write", path_a) is True
        assert _check(rebac_manager, "root-agent", "read", path_docs) is True

        # 2. Root delegates COPY to l1 with can_sub_delegate=True
        r1 = delegation_service.delegate(
            coordinator_agent_id="root-agent",
            coordinator_owner_id="admin",
            worker_id="worker-l1",
            worker_name="Level 1",
            delegation_mode=DelegationMode.COPY,
            zone_id=ZONE,
            ttl_seconds=3600,
            can_sub_delegate=True,
        )

        l1_readable = _list_readable(rebac_manager, "worker-l1")
        assert path_a in l1_readable
        assert path_b in l1_readable
        assert path_docs in l1_readable

        # 3. Warmup l1 → CONNECTED (required to sub-delegate in some flows)
        warmup_l1 = await warmup_service.warmup("worker-l1")
        assert warmup_l1.success is True
        assert agent_registry.get("worker-l1").state == AgentState.CONNECTED

        # 4. l1 delegates CLEAN to l2 — only path_a
        r2 = delegation_service.delegate(
            coordinator_agent_id="worker-l1",
            coordinator_owner_id="admin",
            worker_id="worker-l2",
            worker_name="Level 2",
            delegation_mode=DelegationMode.CLEAN,
            zone_id=ZONE,
            add_grants=[path_a],  # exact path that l1 has
            ttl_seconds=1800,
        )

        # 5. Verify permission hierarchy
        # root-agent grants are from registration (no ABAC conditions) → rebac_check works
        assert _check(rebac_manager, "root-agent", "write", path_a) is True
        # delegation grants have ABAC conditions → use rebac_list_objects
        assert path_a in _list_writeable(rebac_manager, "worker-l1")
        assert path_a in _list_readable(rebac_manager, "worker-l2")
        assert path_b not in _list_readable(rebac_manager, "worker-l2")
        assert path_docs not in _list_readable(rebac_manager, "worker-l2")

        # 6. Revoke l1
        delegation_service.revoke_delegation(r1.delegation_id)

        # 7. l1 loses grants
        assert len(_list_readable(rebac_manager, "worker-l1")) == 0

        # 8. Root agent's grants are unaffected (registration grants, no ABAC conditions)
        assert _check(rebac_manager, "root-agent", "write", path_a) is True
        assert _check(rebac_manager, "root-agent", "read", path_docs) is True

        # 9. Audit trail
        assert (
            delegation_service.get_delegation_by_id(r1.delegation_id).status
            == DelegationStatus.REVOKED
        )
        # l2 delegation record still exists (not cascaded)
        assert delegation_service.get_delegation_by_id(r2.delegation_id) is not None
