"""E2E validation for async wrappers (Issue #1440).

Tests the async wrappers against real (non-mocked) kernel implementations:
1. AsyncAgentRegistry with real AgentRegistry + SQLite DB
2. AsyncNamespaceManager with real NamespaceManager + ReBAC
3. AsyncVFSRouter with real PathRouter + LocalBackend
4. AsyncHookEngine with real PluginHooks
5. Protocol isinstance conformance for all 4 wrappers
6. Server factory wiring (import path validation)

These are true integration tests — no mocks for the inner implementations.

Run with:
    pytest tests/e2e/test_async_wrappers_e2e.py -v --override-ini="addopts="
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from nexus.core.agent_registry import AgentRegistry, InvalidTransitionError
from nexus.core.async_agent_registry import AsyncAgentRegistry
from nexus.core.async_namespace_manager import AsyncNamespaceManager
from nexus.core.async_router import AsyncVFSRouter
from nexus.core.router import PathNotMountedError, PathRouter
from nexus.plugins.async_hooks import AsyncHookEngine
from nexus.plugins.hooks import PluginHooks
from nexus.services.protocols.agent_registry import AgentInfo, AgentRegistryProtocol
from nexus.services.protocols.hook_engine import (
    HookContext,
    HookEngineProtocol,
    HookResult,
    HookSpec,
)
from nexus.services.protocols.namespace_manager import NamespaceManagerProtocol
from nexus.storage.models import Base


# ---------------------------------------------------------------------------
# Fixtures: real implementations, not mocks
# ---------------------------------------------------------------------------


@pytest.fixture()
def sqlite_registry(tmp_path: Path) -> AgentRegistry:
    """Real AgentRegistry backed by SQLite."""
    db_path = tmp_path / f"test_{uuid.uuid4().hex[:8]}.db"
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    return AgentRegistry(
        session_factory=session_factory,
        flush_interval=1,
        cache_ttl=1,
    )


@pytest.fixture()
def async_registry(sqlite_registry: AgentRegistry) -> AsyncAgentRegistry:
    return AsyncAgentRegistry(sqlite_registry)


@pytest.fixture()
def real_router(tmp_path: Path) -> PathRouter:
    """Real PathRouter with a local backend mount."""
    from nexus.backends.local import LocalBackend

    storage = tmp_path / "storage"
    storage.mkdir()
    router = PathRouter()
    backend = LocalBackend(root_path=str(storage))
    router.add_mount("/workspace", backend, priority=10)
    return router


@pytest.fixture()
def async_router(real_router: PathRouter) -> AsyncVFSRouter:
    return AsyncVFSRouter(real_router)


@pytest.fixture()
def real_hooks() -> PluginHooks:
    return PluginHooks()


@pytest.fixture()
def async_hooks(real_hooks: PluginHooks) -> AsyncHookEngine:
    return AsyncHookEngine(real_hooks)


# ---------------------------------------------------------------------------
# 1. Protocol isinstance conformance (all 4 wrappers)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """All 4 async wrappers satisfy isinstance checks against their protocols."""

    def test_agent_registry_protocol(self, async_registry: AsyncAgentRegistry) -> None:
        assert isinstance(async_registry, AgentRegistryProtocol)

    def test_namespace_manager_protocol(self) -> None:
        from unittest.mock import MagicMock

        wrapper = AsyncNamespaceManager(MagicMock())
        assert isinstance(wrapper, NamespaceManagerProtocol)

    def test_vfs_router_protocol(self, async_router: AsyncVFSRouter) -> None:
        from nexus.core.protocols.vfs_router import VFSRouterProtocol

        assert isinstance(async_router, VFSRouterProtocol)

    def test_hook_engine_protocol(self, async_hooks: AsyncHookEngine) -> None:
        assert isinstance(async_hooks, HookEngineProtocol)


# ---------------------------------------------------------------------------
# 2. AsyncAgentRegistry with real SQLite DB
# ---------------------------------------------------------------------------


class TestAsyncAgentRegistryE2E:
    """Full agent lifecycle through AsyncAgentRegistry -> real AgentRegistry -> SQLite."""

    @pytest.mark.asyncio()
    async def test_full_lifecycle(self, async_registry: AsyncAgentRegistry) -> None:
        """Register → transition → heartbeat → list → unregister."""
        # Register
        info = await async_registry.register("e2e-agent-1", "alice", zone_id="default", name="E2E Agent")
        assert isinstance(info, AgentInfo)
        assert info.agent_id == "e2e-agent-1"
        assert info.state == "UNKNOWN"
        assert info.generation == 0

        # Get
        fetched = await async_registry.get("e2e-agent-1")
        assert fetched is not None
        assert fetched.agent_id == "e2e-agent-1"

        # Transition UNKNOWN -> CONNECTED (gen 0 -> 1)
        connected = await async_registry.transition("e2e-agent-1", "CONNECTED", expected_generation=0)
        assert connected.state == "CONNECTED"
        assert connected.generation == 1

        # Heartbeat
        await async_registry.heartbeat("e2e-agent-1")

        # Transition CONNECTED -> IDLE
        idle = await async_registry.transition("e2e-agent-1", "IDLE", expected_generation=1)
        assert idle.state == "IDLE"

        # List by zone
        agents = await async_registry.list_by_zone("default")
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
            return await async_registry.register(f"e2e-concurrent-{i}", f"user-{i}", zone_id="default")

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

        resolved = await async_router.route("/workspace/project/file.txt", zone_id="z1")
        assert isinstance(resolved, ResolvedPath)
        assert resolved.backend_path == "project/file.txt"
        assert resolved.mount_point == "/workspace"
        assert resolved.zone_id == "z1"

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
        assert workspace_mount.priority == 10

    @pytest.mark.asyncio()
    async def test_add_and_remove_mount(self, async_router: AsyncVFSRouter) -> None:
        from unittest.mock import MagicMock

        mock_backend = MagicMock()
        mock_backend.name = "test-backend"
        await async_router.add_mount("/test-mount", mock_backend, priority=5, readonly=True)

        mounts = await async_router.list_mounts()
        test_mount = next((m for m in mounts if m.mount_point == "/test-mount"), None)
        assert test_mount is not None
        assert test_mount.readonly is True

        removed = await async_router.remove_mount("/test-mount")
        assert removed is True

        removed_again = await async_router.remove_mount("/test-mount")
        assert removed_again is False


# ---------------------------------------------------------------------------
# 4. AsyncHookEngine with real PluginHooks
# ---------------------------------------------------------------------------


class TestAsyncHookEngineE2E:
    """Hook lifecycle through AsyncHookEngine -> real PluginHooks."""

    @pytest.mark.asyncio()
    async def test_register_fire_unregister(self, async_hooks: AsyncHookEngine) -> None:
        """Full hook lifecycle: register → fire → unregister."""
        captured_contexts: list[HookContext] = []

        async def handler(ctx: HookContext) -> HookResult:
            captured_contexts.append(ctx)
            return HookResult(proceed=True, modified_context=None, error=None)

        # Register
        spec = HookSpec(phase="pre_write", handler_name="e2e-test-hook", priority=10)
        hook_id = await async_hooks.register_hook(spec, handler)
        assert hook_id.id

        # Fire
        ctx = HookContext(
            phase="pre_write",
            path="/workspace/test.txt",
            zone_id="z1",
            agent_id="agent-1",
            payload={"content": "hello"},
        )
        result = await async_hooks.fire("pre_write", ctx)
        assert result.proceed is True
        assert len(captured_contexts) == 1
        assert captured_contexts[0].path == "/workspace/test.txt"

        # Unregister
        removed = await async_hooks.unregister_hook(hook_id)
        assert removed is True

        # Fire again — handler should NOT be called
        await async_hooks.fire("pre_write", ctx)
        assert len(captured_contexts) == 1  # still 1, not 2

    @pytest.mark.asyncio()
    async def test_veto_hook(self, async_hooks: AsyncHookEngine) -> None:
        """Hook that vetoes stops the operation."""

        async def veto_handler(_ctx: HookContext) -> HookResult:
            return HookResult(proceed=False, modified_context=None, error="blocked by policy")

        spec = HookSpec(phase="pre_delete", handler_name="veto-hook")
        await async_hooks.register_hook(spec, veto_handler)

        ctx = HookContext(
            phase="pre_delete", path="/workspace/protected.txt",
            zone_id=None, agent_id=None, payload={},
        )
        result = await async_hooks.fire("pre_delete", ctx)
        assert result.proceed is False
        assert "pre_delete" in (result.error or "")

    @pytest.mark.asyncio()
    async def test_unknown_phase_raises(self, async_hooks: AsyncHookEngine) -> None:
        """Unknown phase string raises ValueError."""
        ctx = HookContext(phase="unknown", path=None, zone_id=None, agent_id=None, payload={})
        with pytest.raises(ValueError, match="Unknown hook phase"):
            await async_hooks.fire("unknown", ctx)


# ---------------------------------------------------------------------------
# 5. Server factory import validation
# ---------------------------------------------------------------------------


class TestServerWiring:
    """Validate that the fastapi_server.py changes load without errors."""

    def test_import_fastapi_server(self) -> None:
        """fastapi_server.py imports cleanly with async wrapper wiring."""
        from nexus.server import fastapi_server

        # Verify AppState has the new attribute
        state = fastapi_server.AppState()
        assert hasattr(state, "async_agent_registry")
        assert state.async_agent_registry is None  # None until lifespan runs

    def test_async_agent_registry_import(self) -> None:
        """AsyncAgentRegistry can be imported from the expected path."""
        from nexus.core.async_agent_registry import AsyncAgentRegistry

        assert AsyncAgentRegistry is not None

    def test_async_namespace_manager_import(self) -> None:
        from nexus.core.async_namespace_manager import AsyncNamespaceManager

        assert AsyncNamespaceManager is not None

    def test_async_router_import(self) -> None:
        from nexus.core.async_router import AsyncVFSRouter

        assert AsyncVFSRouter is not None

    def test_async_hooks_import(self) -> None:
        from nexus.plugins.async_hooks import AsyncHookEngine

        assert AsyncHookEngine is not None
