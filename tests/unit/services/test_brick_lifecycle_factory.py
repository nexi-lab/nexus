"""Tests for BrickLifecycleManager factory wiring (Issue #1704).

Phase 4 TDD: integration with factory.py boot sequence.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.factory import (
    _FACTORY_BRICKS,
    _FACTORY_SKIP,
    _register_factory_bricks,
    _WorkflowLifecycleAdapter,
)
from nexus.services.protocols.brick_lifecycle import BrickState
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str = "test") -> MagicMock:
    brick = AsyncMock()
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


# ---------------------------------------------------------------------------
# Factory integration: BrickLifecycleManager in KernelServices
# ---------------------------------------------------------------------------


class TestSystemServicesIntegration:
    """Verify BrickLifecycleManager is a first-class SystemServices field (Issue #2034)."""

    def test_system_services_has_brick_lifecycle_manager_field(self) -> None:
        """SystemServices should have a brick_lifecycle_manager field."""
        from nexus.core.config import SystemServices

        ss = SystemServices()
        assert hasattr(ss, "brick_lifecycle_manager")
        assert ss.brick_lifecycle_manager is None  # Default is None

    def test_system_services_accepts_lifecycle_manager(self) -> None:
        """SystemServices should accept a BrickLifecycleManager instance."""
        from nexus.core.config import SystemServices

        manager = BrickLifecycleManager()
        ss = SystemServices(brick_lifecycle_manager=manager)
        assert ss.brick_lifecycle_manager is manager


# ---------------------------------------------------------------------------
# Boot integration: lifecycle manager created during system boot
# ---------------------------------------------------------------------------


class TestBootIntegration:
    """Verify lifecycle manager is created during factory boot."""

    def test_lifecycle_manager_creation(self) -> None:
        """BrickLifecycleManager should be constructable at boot time."""
        manager = BrickLifecycleManager()
        assert isinstance(manager, BrickLifecycleManager)

    @pytest.mark.asyncio
    async def test_lifecycle_manager_register_and_mount_pattern(self) -> None:
        """Verify the factory pattern: register bricks → mount_all → health."""
        manager = BrickLifecycleManager()

        # Register brick services (as factory would)
        wallet = _make_lifecycle_brick("wallet")
        search = _make_lifecycle_brick("search")
        manifest = _make_lifecycle_brick("manifest")

        manager.register("wallet_provisioner", wallet, protocol_name="WalletProtocol")
        manager.register(
            "search_service",
            search,
            protocol_name="SearchProtocol",
        )
        manager.register(
            "manifest_resolver",
            manifest,
            protocol_name="ManifestProtocol",
        )

        # Mount all bricks
        report = await manager.mount_all()

        assert report.total == 3
        assert report.active == 3
        assert report.failed == 0

    @pytest.mark.asyncio
    async def test_lifecycle_manager_with_dependency_chain(self) -> None:
        """Factory pattern with dependencies between bricks."""
        manager = BrickLifecycleManager()

        infra = _make_lifecycle_brick("infra")
        search = _make_lifecycle_brick("search")
        rag = _make_lifecycle_brick("rag")

        manager.register("infra", infra, protocol_name="InfraProtocol")
        manager.register("search", search, protocol_name="SearchProtocol", depends_on=("infra",))
        manager.register("rag", rag, protocol_name="RAGProtocol", depends_on=("search",))

        report = await manager.mount_all()
        assert report.active == 3

        # Shutdown in reverse order
        report = await manager.unmount_all()
        assert report.active == 0

    @pytest.mark.asyncio
    async def test_lifecycle_manager_handles_brick_failure_gracefully(self) -> None:
        """Factory boot should continue even if one brick fails."""
        manager = BrickLifecycleManager()

        good = _make_lifecycle_brick("good")
        bad = _make_lifecycle_brick("bad")
        bad.start = AsyncMock(side_effect=RuntimeError("service unavailable"))
        also_good = _make_lifecycle_brick("also_good")

        manager.register("good", good, protocol_name="GP")
        manager.register("bad", bad, protocol_name="BP")
        manager.register("also_good", also_good, protocol_name="AGP")

        report = await manager.mount_all()

        assert report.total == 3
        assert report.active == 2
        assert report.failed == 1
        assert manager.get_status("bad").state == BrickState.FAILED  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# _register_factory_bricks integration
# ---------------------------------------------------------------------------


class TestRegisterFactoryBricks:
    """Verify _register_factory_bricks registers the correct bricks."""

    def test_registers_non_none_bricks(self) -> None:
        """Non-None brick entries should be registered with the manager."""
        manager = BrickLifecycleManager()
        brick_dict = {
            "manifest_resolver": MagicMock(),
            "chunked_upload_service": MagicMock(),
            "snapshot_service": MagicMock(),
            "task_queue_service": MagicMock(),
            "ipc_vfs_driver": MagicMock(),
            "wallet_provisioner": MagicMock(),
            "workflow_engine": MagicMock(),
            "event_bus": MagicMock(),
            "lock_manager": MagicMock(),
            "delegation_service": MagicMock(),
            "reputation_service": MagicMock(),
            "version_service": MagicMock(),
        }

        _register_factory_bricks(manager, brick_dict)

        # 11 standard bricks + 1 workflow engine = 12
        report = manager.health()
        assert report.total == 12

        # Verify workflow engine is wrapped in adapter
        status = manager.get_status("workflow_engine")
        assert status is not None
        assert status.protocol_name == "WorkflowProtocol"

        # Verify event_bus and lock_manager ARE registered (Issue #2991)
        assert manager.get_status("event_bus") is not None
        assert manager.get_status("event_bus").protocol_name == "EventBusProtocol"
        assert manager.get_status("lock_manager") is not None
        assert manager.get_status("lock_manager").protocol_name == "LockManagerProtocol"

        # Verify dependency edges (Issue #2991)
        ipc_spec = manager.get_spec("ipc_vfs_driver")
        assert ipc_spec is not None
        assert "event_bus" in ipc_spec.depends_on

        delegation_spec = manager.get_spec("delegation_service")
        assert delegation_spec is not None
        assert "reputation_service" in delegation_spec.depends_on

        wf_spec = manager.get_spec("workflow_engine")
        assert wf_spec is not None
        assert "event_bus" in wf_spec.depends_on

    def test_skips_none_values(self) -> None:
        """None entries in brick_dict should be silently skipped."""
        manager = BrickLifecycleManager()
        brick_dict = {
            "manifest_resolver": MagicMock(),
            "chunked_upload_service": None,
            "snapshot_service": None,
            "task_queue_service": None,
            "ipc_vfs_driver": None,
            "wallet_provisioner": None,
            "workflow_engine": None,
        }

        _register_factory_bricks(manager, brick_dict)

        report = manager.health()
        assert report.total == 1  # Only manifest_resolver

    def test_skips_missing_keys(self) -> None:
        """Missing keys in brick_dict should be silently skipped."""
        manager = BrickLifecycleManager()
        _register_factory_bricks(manager, {})

        report = manager.health()
        assert report.total == 0


# ---------------------------------------------------------------------------
# _WorkflowLifecycleAdapter
# ---------------------------------------------------------------------------


class TestWorkflowLifecycleAdapter:
    """Verify the WorkflowEngine lifecycle adapter."""

    @pytest.mark.asyncio
    async def test_start_calls_engine_startup(self) -> None:
        """Adapter.start() should delegate to engine.startup()."""
        engine = AsyncMock()
        engine.startup = AsyncMock(return_value=None)
        adapter = _WorkflowLifecycleAdapter(engine)

        await adapter.start()
        engine.startup.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_start_no_startup_method(self) -> None:
        """Adapter.start() should be safe if engine lacks startup()."""
        engine = MagicMock(spec=[])  # No startup attribute
        adapter = _WorkflowLifecycleAdapter(engine)

        await adapter.start()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_is_noop(self) -> None:
        """Adapter.stop() should be a no-op (WorkflowEngine has no shutdown)."""
        engine = MagicMock()
        adapter = _WorkflowLifecycleAdapter(engine)

        await adapter.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_health_check_without_method(self) -> None:
        """Adapter.health_check() should return True when engine has no health_check."""
        engine = MagicMock(spec=[])  # No health_check attribute
        adapter = _WorkflowLifecycleAdapter(engine)

        result = await adapter.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_delegates_to_engine(self) -> None:
        """Adapter.health_check() should delegate to engine.health_check() when available."""
        engine = AsyncMock()
        engine.health_check = AsyncMock(return_value=True)
        adapter = _WorkflowLifecycleAdapter(engine)

        result = await adapter.health_check()
        assert result is True
        engine.health_check.assert_awaited_once()


# ---------------------------------------------------------------------------
# CI guard: all brick_dict keys must be accounted for
# ---------------------------------------------------------------------------


class TestBrickDictCoverage:
    """Ensure every key returned by _boot_brick_services is either in
    _FACTORY_BRICKS, handled specially (workflow_engine), or in _FACTORY_SKIP.

    This test **fails CI** when a developer adds a new brick to
    _boot_brick_services() without adding it to _FACTORY_BRICKS or _FACTORY_SKIP.
    """

    def test_all_brick_dict_keys_accounted_for(self) -> None:
        """Every key in _boot_brick_services result must be covered."""
        # Build a minimal _BootContext that won't actually create services
        # — we only need the result dict keys.  Inspect the source instead.
        import ast
        import inspect

        from nexus.factory import _boot_brick_services

        source = inspect.getsource(_boot_brick_services)
        tree = ast.parse(source)

        # Find the ``result = { ... }`` dict literal
        dict_keys: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "result"
                and isinstance(node.value, ast.Dict)
            ):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        dict_keys.add(key.value)

        assert dict_keys, "Could not parse brick_dict keys from _boot_brick_services"

        registered = {name for name, _, _ in _FACTORY_BRICKS}
        registered.add("workflow_engine")  # special-cased with adapter
        known = registered | _FACTORY_SKIP

        unaccounted = dict_keys - known
        assert not unaccounted, (
            f"New brick_dict key(s) {unaccounted} found in _boot_brick_services() "
            f"but not in _FACTORY_BRICKS or _FACTORY_SKIP. "
            f"Add each new brick to _FACTORY_BRICKS (if it should be lifecycle-managed) "
            f"or _FACTORY_SKIP (if it should be excluded)."
        )
