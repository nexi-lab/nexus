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

from nexus.bricks.delegation.models import DelegationMode
from nexus.bricks.delegation.service import DelegationService
from nexus.bricks.ipc.provisioning import AgentProvisioner
from nexus.bricks.rebac.entity_registry import EntityRegistry
from nexus.bricks.rebac.manager import EnhancedReBACManager
from nexus.services.agents.agent_registration import AgentRegistrationService
from nexus.services.agents.agent_registry import AgentRegistry
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
def agent_registry():
    return AgentRegistry()


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
    return AgentProvisioner(vfs=ipc_storage, zone_id=ZONE)


@pytest.fixture()
def registration_service(
    record_store, agent_registry, entity_registry, rebac_manager, ipc_provisioner
):
    # Wire provisioner into AgentRegistry so register → provision is automatic
    agent_registry.set_provisioner(ipc_provisioner)
    return AgentRegistrationService(
        record_store=record_store,
        agent_registry=agent_registry,
        entity_registry=entity_registry,
        rebac_manager=rebac_manager,
    )


@pytest.fixture()
def delegation_service(record_store, rebac_manager, entity_registry, agent_registry):
    return DelegationService(
        record_store=record_store,
        rebac_manager=rebac_manager,
        entity_registry=entity_registry,
        agent_registry=agent_registry,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _register(registration_service, agent_id, name, grants=None, owner_id="admin"):
    """Register a top-level agent. grants is a list of (path, role) tuples."""
    from nexus.contracts.grant_helpers import GrantInput

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
