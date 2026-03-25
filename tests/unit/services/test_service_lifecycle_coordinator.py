"""Unit tests for ServiceRegistry lifecycle orchestration (Issue #1452 Phase 3, #1577, #1814).

These tests exercise the lifecycle methods (enlist, swap_service, start/stop,
activate/deactivate) that were merged from ServiceLifecycleCoordinator into
ServiceRegistry in Issue #1814.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import HotSwappable, PersistentService
from nexus.core.kernel_dispatch import KernelDispatch
from nexus.core.service_registry import ServiceRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> ServiceRegistry:
    return ServiceRegistry()


@pytest.fixture()
def dispatch() -> KernelDispatch:
    return KernelDispatch()


@pytest.fixture()
def coordinator(dispatch: KernelDispatch) -> ServiceRegistry:
    return ServiceRegistry(dispatch=dispatch)


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
# insmod — _register_service
# ---------------------------------------------------------------------------


class TestRegisterService:
    def test_registers_in_registry(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        svc = _FakeService()
        coordinator._register_service("search", svc, exports=("glob", "grep"))
        info = coordinator.service_info("search")
        assert info is not None
        assert info.instance is svc
        assert info.exports == ("glob", "grep")

    def test_stores_hook_spec(self, coordinator: ServiceRegistry) -> None:
        svc = _FakeService()
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        coordinator._register_service("search", svc)
        coordinator._set_hook_spec("search", spec)
        assert coordinator._get_hook_spec("search") is spec


# ---------------------------------------------------------------------------
# mount — _mount_service
# ---------------------------------------------------------------------------


class TestMountService:
    @pytest.mark.asyncio()
    async def test_mount_registers_hooks(
        self, coordinator: ServiceRegistry, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        observer = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,), observers=(observer,))
        coordinator._register_service("search", svc)
        coordinator._set_hook_spec("search", spec)
        await coordinator._mount_service("search")

        assert dispatch.read_hook_count == 1
        assert dispatch.observer_count == 1

    @pytest.mark.asyncio()
    async def test_mount_no_hooks_if_no_spec(
        self, coordinator: ServiceRegistry, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        coordinator._register_service("search", svc)
        await coordinator._mount_service("search")
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# umount — _unmount_service
# ---------------------------------------------------------------------------


class TestUnmountService:
    @pytest.mark.asyncio()
    async def test_unmount_removes_hooks(
        self, coordinator: ServiceRegistry, dispatch: KernelDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,))
        coordinator._register_service("search", svc)
        coordinator._set_hook_spec("search", spec)
        await coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        await coordinator._unmount_service("search")
        assert dispatch.read_hook_count == 0


# ---------------------------------------------------------------------------
# rmmod — unregister_service_full
# ---------------------------------------------------------------------------


class TestUnregisterServiceFull:
    @pytest.mark.asyncio()
    async def test_full_unregister(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        svc = _FakeService()
        coordinator._register_service("search", svc)
        await coordinator._mount_service("search")
        await coordinator.unregister_service_full("search")

        # Gone from registry
        assert coordinator.service("search") is None


# ---------------------------------------------------------------------------
# swap — swap_service (the hot-swap test)
# ---------------------------------------------------------------------------


class TestSwapService:
    @pytest.mark.asyncio()
    async def test_basic_swap(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._set_hook_spec("search", spec1)
        await coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _HotSwappableServiceV2(hook_spec_value=spec2)
        await coordinator.swap_service("search", svc2, exports=("glob", "grep"), hook_spec=spec2)

        # New instance is served
        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

        # Old hooks removed, new hooks registered
        assert dispatch.read_hook_count == 1

        # Protocol methods called
        assert svc1.drained is True
        assert svc2.activated is True

    @pytest.mark.asyncio()
    async def test_swap_no_none_window(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Verify that service(name) NEVER returns None during swap."""
        svc1 = _HotSwappableService()
        coordinator._register_service("search", svc1, exports=("glob",))
        await coordinator._mount_service("search")

        assert coordinator.service("search") is not None

        svc2 = _HotSwappableServiceV2()
        await coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio()
    async def test_swap_allows_non_hot_swappable(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 services can be swapped via refcount drain (#1452)."""
        svc1 = _FakeService()  # NOT HotSwappable
        coordinator._register_service("search", svc1, exports=("glob",))
        await coordinator._mount_service("search")

        svc2 = _FakeServiceV2()
        await coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio()
    async def test_swap_auto_detects_hook_spec_from_protocol(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """If no explicit hook_spec param, coordinator reads it from HotSwappable.hook_spec()."""
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        # Register then set hook_spec separately (retroactive capture)
        coordinator._register_service("search", svc1)
        coordinator._set_hook_spec("search", spec1)
        await coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _HotSwappableServiceV2(hook_spec_value=spec2)
        # Swap WITHOUT explicit hook_spec — coordinator auto-detects from protocol
        await coordinator.swap_service("search", svc2)

        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Verify swap waits for in-flight async calls to complete."""
        call_completed = asyncio.Event()

        class _SlowHotSwappable(_HotSwappableService):
            async def glob(self, pattern: str) -> list[str]:
                await asyncio.sleep(0.05)
                call_completed.set()
                return [pattern]

        svc1 = _SlowHotSwappable()
        coordinator._register_service("search", svc1, exports=("glob",))
        await coordinator._mount_service("search")

        # Start an in-flight call via ServiceRef
        ref = coordinator.service("search")
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
        new_ref = coordinator.service("search")
        assert new_ref is not None
        assert new_ref._service_instance is svc2


# ---------------------------------------------------------------------------
# HookSpec management
# ---------------------------------------------------------------------------


class TestHookSpecManagement:
    def test_set_and_get_hook_spec(self, coordinator: ServiceRegistry) -> None:
        spec = HookSpec(observers=(MagicMock(),))
        coordinator._set_hook_spec("events", spec)
        assert coordinator._get_hook_spec("events") is spec

    def test_get_missing_returns_none(self, coordinator: ServiceRegistry) -> None:
        assert coordinator._get_hook_spec("nonexistent") is None


# ---------------------------------------------------------------------------
# Drain
# ---------------------------------------------------------------------------


class TestDrain:
    @pytest.mark.asyncio()
    async def test_drain_immediate_when_no_inflight(self, coordinator: ServiceRegistry) -> None:
        """Drain should return immediately if refcount is 0."""
        # No in-flight calls — drain should not block
        await asyncio.wait_for(coordinator._drain("search", timeout=0.1), timeout=1.0)

    @pytest.mark.asyncio()
    async def test_drain_timeout_when_stuck(self, coordinator: ServiceRegistry) -> None:
        """Drain should timeout and warn if refcount doesn't reach 0."""
        # Manually set refcount > 0 to simulate stuck call
        coordinator._refcounts["stuck"] = 5
        # Should timeout, not hang forever
        await coordinator._drain("stuck", timeout=0.05)
        # Verify refcount wasn't magically cleared
        assert coordinator._refcounts["stuck"] == 5


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
        coordinator: ServiceRegistry,
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
        coordinator._register_service("rebac", svc1)
        coordinator._set_hook_spec("rebac", spec1)
        await coordinator._mount_service("rebac")

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

    @pytest.mark.asyncio()
    async def test_swap_with_no_new_spec_clears_old(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """Swap without new hook_spec should unregister old hooks and leave none."""
        old_hook = MagicMock()
        spec1 = HookSpec(read_hooks=(old_hook,))
        svc1 = _HotSwappableService(hook_spec_value=spec1)
        coordinator._register_service("parser", svc1)
        coordinator._set_hook_spec("parser", spec1)
        await coordinator._mount_service("parser")
        assert dispatch.read_hook_count == 1

        svc2 = _HotSwappableServiceV2()  # empty hook_spec
        await coordinator.swap_service("parser", svc2)

        # Old hook removed, no new hook registered
        assert dispatch.read_hook_count == 0


# ---------------------------------------------------------------------------
# Protocol conformance — isinstance checks (Issue #1577)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify structural subtyping works for HotSwappable and PersistentService."""

    @pytest.mark.parametrize(
        "service_class,protocol,expected",
        [
            (_HotSwappableService, HotSwappable, True),
            (_FakeService, HotSwappable, False),
            (_PersistentFakeService, PersistentService, True),
            (_FakeService, PersistentService, False),
            # HotSwappable and PersistentService are independent protocols
            (_HotSwappableService, PersistentService, False),
            (_PersistentFakeService, HotSwappable, False),
        ],
    )
    def test_protocol_conformance(
        self, service_class: type, protocol: type, expected: bool
    ) -> None:
        """Test protocol detection for services."""
        svc = service_class()
        assert isinstance(svc, protocol) is expected


# ---------------------------------------------------------------------------
# Auto-lifecycle — four-quadrant "one-click" management (Issue #1580)
# ---------------------------------------------------------------------------


class TestAutoLifecyclePersistentService:
    """Auto start/stop for PersistentService (Q3 + Q4)."""

    @pytest.mark.asyncio()
    async def test_start_calls_start_on_persistent(self, coordinator: ServiceRegistry) -> None:
        svc = _PersistentFakeService()
        coordinator._register_service("worker", svc)
        started = await coordinator.start_persistent_services()
        assert started == ["worker"]
        assert svc.started is True

    @pytest.mark.asyncio()
    async def test_start_skips_non_persistent(self, coordinator: ServiceRegistry) -> None:
        coordinator._register_service("search", _FakeService())
        started = await coordinator.start_persistent_services()
        assert started == []

    @pytest.mark.asyncio()
    async def test_stop_calls_stop_on_persistent(self, coordinator: ServiceRegistry) -> None:
        svc = _PersistentFakeService()
        coordinator._register_service("worker", svc)
        stopped = await coordinator.stop_persistent_services()
        assert stopped == ["worker"]
        assert svc.stopped is True

    @pytest.mark.asyncio()
    async def test_start_handles_exception(self, coordinator: ServiceRegistry) -> None:
        """Exception during start() logs error, continues to next service."""

        class _FailStart:
            async def start(self) -> None:
                raise RuntimeError("boom")

            async def stop(self) -> None:
                pass

        ok_svc = _PersistentFakeService()
        coordinator._register_service("fail", _FailStart())
        coordinator._register_service("ok", ok_svc)
        started = await coordinator.start_persistent_services()
        assert "ok" in started
        assert "fail" not in started
        assert ok_svc.started is True

    @pytest.mark.asyncio()
    async def test_stop_handles_exception(self, coordinator: ServiceRegistry) -> None:
        """Exception during stop() logs error, continues."""

        class _FailStop:
            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                raise RuntimeError("boom")

        ok_svc = _PersistentFakeService()
        coordinator._register_service("fail", _FailStop())
        coordinator._register_service("ok", ok_svc)
        stopped = await coordinator.stop_persistent_services()
        assert "ok" in stopped
        assert "fail" not in stopped
        assert ok_svc.stopped is True

    @pytest.mark.asyncio()
    async def test_start_handles_timeout(self, coordinator: ServiceRegistry) -> None:
        """Timeout during start() logs error, continues."""

        class _SlowStart:
            async def start(self) -> None:
                await asyncio.sleep(10)

            async def stop(self) -> None:
                pass

        ok_svc = _PersistentFakeService()
        coordinator._register_service("slow", _SlowStart())
        coordinator._register_service("ok", ok_svc)
        started = await coordinator.start_persistent_services(timeout=0.01)
        assert "ok" in started
        assert "slow" not in started

    @pytest.mark.asyncio()
    async def test_start_stop_idempotent(self, coordinator: ServiceRegistry) -> None:
        svc = _PersistentFakeService()
        coordinator._register_service("worker", svc)
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
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        coordinator._register_service("rebac", svc)
        activated = await coordinator.activate_hot_swappable_services()
        assert activated == ["rebac"]
        assert svc.activated is True
        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_activate_auto_captures_hook_spec_from_protocol(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """hook_spec is auto-detected from HotSwappable.hook_spec() if not set explicitly."""
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        # Register WITHOUT explicit hook_spec — should auto-capture
        coordinator._register_service("rebac", svc)
        assert coordinator._get_hook_spec("rebac") is None
        await coordinator.activate_hot_swappable_services()
        assert coordinator._get_hook_spec("rebac") is spec
        assert dispatch.read_hook_count == 1

    @pytest.mark.asyncio()
    async def test_activate_skips_non_hot_swappable(self, coordinator: ServiceRegistry) -> None:
        coordinator._register_service("search", _FakeService())
        activated = await coordinator.activate_hot_swappable_services()
        assert activated == []

    @pytest.mark.asyncio()
    async def test_deactivate_drains_and_unregisters_hooks(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _HotSwappableService(hook_spec_value=spec)
        coordinator._register_service("rebac", svc)
        coordinator._set_hook_spec("rebac", spec)
        await coordinator.activate_hot_swappable_services()
        assert dispatch.read_hook_count == 1

        deactivated = await coordinator.deactivate_hot_swappable_services()
        assert deactivated == ["rebac"]
        assert svc.drained is True
        assert dispatch.read_hook_count == 0

    @pytest.mark.asyncio()
    async def test_activate_handles_exception(self, coordinator: ServiceRegistry) -> None:
        class _FailActivate:
            def hook_spec(self) -> HookSpec:
                return HookSpec()

            async def drain(self) -> None:
                pass

            async def activate(self) -> None:
                raise RuntimeError("boom")

        ok_svc = _HotSwappableService()
        coordinator._register_service("fail", _FailActivate())
        coordinator._register_service("ok", ok_svc)
        activated = await coordinator.activate_hot_swappable_services()
        assert "ok" in activated
        assert "fail" not in activated


class TestAutoLifecycleQ4BothProtocols:
    """Q4: HotSwappable + PersistentService — both protocols auto-managed."""

    @pytest.mark.asyncio()
    async def test_q4_activate_and_start(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _BothProtocolsService(hook_spec_value=spec)
        coordinator._register_service("q4svc", svc)

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
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc = _BothProtocolsService(hook_spec_value=spec)
        coordinator._register_service("q4svc", svc)
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
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """All four quadrants coexist — each gets its appropriate lifecycle."""
        # Q1: restart-required + on-demand
        q1 = _FakeService()
        coordinator._register_service("q1_search", q1)

        # Q2: hot-swappable + invocation
        q2_hook = MagicMock()
        q2 = _HotSwappableService(hook_spec_value=HookSpec(read_hooks=(q2_hook,)))
        coordinator._register_service("q2_rebac", q2)

        # Q3: static + persistent
        q3 = _PersistentFakeService()
        coordinator._register_service("q3_worker", q3)

        # Q4: both
        q4_hook = MagicMock()
        q4 = _BothProtocolsService(hook_spec_value=HookSpec(observers=(q4_hook,)))
        coordinator._register_service("q4_full", q4)

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


# ---------------------------------------------------------------------------
# enlist — the ONE entry point for all four quadrants (Issue #1502)
# ---------------------------------------------------------------------------


class TestEnlist:
    """Tests for ``reg.enlist()`` — the single entry point for all quadrants."""

    @pytest.mark.asyncio
    async def test_enlist_q1_static(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 service: enlist registers only, no start/activate."""
        svc = _FakeService()
        await coordinator.enlist("q1_svc", svc)

        info = coordinator.service_info("q1_svc")
        assert info is not None
        assert info.instance is svc

    @pytest.mark.asyncio
    async def test_enlist_q3_persistent_pre_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q3 service pre-bootstrap: enlist registers but defers start()."""
        svc = _PersistentFakeService()
        assert svc.started is False

        await coordinator.enlist("q3_svc", svc)

        assert svc.started is False  # deferred — not yet bootstrapped
        info = coordinator.service_info("q3_svc")
        assert info is not None

    @pytest.mark.asyncio
    async def test_enlist_q3_persistent_post_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q3 service post-bootstrap: enlist registers + calls start() immediately."""
        coordinator.mark_bootstrapped()
        svc = _PersistentFakeService()
        assert svc.started is False

        await coordinator.enlist("q3_svc", svc)

        assert svc.started is True
        info = coordinator.service_info("q3_svc")
        assert info is not None

    @pytest.mark.asyncio
    async def test_enlist_q2_hot_swappable(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q2 service: enlist registers + captures hooks + activates."""
        svc = _HotSwappableService()
        assert svc.activated is False

        await coordinator.enlist("q2_svc", svc)

        assert svc.activated is True
        info = coordinator.service_info("q2_svc")
        assert info is not None
        assert coordinator._get_hook_spec("q2_svc") is not None

    @pytest.mark.asyncio
    async def test_enlist_q4_both_pre_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q4 pre-bootstrap: enlist registers + activate but defers start."""
        svc = _BothProtocolsService()
        assert svc.started is False
        assert svc.activated is False

        await coordinator.enlist("q4_svc", svc)

        assert svc.started is False  # deferred — not yet bootstrapped
        assert svc.activated is True  # HotSwappable activate is always immediate
        info = coordinator.service_info("q4_svc")
        assert info is not None

    @pytest.mark.asyncio
    async def test_enlist_q4_both_post_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q4 post-bootstrap: enlist registers + start + activate."""
        coordinator.mark_bootstrapped()
        svc = _BothProtocolsService()

        await coordinator.enlist("q4_svc", svc)

        assert svc.started is True
        assert svc.activated is True
        info = coordinator.service_info("q4_svc")
        assert info is not None

    @pytest.mark.asyncio
    async def test_enlist_with_depends_on(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """enlist with depends_on registers without error."""
        dep = _FakeService()
        await coordinator.enlist("dep", dep)

        svc = _PersistentFakeService()
        await coordinator.enlist("child", svc, depends_on=("dep",))

        info = coordinator.service_info("child")
        assert info is not None


# ---------------------------------------------------------------------------
# ServiceQuadrant — classification and guards (Issue #1673)
# ---------------------------------------------------------------------------


class TestServiceQuadrant:
    """Tests for ServiceQuadrant enum and classify_service()."""

    @pytest.mark.parametrize(
        "service_class,expected_quadrant,expected_hot_swappable,expected_persistent",
        [
            (_FakeService, "Q1_RESTART_REQUIRED", False, False),
            (_HotSwappableService, "Q2_HOT_SWAPPABLE", True, False),
            (_PersistentFakeService, "Q3_PERSISTENT", False, True),
            (_BothProtocolsService, "Q4_BOTH", True, True),
        ],
    )
    def test_classify_quadrant(
        self,
        service_class: type,
        expected_quadrant: str,
        expected_hot_swappable: bool,
        expected_persistent: bool,
    ) -> None:
        from nexus.contracts.protocols.service_lifecycle import ServiceQuadrant

        q = ServiceQuadrant.of(service_class())
        assert q == getattr(ServiceQuadrant, expected_quadrant)
        assert q.is_hot_swappable is expected_hot_swappable
        assert q.is_persistent is expected_persistent
        # Every quadrant label should contain its Q-number
        assert expected_quadrant[:2] in q.label

    def test_coordinator_classify_all(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        from nexus.contracts.protocols.service_lifecycle import ServiceQuadrant

        coordinator._register_service("q1", _FakeService())
        coordinator._register_service("q2", _HotSwappableService())
        coordinator._register_service("q3", _PersistentFakeService())

        result = coordinator.classify_all()
        assert result == {
            "q1": ServiceQuadrant.Q1_RESTART_REQUIRED,
            "q2": ServiceQuadrant.Q2_HOT_SWAPPABLE,
            "q3": ServiceQuadrant.Q3_PERSISTENT,
        }


class TestQuadrantGuards:
    """Tests for quadrant-enforced guards on swap/activate/deactivate."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service_class,replacement_class",
        [
            (_FakeService, _FakeServiceV2),
            (_PersistentFakeService, _PersistentFakeService),
        ],
    )
    async def test_swap_allows_non_hot_swappable(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
        replacement_class: type,
    ) -> None:
        """All quadrants can be swapped via refcount drain (#1452)."""
        coordinator._register_service("svc", service_class())
        await coordinator._mount_service("svc")
        svc2 = replacement_class()
        await coordinator.swap_service("svc", svc2)

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service_class,replacement_class",
        [
            (_HotSwappableService, _HotSwappableServiceV2),
            (_BothProtocolsService, _BothProtocolsService),
        ],
    )
    async def test_swap_allows_hot_swappable(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
        replacement_class: type,
    ) -> None:
        """HotSwappable services (Q2, Q4) can be swapped."""
        svc1 = service_class()
        coordinator._register_service("svc", svc1)
        await coordinator._mount_service("svc")

        svc2 = replacement_class()
        await coordinator.swap_service("svc", svc2)

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service_class,error_match",
        [
            (_FakeService, "Q1.*restart-required.*cannot activate"),
            (_PersistentFakeService, "Q3.*PersistentService.*cannot activate"),
        ],
    )
    async def test_activate_rejects_non_hot_swappable(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
        error_match: str,
    ) -> None:
        """activate_service on non-HotSwappable quadrants raises TypeError."""
        coordinator._register_service("svc", service_class())
        with pytest.raises(TypeError, match=error_match):
            await coordinator._activate_service("svc")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service_class",
        [_HotSwappableService, _BothProtocolsService],
    )
    async def test_activate_allows_hot_swappable(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
    ) -> None:
        """activate_service succeeds on HotSwappable quadrants (Q2, Q4)."""
        svc = service_class()
        coordinator._register_service("svc", svc)
        await coordinator._activate_service("svc")
        assert svc.activated is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "service_class,error_match",
        [
            (_FakeService, "Q1.*restart-required.*cannot deactivate"),
            (_PersistentFakeService, "Q3.*PersistentService.*cannot deactivate"),
        ],
    )
    async def test_deactivate_rejects_non_hot_swappable(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
        error_match: str,
    ) -> None:
        """deactivate_service on non-HotSwappable quadrants raises TypeError."""
        coordinator._register_service("svc", service_class())
        with pytest.raises(TypeError, match=error_match):
            await coordinator._deactivate_service("svc")

    @pytest.mark.asyncio
    async def test_deactivate_allows_q2(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """deactivate_service on Q2 succeeds — calls drain + unregister hooks."""
        svc = _HotSwappableService()
        coordinator._register_service("svc", svc)
        await coordinator._activate_service("svc")
        await coordinator._deactivate_service("svc")
        assert svc.drained is True

    @pytest.mark.asyncio
    async def test_activate_not_found(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        with pytest.raises(KeyError, match="not registered"):
            await coordinator._activate_service("ghost")

    @pytest.mark.asyncio
    async def test_deactivate_not_found(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        with pytest.raises(KeyError, match="not registered"):
            await coordinator._deactivate_service("ghost")


# ---------------------------------------------------------------------------
# Q1/Q3 swap — non-HotSwappable swap via refcount drain (Issue #1452)
# ---------------------------------------------------------------------------


class TestNonHotSwappableSwap:
    """Tests for swapping Q1/Q3 services that don't implement HotSwappable."""

    @pytest.mark.asyncio
    async def test_q1_swap_succeeds(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 (restart-required) services can be swapped at runtime."""
        svc1 = _FakeService()
        coordinator._register_service("search", svc1, exports=("glob",))
        await coordinator._mount_service("search")

        svc2 = _FakeServiceV2()
        await coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2
        assert ref.glob("*.py") == ["v2:*.py"]

    @pytest.mark.asyncio
    async def test_q1_swap_does_not_call_hot_swappable_methods(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 swap must NOT invoke drain/activate — the service has no such methods."""

        class _TrackedQ1:
            def glob(self, pattern: str) -> list[str]:
                return [pattern]

        class _TrackedQ1V2:
            def glob(self, pattern: str) -> list[str]:
                return [f"v2:{pattern}"]

        svc1 = _TrackedQ1()
        coordinator._register_service("svc", svc1)
        await coordinator._mount_service("svc")

        svc2 = _TrackedQ1V2()
        # If swap_service tries to call drain()/activate() on Q1 services,
        # it will raise AttributeError — this test verifies it doesn't.
        await coordinator.swap_service("svc", svc2)

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2

    @pytest.mark.asyncio
    async def test_q1_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 swap still waits for in-flight calls via ServiceRef refcount drain."""
        call_completed = asyncio.Event()

        class _SlowQ1:
            async def work(self) -> str:
                await asyncio.sleep(0.05)
                call_completed.set()
                return "done"

        svc1 = _SlowQ1()
        coordinator._register_service("svc", svc1, exports=("work",))
        await coordinator._mount_service("svc")

        ref = coordinator.service("svc")
        assert ref is not None
        in_flight = asyncio.create_task(ref.work())

        svc2 = _FakeServiceV2()
        swap_task = asyncio.create_task(coordinator.swap_service("svc", svc2, drain_timeout=2.0))

        result = await in_flight
        assert result == "done"
        assert call_completed.is_set()

        await swap_task
        new_ref = coordinator.service("svc")
        assert new_ref is not None
        assert new_ref._service_instance is svc2

    @pytest.mark.asyncio
    async def test_q1_to_q2_upgrade_swap(
        self,
        coordinator: ServiceRegistry,
        dispatch: KernelDispatch,
    ) -> None:
        """Swap Q1 old → Q2 new: new HotSwappable instance gets activated."""
        svc1 = _FakeService()  # Q1
        coordinator._register_service("svc", svc1)
        await coordinator._mount_service("svc")

        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc2 = _HotSwappableService(hook_spec_value=spec)  # Q2
        await coordinator.swap_service("svc", svc2, hook_spec=spec)

        assert svc2.activated is True
        assert dispatch.read_hook_count == 1

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2
