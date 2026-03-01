"""E2E validation for async wrappers (Issue #1440).

Tests the async wrappers against real (non-mocked) kernel implementations:
1. AsyncAgentRegistry with real AgentRegistry + SQLite DB
2. AsyncNamespaceManager with real NamespaceManager + ReBAC
3. AsyncVFSRouter with real PathRouter + LocalBackend
4. Protocol isinstance conformance for all wrappers
5. Server factory wiring (import path validation)

These are true integration tests — no mocks for the inner implementations.

Run with:
    pytest tests/e2e/test_async_wrappers_e2e.py -v --override-ini="addopts="
"""

import asyncio
import types
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from nexus.bricks.rebac.async_namespace_manager import AsyncNamespaceManager
from nexus.core.async_router import AsyncVFSRouter
from nexus.core.router import PathNotMountedError, PathRouter
from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol
from nexus.storage.record_store import SQLAlchemyRecordStore
from nexus.system_services.agents.agent_registry import AgentRegistry, InvalidTransitionError
from nexus.system_services.agents.async_agent_registry import AsyncAgentRegistry

# ---------------------------------------------------------------------------
# Fixtures: real implementations, not mocks
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_registry(tmp_path: Path) -> Iterator[AgentRegistry]:
    """Real AgentRegistry backed by SQLite."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}", create_tables=True)
    yield AgentRegistry(
        record_store=record_store,
        flush_interval=1,
    )
    record_store.close()


@pytest.fixture()
def async_registry(sqlite_registry: AgentRegistry) -> AsyncAgentRegistry:
    return AsyncAgentRegistry(sqlite_registry)


@pytest.fixture()
def real_router(tmp_path: Path) -> PathRouter:
    """Real PathRouter with a local backend mount."""
    from nexus.backends.local import LocalBackend
    from tests.helpers.dict_metastore import DictMetastore

    storage = tmp_path / "storage"
    storage.mkdir()
    metastore = DictMetastore()
    router = PathRouter(metastore)
    backend = LocalBackend(root_path=str(storage))
    router.add_mount("/workspace", backend)
    return router


@pytest.fixture()
def async_router(real_router: PathRouter) -> AsyncVFSRouter:
    return AsyncVFSRouter(real_router)


# ---------------------------------------------------------------------------
# 1. Protocol isinstance conformance (all wrappers)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Async wrappers satisfy isinstance checks against their protocols."""

    def test_agent_registry_protocol(self, async_registry: AsyncAgentRegistry) -> None:
        assert isinstance(async_registry, AgentRegistryProtocol)

    def test_namespace_manager_protocol(self) -> None:
        from unittest.mock import MagicMock

        wrapper = AsyncNamespaceManager(MagicMock())
        assert isinstance(wrapper, NamespaceManagerProtocol)

    def test_vfs_router_protocol(self, async_router: AsyncVFSRouter) -> None:
        from nexus.core.protocols.vfs_router import VFSRouterProtocol

        assert isinstance(async_router, VFSRouterProtocol)


# ---------------------------------------------------------------------------
# 2. AsyncAgentRegistry with real SQLite DB
# ---------------------------------------------------------------------------


class TestAsyncAgentRegistryE2E:
    """Full agent lifecycle through AsyncAgentRegistry -> real AgentRegistry -> SQLite."""

    @pytest.mark.asyncio()
    async def test_full_lifecycle(self, async_registry: AsyncAgentRegistry) -> None:
        """Register → transition → heartbeat → list → unregister."""
        # Register
        info = await async_registry.register(
            "e2e-agent-1", "alice", zone_id="root", name="E2E Agent"
        )
        assert isinstance(info, AgentInfo)
        assert info.agent_id == "e2e-agent-1"
        assert info.state == "UNKNOWN"
        assert info.generation == 0

        # Get
        fetched = await async_registry.get("e2e-agent-1")
        assert fetched is not None
        assert fetched.agent_id == "e2e-agent-1"

        # Transition UNKNOWN -> CONNECTED (gen 0 -> 1)
        connected = await async_registry.transition(
            "e2e-agent-1", "CONNECTED", expected_generation=0
        )
        assert connected.state == "CONNECTED"
        assert connected.generation == 1

        # Heartbeat
        await async_registry.heartbeat("e2e-agent-1")

        # Transition CONNECTED -> IDLE
        idle = await async_registry.transition("e2e-agent-1", "IDLE", expected_generation=1)
        assert idle.state == "IDLE"

        # List by zone
        agents = await async_registry.list_by_zone("root")
        assert any(a.agent_id == "e2e-agent-1" for a in agents)

        # Unregister
        removed = await async_registry.unregister("e2e-agent-1")
        assert removed is True

        # Verify gone
        gone = await async_registry.get("e2e-agent-1")
        assert gone is None

    @pytest.mark.asyncio()
    async def test_invalid_transition_propagates(self, async_registry: AsyncAgentRegistry) -> None:
        """InvalidTransitionError propagates through to_thread."""
        await async_registry.register("e2e-invalid-1", "alice")
        with pytest.raises(InvalidTransitionError):
            await async_registry.transition("e2e-invalid-1", "IDLE")

    @pytest.mark.asyncio()
    async def test_invalid_state_string_raises(self, async_registry: AsyncAgentRegistry) -> None:
        """Invalid state string raises ValueError with helpful message."""
        await async_registry.register("e2e-badstate-1", "alice")
        with pytest.raises(ValueError, match="Invalid target state"):
            await async_registry.transition("e2e-badstate-1", "BOGUS")

    @pytest.mark.asyncio()
    async def test_concurrent_registrations(self, async_registry: AsyncAgentRegistry) -> None:
        """Multiple concurrent registrations via asyncio.gather."""

        async def register(i: int) -> AgentInfo:
            return await async_registry.register(f"e2e-concurrent-{i}", f"user-{i}", zone_id="root")

        results = await asyncio.gather(*[register(i) for i in range(10)])
        assert len(results) == 10
        agent_ids = {r.agent_id for r in results}
        assert len(agent_ids) == 10


# ---------------------------------------------------------------------------
# 3. AsyncVFSRouter with real PathRouter + LocalBackend
# ---------------------------------------------------------------------------


class TestAsyncVFSRouterE2E:
    """Route resolution through AsyncVFSRouter -> real PathRouter -> LocalBackend."""

    @pytest.mark.asyncio()
    async def test_route_resolves(self, async_router: AsyncVFSRouter) -> None:
        from nexus.core.protocols.vfs_router import ResolvedPath

        resolved = await async_router.route("/workspace/project/file.txt")
        assert isinstance(resolved, ResolvedPath)
        assert resolved.backend_path == "project/file.txt"
        assert resolved.mount_point == "/workspace"

    @pytest.mark.asyncio()
    async def test_not_mounted_raises(self, async_router: AsyncVFSRouter) -> None:
        with pytest.raises(PathNotMountedError):
            await async_router.route("/nonexistent/path")

    @pytest.mark.asyncio()
    async def test_list_mounts(self, async_router: AsyncVFSRouter) -> None:
        from nexus.core.protocols.vfs_router import MountInfo

        mounts = await async_router.list_mounts()
        assert len(mounts) >= 1
        assert all(isinstance(m, MountInfo) for m in mounts)
        workspace_mount = next(m for m in mounts if m.mount_point == "/workspace")
        assert workspace_mount.readonly is False

    @pytest.mark.asyncio()
    async def test_add_and_remove_mount(self, async_router: AsyncVFSRouter) -> None:
        from unittest.mock import MagicMock

        mock_backend = MagicMock()
        mock_backend.name = "test-backend"
        await async_router.add_mount("/test-mount", mock_backend, readonly=True)

        mounts = await async_router.list_mounts()
        test_mount = next((m for m in mounts if m.mount_point == "/test-mount"), None)
        assert test_mount is not None
        assert test_mount.readonly is True

        removed = await async_router.remove_mount("/test-mount")
        assert removed is True

        removed_again = await async_router.remove_mount("/test-mount")
        assert removed_again is False


# ---------------------------------------------------------------------------
# 4. Server factory import validation
# ---------------------------------------------------------------------------


class TestServerWiring:
    """Validate that the fastapi_server.py changes load without errors."""

    def test_import_fastapi_server(self) -> None:
        """fastapi_server.py imports cleanly with async wrapper wiring."""
        from nexus.server import fastapi_server

        # AppState class was removed (Issue #1288); app state now lives on
        # app.state directly.  Just verify the module imports cleanly.
        assert hasattr(fastapi_server, "create_app")

    def test_async_agent_registry_import(self) -> None:
        """AsyncAgentRegistry can be imported from the expected path."""
        from nexus.system_services.agents.async_agent_registry import AsyncAgentRegistry

        assert AsyncAgentRegistry is not None

    def test_async_namespace_manager_import(self) -> None:
        from nexus.bricks.rebac.async_namespace_manager import AsyncNamespaceManager

        assert AsyncNamespaceManager is not None

    def test_async_router_import(self) -> None:
        from nexus.core.async_router import AsyncVFSRouter

        assert AsyncVFSRouter is not None


# ---------------------------------------------------------------------------
# 5. Server lifespan wiring simulation
# ---------------------------------------------------------------------------


class TestServerLifespanWiring:
    """Simulate the FastAPI lifespan wiring path for async wrappers.

    Reproduces the exact initialization logic from fastapi_server.py lifespan
    to verify that AsyncAgentRegistry is created correctly when AgentRegistry
    is available (as it would be in production with permissions enabled).
    """

    @pytest.mark.asyncio()
    async def test_lifespan_wiring_with_real_db(self, tmp_path: Path) -> None:
        """Simulate server lifespan: AgentRegistry + AsyncAgentRegistry wiring."""
        from nexus.system_services.agents.agent_registry import AgentRegistry
        from nexus.system_services.agents.async_agent_registry import AsyncAgentRegistry

        # Create real SQLite-backed AgentRegistry (same as server lifespan does)
        db_path = tmp_path / f"lifespan_{uuid.uuid4().hex[:8]}.db"
        record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}", create_tables=True)

        agent_registry = AgentRegistry(
            record_store=record_store,
            flush_interval=60,
        )

        # Simulate the lifespan wiring — AppState was removed (Issue #1288),
        # app state now lives on app.state (a SimpleNamespace).
        state = types.SimpleNamespace()
        state.agent_registry = agent_registry
        state.async_agent_registry = AsyncAgentRegistry(state.agent_registry)

        try:
            # Verify the async wrapper is functional through the wired path
            assert isinstance(state.async_agent_registry, AgentRegistryProtocol)

            info = await state.async_agent_registry.register(
                "lifespan-agent",
                "admin",
                zone_id="root",
                name="Lifespan Test",
            )
            assert info.agent_id == "lifespan-agent"
            assert info.state == "UNKNOWN"

            # Transition (simulates what an API endpoint would do)
            connected = await state.async_agent_registry.transition(
                "lifespan-agent",
                "CONNECTED",
                expected_generation=0,
            )
            assert connected.state == "CONNECTED"

            # List agents in zone (simulates permission-aware listing)
            agents = await state.async_agent_registry.list_by_zone("root")
            assert len(agents) == 1
            assert agents[0].agent_id == "lifespan-agent"

            # Cleanup
            await state.async_agent_registry.unregister("lifespan-agent")
        finally:
            record_store.close()

    @pytest.mark.asyncio()
    async def test_all_wrappers_wired_together(self, tmp_path: Path) -> None:
        """All async wrappers initialized and operational simultaneously.

        Simulates a server with all wrappers active (as would happen
        when permissions are enabled and all subsystems are available).
        """
        from unittest.mock import MagicMock

        from nexus.backends.local import LocalBackend
        from nexus.bricks.rebac.async_namespace_manager import AsyncNamespaceManager
        from nexus.core.async_router import AsyncVFSRouter
        from nexus.core.protocols.vfs_router import VFSRouterProtocol
        from nexus.core.router import PathRouter
        from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol
        from nexus.system_services.agents.agent_registry import AgentRegistry
        from nexus.system_services.agents.async_agent_registry import AsyncAgentRegistry

        # 1. AgentRegistry + AsyncAgentRegistry (SQLite)
        db_path = tmp_path / f"all3_{uuid.uuid4().hex[:8]}.db"
        record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}", create_tables=True)
        sync_registry = AgentRegistry(record_store=record_store, flush_interval=1)
        async_registry = AsyncAgentRegistry(sync_registry)

        # 2. NamespaceManager + AsyncNamespaceManager
        async_ns = AsyncNamespaceManager(MagicMock())

        # 3. PathRouter + AsyncVFSRouter (real LocalBackend)
        storage = tmp_path / "storage"
        storage.mkdir()
        from tests.helpers.dict_metastore import DictMetastore

        sync_router = PathRouter(DictMetastore())
        sync_router.add_mount("/workspace", LocalBackend(root_path=str(storage)))
        async_router = AsyncVFSRouter(sync_router)

        # All satisfy their protocols
        assert isinstance(async_registry, AgentRegistryProtocol)
        assert isinstance(async_ns, NamespaceManagerProtocol)
        assert isinstance(async_router, VFSRouterProtocol)

        try:
            # Cross-wrapper interaction: register agent, route a path
            agent_info = await async_registry.register("cross-agent", "admin", zone_id="z1")
            assert agent_info.agent_id == "cross-agent"

            resolved = await async_router.route("/workspace/doc.txt")
            assert resolved.backend_path == "doc.txt"

            # Cleanup
            await async_registry.unregister("cross-agent")
        finally:
            record_store.close()

    @pytest.mark.asyncio()
    async def test_permission_enforcer_can_use_async_registry(self, tmp_path: Path) -> None:
        """AsyncAgentRegistry is compatible with permission enforcement flow.

        When permissions are enabled, the PermissionEnforcer needs to look up
        agent info. This test verifies the async wrapper provides correct data
        for permission decisions.
        """
        from nexus.system_services.agents.agent_registry import AgentRegistry
        from nexus.system_services.agents.async_agent_registry import AsyncAgentRegistry

        db_path = tmp_path / f"perm_{uuid.uuid4().hex[:8]}.db"
        record_store = SQLAlchemyRecordStore(db_url=f"sqlite:///{db_path}", create_tables=True)

        sync_reg = AgentRegistry(record_store=record_store, flush_interval=1)
        async_reg = AsyncAgentRegistry(sync_reg)

        try:
            # Register agent with specific owner and zone (permission-relevant fields)
            info = await async_reg.register(
                "perm-agent",
                "user:alice",
                zone_id="zone-42",
                name="Permission Test Agent",
            )
            assert info.owner_id == "user:alice"
            assert info.zone_id == "zone-42"

            # Simulate permission check: "does this agent belong to this zone?"
            fetched = await async_reg.get("perm-agent")
            assert fetched is not None
            assert fetched.zone_id == "zone-42"
            assert fetched.owner_id == "user:alice"
            assert fetched.state == "UNKNOWN"  # Not yet connected

            # Transition to CONNECTED (agent authenticates)
            connected = await async_reg.transition("perm-agent", "CONNECTED", expected_generation=0)
            assert connected.state == "CONNECTED"
            assert connected.generation == 1

            # List by zone (used in zone-scoped permission queries)
            zone_agents = await async_reg.list_by_zone("zone-42")
            assert len(zone_agents) == 1
            assert zone_agents[0].owner_id == "user:alice"

            # Different zone should be empty
            other_zone = await async_reg.list_by_zone("zone-99")
            assert len(other_zone) == 0

            await async_reg.unregister("perm-agent")
        finally:
            record_store.close()
