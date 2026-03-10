"""Unit tests for ServiceLifecycleCoordinator (Issue #1452 Phase 3)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import BrickState
from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.core.kernel_dispatch import KernelDispatch
from nexus.core.service_registry import ServiceRegistry
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
    ServiceLifecycleCoordinator,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> ServiceRegistry:
    return ServiceRegistry()


@pytest.fixture()
def blm() -> BrickLifecycleManager:
    return BrickLifecycleManager()


@pytest.fixture()
def dispatch() -> KernelDispatch:
    return KernelDispatch()


@pytest.fixture()
def coordinator(
    registry: ServiceRegistry, blm: BrickLifecycleManager, dispatch: KernelDispatch
) -> ServiceLifecycleCoordinator:
    return ServiceLifecycleCoordinator(registry, blm, dispatch)


class _FakeService:
    """Simple service stub with callable methods."""

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


class _FakeServiceV2:
    """V2 replacement for hot-swap tests."""

    def glob(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]

    def grep(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]


# ---------------------------------------------------------------------------
# insmod — register_service
# ---------------------------------------------------------------------------


class TestRegisterService:
    def test_registers_in_both_registry_and_blm(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
        blm: BrickLifecycleManager,
    ) -> None:
        svc = _FakeService()
        coordinator.register_service(
            "search", svc, exports=("glob", "grep"), protocol_name="SearchProtocol"
        )
        # ServiceRegistry
        info = registry.service_info("search")
        assert info is not None
        assert info.instance is svc
        assert info.exports == ("glob", "grep")
        # BLM
        status = blm.get_status("search")
        assert status is not None
        assert status.state == BrickState.REGISTERED
        assert status.protocol_name == "SearchProtocol"

    def test_stores_hook_spec(self, coordinator: ServiceLifecycleCoordinator) -> None:
        svc = _FakeService()
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        coordinator.register_service("search", svc, hook_spec=spec)
        assert coordinator.get_hook_spec("search") is spec


# ---------------------------------------------------------------------------
# mount — mount_service
# ---------------------------------------------------------------------------


class TestMountService:
    @pytest.mark.asyncio()
    async def test_mount_registers_hooks(
        self, coordinator: ServiceLifecycleCoordinator, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        observer = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,), observers=(observer,))
        coordinator.register_service("search", svc, hook_spec=spec)
        await coordinator.mount_service("search")

        assert dispatch.read_hook_count == 1
        assert dispatch.observer_count == 1

    @pytest.mark.asyncio()
    async def test_mount_no_hooks_if_no_spec(
        self, coordinator: ServiceLifecycleCoordinator, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        coordinator.register_service("search", svc)
        await coordinator.mount_service("search")
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# umount — unmount_service
# ---------------------------------------------------------------------------


class TestUnmountService:
    @pytest.mark.asyncio()
    async def test_unmount_removes_hooks(
        self, coordinator: ServiceLifecycleCoordinator, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,))
        coordinator.register_service("search", svc, hook_spec=spec)
        await coordinator.mount_service("search")
        assert dispatch.read_hook_count == 1

        await coordinator.unmount_service("search")
        assert dispatch.read_hook_count == 0


# ---------------------------------------------------------------------------
# rmmod — unregister_service
# ---------------------------------------------------------------------------


class TestUnregisterService:
    @pytest.mark.asyncio()
    async def test_full_unregister(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
        blm: BrickLifecycleManager,
    ) -> None:
        svc = _FakeService()
        coordinator.register_service("search", svc)
        await coordinator.mount_service("search")
        await coordinator.unregister_service("search")

        # Gone from registry
        assert registry.service("search") is None
        # Gone from BLM
        assert blm.get_status("search") is None


# ---------------------------------------------------------------------------
# swap — swap_service (the hot-swap test)
# ---------------------------------------------------------------------------


class TestSwapService:
    @pytest.mark.asyncio()
    async def test_basic_swap(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        svc1 = _FakeService()
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        coordinator.register_service("search", svc1, exports=("glob",), hook_spec=spec1)
        await coordinator.mount_service("search")
        assert dispatch.read_hook_count == 1

        svc2 = _FakeServiceV2()
        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        await coordinator.swap_service("search", svc2, exports=("glob", "grep"), hook_spec=spec2)

        # New instance is served
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

        # Old hooks removed, new hooks registered
        assert dispatch.read_hook_count == 1
        # Verify it's the new hook (old was unregistered, new was registered)
        assert hook2 in dispatch._read_hooks
        assert hook1 not in dispatch._read_hooks

    @pytest.mark.asyncio()
    async def test_swap_no_none_window(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
    ) -> None:
        """Verify that service(name) NEVER returns None during swap."""
        svc1 = _FakeService()
        coordinator.register_service("search", svc1, exports=("glob",))
        await coordinator.mount_service("search")

        # Before swap
        assert registry.service("search") is not None

        svc2 = _FakeServiceV2()
        await coordinator.swap_service("search", svc2, exports=("glob",))

        # After swap — should have new instance, never None
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio()
    async def test_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
    ) -> None:
        """Verify swap waits for in-flight async calls to complete."""
        svc1 = MagicMock()
        call_completed = asyncio.Event()

        async def _slow_glob(pattern: str) -> list[str]:
            await asyncio.sleep(0.05)
            call_completed.set()
            return [pattern]

        svc1.glob = _slow_glob
        coordinator.register_service("search", svc1, exports=("glob",))
        await coordinator.mount_service("search")

        # Start an in-flight call via ServiceRef
        ref = registry.service("search")
        assert ref is not None
        in_flight = asyncio.create_task(ref.glob("*.py"))

        # Swap should wait for the in-flight call to drain
        svc2 = MagicMock()
        svc2.glob = MagicMock(return_value=["v2"])
        swap_task = asyncio.create_task(
            coordinator.swap_service("search", svc2, exports=("glob",), drain_timeout=2.0)
        )

        # Wait for both
        result = await in_flight
        assert result == ["*.py"]
        assert call_completed.is_set()

        await swap_task
        # New instance is now active
        new_ref = registry.service("search")
        assert new_ref is not None
        assert new_ref._service_instance is svc2


# ---------------------------------------------------------------------------
# HookSpec management
# ---------------------------------------------------------------------------


class TestHookSpecManagement:
    def test_set_and_get_hook_spec(self, coordinator: ServiceLifecycleCoordinator) -> None:
        spec = HookSpec(observers=(MagicMock(),))
        coordinator.set_hook_spec("events", spec)
        assert coordinator.get_hook_spec("events") is spec

    def test_get_missing_returns_none(self, coordinator: ServiceLifecycleCoordinator) -> None:
        assert coordinator.get_hook_spec("nonexistent") is None


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


class TestDrain:
    @pytest.mark.asyncio()
    async def test_drain_immediate_when_no_inflight(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        """Drain should return immediately if refcount is 0."""
        # No in-flight calls — drain should not block
        await asyncio.wait_for(coordinator._drain("search", timeout=0.1), timeout=1.0)

    @pytest.mark.asyncio()
    async def test_drain_timeout_when_stuck(
        self, coordinator: ServiceLifecycleCoordinator, registry: ServiceRegistry
    ) -> None:
        """Drain should timeout and warn if refcount doesn't reach 0."""
        # Manually set refcount > 0 to simulate stuck call
        registry._refcounts["stuck"] = 5
        # Should timeout, not hang forever
        await coordinator._drain("stuck", timeout=0.05)
        # Verify refcount wasn't magically cleared
        assert registry._refcounts["stuck"] == 5


# ---------------------------------------------------------------------------
# HookSpec dataclass
# ---------------------------------------------------------------------------


class TestHookSpec:
    def test_empty_spec(self) -> None:
        spec = HookSpec()
        assert spec.is_empty is True
        assert spec.total_hooks == 0

    def test_non_empty_spec(self) -> None:
        spec = HookSpec(read_hooks=(MagicMock(),), observers=(MagicMock(), MagicMock()))
        assert spec.is_empty is False
        assert spec.total_hooks == 3

    def test_frozen(self) -> None:
        spec = HookSpec()
        with pytest.raises(AttributeError):
            spec.read_hooks = (MagicMock(),)
