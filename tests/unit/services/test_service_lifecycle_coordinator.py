"""Unit tests for ServiceLifecycleCoordinator (Issue #1452 Phase 3, #1577)."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import BrickState
from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import HotSwappable, PersistentService
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
    """Simple static service stub (NOT HotSwappable)."""

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


class _FakeServiceV2:
    """V2 static replacement (NOT HotSwappable)."""

    def glob(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]

    def grep(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]


class _HotSwappableService:
    """HotSwappable service stub — satisfies the Protocol structurally."""

    def __init__(self, hook_spec_value: HookSpec | None = None) -> None:
        self._hook_spec = hook_spec_value or HookSpec()
        self.drained = False
        self.activated = False

    def hook_spec(self) -> HookSpec:
        return self._hook_spec

    async def drain(self) -> None:
        self.drained = True

    async def activate(self) -> None:
        self.activated = True

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


class _HotSwappableServiceV2(_HotSwappableService):
    """V2 HotSwappable replacement."""

    def glob(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]

    def grep(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]


class _PersistentFakeService:
    """PersistentService stub — satisfies the Protocol structurally."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def do_work(self) -> str:
        return "working"


class _BothProtocolsService:
    """Q4: HotSwappable + PersistentService — satisfies both protocols."""

    def __init__(self, hook_spec_value: HookSpec | None = None) -> None:
        self._hook_spec = hook_spec_value or HookSpec()
        self.drained = False
        self.activated = False
        self.started = False
        self.stopped = False

    def hook_spec(self) -> HookSpec:
        return self._hook_spec

    async def drain(self) -> None:
        self.drained = True

    async def activate(self) -> None:
        self.activated = True

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


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
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        coordinator.register_service("search", svc1, exports=("glob",), hook_spec=spec1)
        await coordinator.mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _HotSwappableServiceV2(hook_spec_value=spec2)
        await coordinator.swap_service("search", svc2, exports=("glob", "grep"), hook_spec=spec2)

        # New instance is served
        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

        # Old hooks removed, new hooks registered
        assert dispatch.read_hook_count == 1
        assert hook2 in dispatch._read_hooks
        assert hook1 not in dispatch._read_hooks

        # Protocol methods called
        assert svc1.drained is True
        assert svc2.activated is True

    @pytest.mark.asyncio()
    async def test_swap_no_none_window(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
    ) -> None:
        """Verify that service(name) NEVER returns None during swap."""
        svc1 = _HotSwappableService()
        coordinator.register_service("search", svc1, exports=("glob",))
        await coordinator.mount_service("search")

        assert registry.service("search") is not None

        svc2 = _HotSwappableServiceV2()
        await coordinator.swap_service("search", svc2, exports=("glob",))

        ref = registry.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio()
    async def test_swap_rejects_non_hot_swappable(
        self,
        coordinator: ServiceLifecycleCoordinator,
    ) -> None:
        """Static (non-HotSwappable) services cannot be hot-swapped."""
        svc1 = _FakeService()  # NOT HotSwappable
        coordinator.register_service("search", svc1, exports=("glob",))
        await coordinator.mount_service("search")

        svc2 = _FakeServiceV2()
        with pytest.raises(TypeError, match="not HotSwappable"):
            await coordinator.swap_service("search", svc2, exports=("glob",))

    @pytest.mark.asyncio()
    async def test_swap_auto_detects_hook_spec_from_protocol(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """If no explicit hook_spec param, coordinator reads it from HotSwappable.hook_spec()."""
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        # Register WITH explicit hook_spec (retroactive capture)
        coordinator.register_service("search", svc1, hook_spec=spec1)
        await coordinator.mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _HotSwappableServiceV2(hook_spec_value=spec2)
        # Swap WITHOUT explicit hook_spec — coordinator auto-detects from protocol
        await coordinator.swap_service("search", svc2)

        assert dispatch.read_hook_count == 1
        assert hook2 in dispatch._read_hooks
        assert hook1 not in dispatch._read_hooks

    @pytest.mark.asyncio()
    async def test_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
    ) -> None:
        """Verify swap waits for in-flight async calls to complete."""
        call_completed = asyncio.Event()

        class _SlowHotSwappable(_HotSwappableService):
            async def glob(self, pattern: str) -> list[str]:
                await asyncio.sleep(0.05)
                call_completed.set()
                return [pattern]

        svc1 = _SlowHotSwappable()
        coordinator.register_service("search", svc1, exports=("glob",))
        await coordinator.mount_service("search")

        # Start an in-flight call via ServiceRef
        ref = registry.service("search")
        assert ref is not None
        in_flight = asyncio.create_task(ref.glob("*.py"))

        # Swap should wait for the in-flight call to drain
        svc2 = _HotSwappableServiceV2()
        swap_task = asyncio.create_task(
            coordinator.swap_service("search", svc2, exports=("glob",), drain_timeout=2.0)
        )

        result = await in_flight
        assert result == ["*.py"]
        assert call_completed.is_set()

        await swap_task
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


# ---------------------------------------------------------------------------
# swap with multi-channel HookSpec (Issue #1452 Phase 4)
# ---------------------------------------------------------------------------


class TestSwapWithFullHookSpec:
    """Verify swap_service() correctly handles multi-channel HookSpecs."""

    @pytest.mark.asyncio()
    async def test_swap_unregisters_old_hooks_registers_new(
        self,
        coordinator: ServiceLifecycleCoordinator,
        registry: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """Multi-channel spec: old hooks removed, new hooks installed on same channels."""
        old_read = MagicMock()
        old_write = MagicMock()
        old_observer = MagicMock()
        spec1 = HookSpec(
            read_hooks=(old_read,),
            write_hooks=(old_write,),
            observers=(old_observer,),
        )
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        coordinator.register_service("rebac", svc1, hook_spec=spec1)
        await coordinator.mount_service("rebac")

        assert dispatch.read_hook_count == 1
        assert dispatch.write_hook_count == 1
        assert dispatch.observer_count == 1

        new_read = MagicMock()
        new_write = MagicMock()
        new_observer = MagicMock()
        spec2 = HookSpec(
            read_hooks=(new_read,),
            write_hooks=(new_write,),
            observers=(new_observer,),
        )
        svc2 = _HotSwappableServiceV2(hook_spec_value=spec2)
        await coordinator.swap_service("rebac", svc2, hook_spec=spec2)

        # Counts unchanged (old removed, new added)
        assert dispatch.read_hook_count == 1
        assert dispatch.write_hook_count == 1
        assert dispatch.observer_count == 1

        # Identity check — new hooks, not old
        assert new_read in dispatch._read_hooks
        assert old_read not in dispatch._read_hooks
        assert new_write in dispatch._write_hooks
        assert old_write not in dispatch._write_hooks

    @pytest.mark.asyncio()
    async def test_swap_with_no_new_spec_clears_old(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        """Swap without new hook_spec should unregister old hooks and leave none."""
        old_hook = MagicMock()
        spec1 = HookSpec(read_hooks=(old_hook,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        coordinator.register_service("parser", svc1, hook_spec=spec1)
        await coordinator.mount_service("parser")
        assert dispatch.read_hook_count == 1

        svc2 = _HotSwappableServiceV2()  # empty hook_spec
        await coordinator.swap_service("parser", svc2)

        # Old hook removed, no new hook registered
        assert dispatch.read_hook_count == 0
        assert old_hook not in dispatch._read_hooks


# ---------------------------------------------------------------------------
# Protocol conformance — isinstance checks (Issue #1577)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify structural subtyping works for HotSwappable and PersistentService."""

    def test_hot_swappable_detected(self) -> None:
        svc = _HotSwappableService()
        assert isinstance(svc, HotSwappable)

    def test_static_service_not_hot_swappable(self) -> None:
        svc = _FakeService()
        assert not isinstance(svc, HotSwappable)

    def test_persistent_service_detected(self) -> None:
        svc = _PersistentFakeService()
        assert isinstance(svc, PersistentService)

    def test_static_service_not_persistent(self) -> None:
        svc = _FakeService()
        assert not isinstance(svc, PersistentService)

    def test_hot_swappable_not_persistent(self) -> None:
        """HotSwappable and PersistentService are independent protocols."""
        svc = _HotSwappableService()
        assert isinstance(svc, HotSwappable)
        assert not isinstance(svc, PersistentService)

    def test_persistent_not_hot_swappable(self) -> None:
        svc = _PersistentFakeService()
        assert isinstance(svc, PersistentService)
        assert not isinstance(svc, HotSwappable)


# ---------------------------------------------------------------------------
# Distro classification (Issue #1577)
# ---------------------------------------------------------------------------


class TestDistroClassification:
    def test_no_persistent_services(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """All static services → invocation-compatible distro."""
        svc = _FakeService()
        coordinator.register_service("search", svc)

        is_persistent, names = coordinator.classify_distro()
        assert is_persistent is False
        assert names == []

    def test_persistent_service_detected(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """PersistentService present → persistent distro."""
        svc = _PersistentFakeService()
        coordinator.register_service("delivery_worker", svc)

        is_persistent, names = coordinator.classify_distro()
        assert is_persistent is True
        assert names == ["delivery_worker"]

    def test_mixed_services(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """Mix of static and persistent → persistent distro."""
        coordinator.register_service("search", _FakeService())
        coordinator.register_service("worker", _PersistentFakeService())

        is_persistent, names = coordinator.classify_distro()
        assert is_persistent is True
        assert names == ["worker"]

    def test_classify_hot_swappable(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """Classify services into hot-swappable vs static."""
        coordinator.register_service("search", _FakeService())
        coordinator.register_service("rebac", _HotSwappableService())
        coordinator.register_service("worker", _PersistentFakeService())

        hot, static = coordinator.classify_hot_swappable()
        assert "rebac" in hot
        assert "search" in static
        assert "worker" in static  # persistent but not hot-swappable


# ---------------------------------------------------------------------------
# Auto-lifecycle — four-quadrant "one-click" management (Issue #1580)
# ---------------------------------------------------------------------------


class TestAutoLifecyclePersistentService:
    """Auto start/stop for PersistentService (Q3 + Q4)."""

    @pytest.mark.asyncio()
    async def test_start_calls_start_on_persistent(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        svc = _PersistentFakeService()
        coordinator.register_service("worker", svc)
        started = await coordinator.start_persistent_services()
        assert started == ["worker"]
        assert svc.started is True

    @pytest.mark.asyncio()
    async def test_start_skips_non_persistent(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        coordinator.register_service("search", _FakeService())
        started = await coordinator.start_persistent_services()
        assert started == []

    @pytest.mark.asyncio()
    async def test_stop_calls_stop_on_persistent(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        svc = _PersistentFakeService()
        coordinator.register_service("worker", svc)
        stopped = await coordinator.stop_persistent_services()
        assert stopped == ["worker"]
        assert svc.stopped is True

    @pytest.mark.asyncio()
    async def test_start_handles_exception(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """Exception during start() logs error, continues to next service."""

        class _FailStart:
            async def start(self) -> None:
                raise RuntimeError("boom")

            async def stop(self) -> None:
                pass

        ok_svc = _PersistentFakeService()
        coordinator.register_service("fail", _FailStart())
        coordinator.register_service("ok", ok_svc)
        started = await coordinator.start_persistent_services()
        assert "ok" in started
        assert "fail" not in started
        assert ok_svc.started is True

    @pytest.mark.asyncio()
    async def test_stop_handles_exception(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """Exception during stop() logs error, continues."""

        class _FailStop:
            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                raise RuntimeError("boom")

        ok_svc = _PersistentFakeService()
        coordinator.register_service("fail", _FailStop())
        coordinator.register_service("ok", ok_svc)
        stopped = await coordinator.stop_persistent_services()
        assert "ok" in stopped
        assert "fail" not in stopped
        assert ok_svc.stopped is True

    @pytest.mark.asyncio()
    async def test_start_handles_timeout(self, coordinator: ServiceLifecycleCoordinator) -> None:
        """Timeout during start() logs error, continues."""

        class _SlowStart:
            async def start(self) -> None:
                await asyncio.sleep(10)

            async def stop(self) -> None:
                pass

        ok_svc = _PersistentFakeService()
        coordinator.register_service("slow", _SlowStart())
        coordinator.register_service("ok", ok_svc)
        started = await coordinator.start_persistent_services(timeout=0.01)
        assert "ok" in started
        assert "slow" not in started

    @pytest.mark.asyncio()
    async def test_start_stop_idempotent(self, coordinator: ServiceLifecycleCoordinator) -> None:
        svc = _PersistentFakeService()
        coordinator.register_service("worker", svc)
        await coordinator.start_persistent_services()
        await coordinator.start_persistent_services()
        assert svc.started is True
        await coordinator.stop_persistent_services()
        await coordinator.stop_persistent_services()
        assert svc.stopped is True


class TestAutoLifecycleHotSwappable:
    """Auto activate/deactivate for HotSwappable (Q2 + Q4)."""

    @pytest.mark.asyncio()
    async def test_activate_registers_hooks_and_calls_activate(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        coordinator.register_service("rebac", svc)
        activated = await coordinator.activate_hot_swappable_services()
        assert activated == ["rebac"]
        assert svc.activated is True
        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_activate_auto_captures_hook_spec_from_protocol(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        """hook_spec is auto-detected from HotSwappable.hook_spec() if not set explicitly."""
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        # Register WITHOUT explicit hook_spec — should auto-capture
        coordinator.register_service("rebac", svc)
        assert coordinator.get_hook_spec("rebac") is None
        await coordinator.activate_hot_swappable_services()
        assert coordinator.get_hook_spec("rebac") is spec
        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_activate_skips_non_hot_swappable(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        coordinator.register_service("search", _FakeService())
        activated = await coordinator.activate_hot_swappable_services()
        assert activated == []

    @pytest.mark.asyncio()
    async def test_deactivate_drains_and_unregisters_hooks(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        coordinator.register_service("rebac", svc, hook_spec=spec)
        await coordinator.activate_hot_swappable_services()
        assert dispatch.read_hook_count == 1

        deactivated = await coordinator.deactivate_hot_swappable_services()
        assert deactivated == ["rebac"]
        assert svc.drained is True
        assert dispatch.read_hook_count == 0

    @pytest.mark.asyncio()
    async def test_activate_handles_exception(
        self, coordinator: ServiceLifecycleCoordinator
    ) -> None:
        class _FailActivate:
            def hook_spec(self) -> HookSpec:
                return HookSpec()

            async def drain(self) -> None:
                pass

            async def activate(self) -> None:
                raise RuntimeError("boom")

        ok_svc = _HotSwappableService()
        coordinator.register_service("fail", _FailActivate())
        coordinator.register_service("ok", ok_svc)
        activated = await coordinator.activate_hot_swappable_services()
        assert "ok" in activated
        assert "fail" not in activated


class TestAutoLifecycleQ4BothProtocols:
    """Q4: HotSwappable + PersistentService — both protocols auto-managed."""

    @pytest.mark.asyncio()
    async def test_q4_activate_and_start(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _BothProtocolsService(hook_spec_value=spec)
        coordinator.register_service("q4svc", svc)

        activated = await coordinator.activate_hot_swappable_services()
        started = await coordinator.start_persistent_services()

        assert activated == ["q4svc"]
        assert started == ["q4svc"]
        assert svc.activated is True
        assert svc.started is True
        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_q4_stop_and_deactivate(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _BothProtocolsService(hook_spec_value=spec)
        coordinator.register_service("q4svc", svc)
        await coordinator.activate_hot_swappable_services()
        await coordinator.start_persistent_services()

        stopped = await coordinator.stop_persistent_services()
        deactivated = await coordinator.deactivate_hot_swappable_services()

        assert stopped == ["q4svc"]
        assert deactivated == ["q4svc"]
        assert svc.stopped is True
        assert svc.drained is True
        assert dispatch.read_hook_count == 0

    @pytest.mark.asyncio()
    async def test_q4_mixed_with_other_quadrants(
        self,
        coordinator: ServiceLifecycleCoordinator,
        dispatch: KernelDispatch,
    ) -> None:
        """All four quadrants coexist — each gets its appropriate lifecycle."""
        # Q1: static + invocation
        q1 = _FakeService()
        coordinator.register_service("q1_search", q1)

        # Q2: hot-swappable + invocation
        q2_hook = MagicMock()
        q2 = _HotSwappableService(hook_spec_value=HookSpec(read_hooks=(q2_hook,)))
        coordinator.register_service("q2_rebac", q2)

        # Q3: static + persistent
        q3 = _PersistentFakeService()
        coordinator.register_service("q3_worker", q3)

        # Q4: both
        q4_hook = MagicMock()
        q4 = _BothProtocolsService(hook_spec_value=HookSpec(observers=(q4_hook,)))
        coordinator.register_service("q4_full", q4)

        # --- Bootstrap ---
        activated = await coordinator.activate_hot_swappable_services()
        started = await coordinator.start_persistent_services()

        assert sorted(activated) == ["q2_rebac", "q4_full"]
        assert sorted(started) == ["q3_worker", "q4_full"]
        assert q2.activated is True
        assert q3.started is True
        assert q4.activated is True
        assert q4.started is True
        assert dispatch.read_hook_count == 1  # q2
        assert dispatch.observer_count == 1  # q4

        # --- Shutdown ---
        stopped = await coordinator.stop_persistent_services()
        deactivated = await coordinator.deactivate_hot_swappable_services()

        assert sorted(stopped) == ["q3_worker", "q4_full"]
        assert sorted(deactivated) == ["q2_rebac", "q4_full"]
        assert q3.stopped is True
        assert q4.stopped is True
        assert q4.drained is True
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0
