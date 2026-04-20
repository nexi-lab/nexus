"""Unit tests for ServiceRegistry lifecycle orchestration (Issue #1452 Phase 3, #1577, #1814).

These tests exercise the lifecycle methods (enlist, swap_service, start/stop)
that were merged from ServiceLifecycleCoordinator into ServiceRegistry in Issue #1814.

One-dimension model: BackgroundService protocol + duck-typed hook_spec().
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.protocols.service_lifecycle import BackgroundService
from nexus.core.nexus_fs_dispatch import DispatchMixin
from nexus.core.service_registry import ServiceRegistry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def registry() -> ServiceRegistry:
    return ServiceRegistry()


class _TestDispatch(DispatchMixin):
    def __init__(self):
        from nexus_kernel import Kernel

        self._kernel = Kernel()
        self._init_dispatch()


@pytest.fixture()
def dispatch() -> _TestDispatch:
    return _TestDispatch()


@pytest.fixture()
def coordinator(dispatch: _TestDispatch) -> ServiceRegistry:
    return ServiceRegistry(dispatch=dispatch)


class _FakeService:
    """Simple on-demand service stub (no hook_spec)."""

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


class _FakeServiceV2:
    """V2 on-demand replacement (no hook_spec)."""

    def glob(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]

    def grep(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]


class _FakeHookService:
    """Fake service that has a hook_spec() method."""

    def __init__(self, hook_spec_value: HookSpec | None = None) -> None:
        self._hook_spec = hook_spec_value or HookSpec()

    def hook_spec(self) -> HookSpec:
        return self._hook_spec

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


class _FakeHookServiceV2(_FakeHookService):
    """V2 replacement that also has hook_spec()."""

    def glob(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]

    def grep(self, pattern: str) -> list[str]:
        return [f"v2:{pattern}"]


class _BackgroundFakeService:
    """BackgroundService stub — satisfies the Protocol structurally."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def do_work(self) -> str:
        return "working"


class _FakeBackgroundHookService:
    """Fake service with both start/stop and hook_spec()."""

    def __init__(self, hook_spec_value: HookSpec | None = None) -> None:
        self._hook_spec = hook_spec_value or HookSpec()
        self.started = False
        self.stopped = False

    def hook_spec(self) -> HookSpec:
        return self._hook_spec

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _DynamicProxyLikeService:
    """Proxy-shaped object that synthesizes arbitrary public attributes."""

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def _synthetic(*_args, **_kwargs):
            return None

        return _synthetic

    def glob(self, pattern: str) -> list[str]:
        return [pattern]

    def grep(self, pattern: str) -> list[str]:
        return [pattern]


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

    def test_enlist_ignores_synthetic_hook_spec(
        self, coordinator: ServiceRegistry, dispatch: _TestDispatch
    ) -> None:
        svc = _DynamicProxyLikeService()

        coordinator.enlist("search", svc, exports=("glob", "grep"))

        assert coordinator._get_hook_spec("search") is None
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# mount — _mount_service
# ---------------------------------------------------------------------------


class TestMountService:
    def test_mount_registers_hooks(
        self, coordinator: ServiceRegistry, dispatch: _TestDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        observer = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,), observers=(observer,))
        coordinator._register_service("search", svc)
        coordinator._set_hook_spec("search", spec)
        coordinator._mount_service("search")

        assert dispatch.read_hook_count == 1
        # register_observe is now a no-op — observer_count always 0
        assert dispatch.observer_count == 0

    def test_mount_no_hooks_if_no_spec(
        self, coordinator: ServiceRegistry, dispatch: _TestDispatch
    ) -> None:
        svc = _FakeService()
        coordinator._register_service("search", svc)
        coordinator._mount_service("search")
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# umount — _unmount_service
# ---------------------------------------------------------------------------


class TestUnmountService:
    def test_unmount_removes_hooks(
        self, coordinator: ServiceRegistry, dispatch: _TestDispatch
    ) -> None:
        svc = _FakeService()
        read_hook = MagicMock()
        spec = HookSpec(read_hooks=(read_hook,))
        coordinator._register_service("search", svc)
        coordinator._set_hook_spec("search", spec)
        coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        coordinator._unmount_service("search")
        assert dispatch.read_hook_count == 0


# ---------------------------------------------------------------------------
# rmmod — unregister_service_full
# ---------------------------------------------------------------------------


class TestUnregisterServiceFull:
    def test_full_unregister(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        svc = _FakeService()
        coordinator._register_service("search", svc)
        coordinator._mount_service("search")
        coordinator.unregister_service_full("search")

        # Gone from registry
        assert coordinator.service("search") is None


# ---------------------------------------------------------------------------
# swap — swap_service (the hot-swap test)
# ---------------------------------------------------------------------------


class TestSwapService:
    def test_basic_swap(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _FakeHookService(hook_spec_value=spec1)
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._set_hook_spec("search", spec1)
        coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _FakeHookServiceV2(hook_spec_value=spec2)
        coordinator.swap_service("search", svc2, exports=("glob", "grep"), hook_spec=spec2)

        # New instance is served
        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

        # Old hooks removed, new hooks registered
        assert dispatch.read_hook_count == 1

    def test_swap_no_none_window(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Verify that service(name) NEVER returns None during swap."""
        svc1 = _FakeHookService()
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._mount_service("search")

        assert coordinator.service("search") is not None

        svc2 = _FakeHookServiceV2()
        coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    def test_swap_allows_non_hot_swappable(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Q1 services can be swapped via refcount drain (#1452)."""
        svc1 = _FakeService()  # no hook_spec
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._mount_service("search")

        svc2 = _FakeServiceV2()
        coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2

    def test_swap_auto_detects_hook_spec_from_protocol(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """If no explicit hook_spec param, coordinator reads it from duck-typed hook_spec()."""
        hook1 = MagicMock()
        spec1 = HookSpec(read_hooks=(hook1,))
        svc1 = _FakeHookService(hook_spec_value=spec1)
        # Register then set hook_spec separately (retroactive capture)
        coordinator._register_service("search", svc1)
        coordinator._set_hook_spec("search", spec1)
        coordinator._mount_service("search")
        assert dispatch.read_hook_count == 1

        hook2 = MagicMock()
        spec2 = HookSpec(read_hooks=(hook2,))
        svc2 = _FakeHookServiceV2(hook_spec_value=spec2)
        # Swap WITHOUT explicit hook_spec — coordinator auto-detects from protocol
        coordinator.swap_service("search", svc2)

        assert dispatch.read_hook_count == 1

    def test_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Verify swap waits for in-flight sync calls to complete."""
        call_completed = threading.Event()

        class _SlowService:
            def glob(self, pattern: str) -> list[str]:
                import time

                time.sleep(0.05)
                call_completed.set()
                return [pattern]

            def hook_spec(self) -> HookSpec:
                return HookSpec()

        svc1 = _SlowService()
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._mount_service("search")

        # Start an in-flight call via ServiceRef in a thread
        ref = coordinator.service("search")
        assert ref is not None
        result_holder: list[list[str]] = []

        def _call() -> None:
            result_holder.append(ref.glob("*.py"))

        t = threading.Thread(target=_call)
        t.start()

        # Swap should wait for the in-flight call to drain
        svc2 = _FakeHookServiceV2()
        coordinator.swap_service("search", svc2, exports=("glob",), drain_timeout=2.0)

        t.join(timeout=5.0)
        assert result_holder == [["*.py"]]
        assert call_completed.is_set()

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
    def test_drain_immediate_when_no_inflight(self, coordinator: ServiceRegistry) -> None:
        """Drain should return immediately if refcount is 0."""
        # No in-flight calls — drain should not block
        coordinator._drain("search", timeout=0.1)

    def test_drain_timeout_when_stuck(self, coordinator: ServiceRegistry) -> None:
        """Drain should timeout and warn if refcount doesn't reach 0."""
        # Manually set refcount > 0 to simulate stuck call
        coordinator._refcounts["stuck"] = 5
        # Should timeout, not hang forever
        coordinator._drain("stuck", timeout=0.05)
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

    def test_swap_unregisters_old_hooks_registers_new(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
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
        svc1 = _FakeHookService(hook_spec_value=spec1)
        coordinator._register_service("rebac", svc1)
        coordinator._set_hook_spec("rebac", spec1)
        coordinator._mount_service("rebac")

        assert dispatch.read_hook_count == 1
        assert dispatch.write_hook_count == 1
        # register_observe is now a no-op — observer_count always 0
        assert dispatch.observer_count == 0

        new_read = MagicMock()
        new_write = MagicMock()
        new_observer = MagicMock()
        spec2 = HookSpec(
            read_hooks=(new_read,),
            write_hooks=(new_write,),
            observers=(new_observer,),
        )
        svc2 = _FakeHookServiceV2(hook_spec_value=spec2)
        coordinator.swap_service("rebac", svc2, hook_spec=spec2)

        # Counts unchanged (old removed, new added) — observer_count still 0
        assert dispatch.read_hook_count == 1
        assert dispatch.write_hook_count == 1
        assert dispatch.observer_count == 0

    def test_swap_with_no_new_spec_clears_old(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """Swap without new hook_spec should unregister old hooks and leave none."""
        old_hook = MagicMock()
        spec1 = HookSpec(read_hooks=(old_hook,))
        svc1 = _FakeHookService(hook_spec_value=spec1)
        coordinator._register_service("parser", svc1)
        coordinator._set_hook_spec("parser", spec1)
        coordinator._mount_service("parser")
        assert dispatch.read_hook_count == 1

        svc2 = _FakeHookServiceV2()  # empty hook_spec
        coordinator.swap_service("parser", svc2)

        # Old hook removed, no new hook registered
        assert dispatch.read_hook_count == 0


# ---------------------------------------------------------------------------
# Protocol conformance — isinstance checks (Issue #1577)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """Verify structural subtyping works for BackgroundService."""

    @pytest.mark.parametrize(
        "service_class,protocol,expected",
        [
            (_BackgroundFakeService, BackgroundService, True),
            (_FakeService, BackgroundService, False),
            (_FakeHookService, BackgroundService, False),
        ],
    )
    def test_protocol_conformance(
        self, service_class: type, protocol: type, expected: bool
    ) -> None:
        """Test protocol detection for services."""
        svc = service_class()
        assert isinstance(svc, protocol) is expected


# ---------------------------------------------------------------------------
# Auto-lifecycle — BackgroundService management (Issue #1580)
# ---------------------------------------------------------------------------


class TestAutoLifecycleBackgroundService:
    """Auto start/stop for BackgroundService (Q3 + Q4)."""

    def test_start_calls_start_on_background(self, coordinator: ServiceRegistry) -> None:
        svc = _BackgroundFakeService()
        coordinator._register_service("worker", svc)
        started = coordinator.start_background_services()
        assert started == ["worker"]
        assert svc.started is True

    def test_start_skips_non_background(self, coordinator: ServiceRegistry) -> None:
        coordinator._register_service("search", _FakeService())
        started = coordinator.start_background_services()
        assert started == []

    def test_stop_calls_stop_on_background(self, coordinator: ServiceRegistry) -> None:
        svc = _BackgroundFakeService()
        coordinator._register_service("worker", svc)
        stopped = coordinator.stop_background_services()
        assert stopped == ["worker"]
        assert svc.stopped is True

    def test_start_handles_exception(self, coordinator: ServiceRegistry) -> None:
        """Exception during start() logs error, continues to next service."""

        class _FailStart:
            async def start(self) -> None:
                raise RuntimeError("boom")

            async def stop(self) -> None:
                pass

        ok_svc = _BackgroundFakeService()
        coordinator._register_service("fail", _FailStart())
        coordinator._register_service("ok", ok_svc)
        started = coordinator.start_background_services()
        assert "ok" in started
        assert "fail" not in started
        assert ok_svc.started is True

    def test_stop_handles_exception(self, coordinator: ServiceRegistry) -> None:
        """Exception during stop() logs error, continues."""

        class _FailStop:
            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                raise RuntimeError("boom")

        ok_svc = _BackgroundFakeService()
        coordinator._register_service("fail", _FailStop())
        coordinator._register_service("ok", ok_svc)
        stopped = coordinator.stop_background_services()
        assert "ok" in stopped
        assert "fail" not in stopped
        assert ok_svc.stopped is True

    def test_start_handles_timeout(self, coordinator: ServiceRegistry) -> None:
        """Timeout during start() logs error, continues."""

        class _SlowStart:
            async def start(self) -> None:
                await asyncio.sleep(10)

            async def stop(self) -> None:
                pass

        ok_svc = _BackgroundFakeService()
        coordinator._register_service("slow", _SlowStart())
        coordinator._register_service("ok", ok_svc)
        started = coordinator.start_background_services(timeout=0.01)
        assert "ok" in started
        assert "slow" not in started

    def test_start_stop_idempotent(self, coordinator: ServiceRegistry) -> None:
        svc = _BackgroundFakeService()
        coordinator._register_service("worker", svc)
        coordinator.start_background_services()
        coordinator.start_background_services()
        assert svc.started is True
        coordinator.stop_background_services()
        coordinator.stop_background_services()
        assert svc.stopped is True


class TestUnregisterAllHooks:
    """Verify _unregister_all_hooks() used by aclose()."""

    def test_unregisters_all_hooks(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        hook1 = MagicMock()
        hook2 = MagicMock()
        coordinator._set_hook_spec("svc1", HookSpec(read_hooks=(hook1,)))
        coordinator._set_hook_spec("svc2", HookSpec(observers=(hook2,)))
        coordinator._register_hooks("svc1")
        coordinator._register_hooks("svc2")
        assert dispatch.read_hook_count == 1
        # register_observe is now a no-op — observer_count always 0
        assert dispatch.observer_count == 0

        coordinator._unregister_all_hooks()
        assert dispatch.read_hook_count == 0
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# enlist — the ONE entry point for all services (Issue #1502)
# ---------------------------------------------------------------------------


class TestEnlist:
    """Tests for ``reg.enlist()`` — the single entry point for all services."""

    def test_enlist_on_demand(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """On-demand service: enlist registers only, no start."""
        svc = _FakeService()
        coordinator.enlist("svc", svc)

        info = coordinator.service_info("svc")
        assert info is not None
        assert info.instance is svc

    def test_enlist_background_pre_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """BackgroundService pre-bootstrap: enlist registers but defers start()."""
        svc = _BackgroundFakeService()
        assert svc.started is False

        coordinator.enlist("svc", svc)

        assert svc.started is False  # deferred — not yet bootstrapped
        info = coordinator.service_info("svc")
        assert info is not None

    def test_enlist_background_post_bootstrap(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """BackgroundService post-bootstrap: enlist registers + calls start() immediately."""
        coordinator.mark_bootstrapped()
        svc = _BackgroundFakeService()
        assert svc.started is False

        coordinator.enlist("svc", svc)

        assert svc.started is True
        info = coordinator.service_info("svc")
        assert info is not None

    def test_enlist_auto_registers_hooks(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """Service with hook_spec(): enlist registers + captures hooks immediately."""
        hook = MagicMock()
        svc = _FakeHookService(hook_spec_value=HookSpec(read_hooks=(hook,)))

        coordinator.enlist("svc", svc)

        info = coordinator.service_info("svc")
        assert info is not None
        assert coordinator._get_hook_spec("svc") is not None
        assert dispatch.read_hook_count == 1

    def test_enlist_background_with_hooks_pre_bootstrap(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """BackgroundService + hook_spec pre-bootstrap: hooks registered, start deferred."""
        hook = MagicMock()
        svc = _FakeBackgroundHookService(hook_spec_value=HookSpec(read_hooks=(hook,)))
        assert svc.started is False

        coordinator.enlist("svc", svc)

        assert svc.started is False  # deferred — not yet bootstrapped
        assert coordinator._get_hook_spec("svc") is not None
        assert dispatch.read_hook_count == 1

    def test_enlist_background_with_hooks_post_bootstrap(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """BackgroundService + hook_spec post-bootstrap: hooks registered + started."""
        coordinator.mark_bootstrapped()
        hook = MagicMock()
        svc = _FakeBackgroundHookService(hook_spec_value=HookSpec(read_hooks=(hook,)))

        coordinator.enlist("svc", svc)

        assert svc.started is True
        assert coordinator._get_hook_spec("svc") is not None
        assert dispatch.read_hook_count == 1

    def test_enlist_with_depends_on(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """enlist with depends_on registers without error."""
        dep = _FakeService()
        coordinator.enlist("dep", dep)

        svc = _BackgroundFakeService()
        coordinator.enlist("child", svc, depends_on=("dep",))

        info = coordinator.service_info("child")
        assert info is not None


class TestSwapUnifiedPath:
    """All services use the same swap path — no separate drain/activate."""

    @pytest.mark.parametrize(
        "service_class,replacement_class",
        [
            (_FakeService, _FakeServiceV2),
            (_BackgroundFakeService, _BackgroundFakeService),
            (_FakeHookService, _FakeHookServiceV2),
            (_FakeBackgroundHookService, _FakeBackgroundHookService),
        ],
    )
    def test_swap_all_service_types(
        self,
        coordinator: ServiceRegistry,
        service_class: type,
        replacement_class: type,
    ) -> None:
        """All service types can be swapped via unified refcount drain path."""
        coordinator._register_service("svc", service_class())
        coordinator._mount_service("svc")
        svc2 = replacement_class()
        coordinator.swap_service("svc", svc2)

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2


# ---------------------------------------------------------------------------
# Swap without hook_spec — refcount drain (Issue #1452)
# ---------------------------------------------------------------------------


class TestSwapWithoutHooks:
    """Tests for swapping services that don't have hook_spec()."""

    def test_swap_on_demand_succeeds(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """On-demand services can be swapped at runtime."""
        svc1 = _FakeService()
        coordinator._register_service("search", svc1, exports=("glob",))
        coordinator._mount_service("search")

        svc2 = _FakeServiceV2()
        coordinator.swap_service("search", svc2, exports=("glob",))

        ref = coordinator.service("search")
        assert ref is not None
        assert ref._service_instance is svc2
        assert ref.glob("*.py") == ["v2:*.py"]

    def test_swap_does_not_require_hook_spec(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Services without hook_spec() can be swapped without AttributeError."""

        class _Plain:
            def glob(self, pattern: str) -> list[str]:
                return [pattern]

        class _PlainV2:
            def glob(self, pattern: str) -> list[str]:
                return [f"v2:{pattern}"]

        svc1 = _Plain()
        coordinator._register_service("svc", svc1)
        coordinator._mount_service("svc")

        svc2 = _PlainV2()
        coordinator.swap_service("svc", svc2)

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2

    def test_swap_drains_in_flight_calls(
        self,
        coordinator: ServiceRegistry,
    ) -> None:
        """Swap waits for in-flight calls via ServiceRef refcount drain."""
        call_completed = threading.Event()

        class _SlowService:
            def work(self) -> str:
                import time

                time.sleep(0.05)
                call_completed.set()
                return "done"

        svc1 = _SlowService()
        coordinator._register_service("svc", svc1, exports=("work",))
        coordinator._mount_service("svc")

        ref = coordinator.service("svc")
        assert ref is not None
        result_holder: list[str] = []

        def _call() -> None:
            result_holder.append(ref.work())

        t = threading.Thread(target=_call)
        t.start()

        svc2 = _FakeServiceV2()
        coordinator.swap_service("svc", svc2, drain_timeout=2.0)

        t.join(timeout=5.0)
        assert result_holder == ["done"]
        assert call_completed.is_set()

        new_ref = coordinator.service("svc")
        assert new_ref is not None
        assert new_ref._service_instance is svc2

    def test_swap_plain_to_hookspec_registers_new_hooks(
        self,
        coordinator: ServiceRegistry,
        dispatch: _TestDispatch,
    ) -> None:
        """Swap plain old → hook_spec new: new hooks get registered."""
        svc1 = _FakeService()
        coordinator._register_service("svc", svc1)
        coordinator._mount_service("svc")

        hook = MagicMock()
        spec = HookSpec(read_hooks=(hook,))
        svc2 = _FakeHookService(hook_spec_value=spec)
        coordinator.swap_service("svc", svc2, hook_spec=spec)

        assert dispatch.read_hook_count == 1

        ref = coordinator.service("svc")
        assert ref is not None
        assert ref._service_instance is svc2
