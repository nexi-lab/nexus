"""Unit tests for DriverLifecycleCoordinator.

Tests mount/unmount lifecycle: routing table + VFS hook registration
+ mount/unmount KernelDispatch notification.

Issue #1811, #1320.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.contracts.vfs_hooks import MountHookContext, UnmountHookContext
from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator
from nexus.core.kernel_dispatch import KernelDispatch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Minimal backend with name, no hook_spec."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name


class _FakeObserver:
    """Minimal VFSObserver."""

    def on_mutation(self, event: object) -> None:
        pass


class _FakeMountHook:
    """VFSMountHook that records calls."""

    def __init__(self) -> None:
        self.calls: list[MountHookContext] = []

    def on_mount(self, ctx: MountHookContext) -> None:
        self.calls.append(ctx)


class _FakeUnmountHook:
    """VFSUnmountHook that records calls."""

    def __init__(self) -> None:
        self.calls: list[UnmountHookContext] = []

    def on_unmount(self, ctx: UnmountHookContext) -> None:
        self.calls.append(ctx)


class _BackendWithHookSpec:
    """Backend that declares hook_spec with an observer and mount hook."""

    def __init__(self, name: str = "cas-test") -> None:
        self.name = name
        self._observer = _FakeObserver()
        self._mount_hook = _FakeMountHook()

    def hook_spec(self) -> HookSpec:
        return HookSpec(
            observers=(self._observer,),
            mount_hooks=(self._mount_hook,),
        )


def _make_coordinator() -> tuple[MagicMock, KernelDispatch, DriverLifecycleCoordinator]:
    """Create a coordinator with a mock router and real KernelDispatch."""
    router = MagicMock()
    dispatch = KernelDispatch()
    coord = DriverLifecycleCoordinator(router, dispatch)
    return router, dispatch, coord


# ---------------------------------------------------------------------------
# mount()
# ---------------------------------------------------------------------------


class TestMount:
    def test_mount_calls_router_add_mount(self) -> None:
        router, _, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.mount("/data", backend, readonly=True, io_profile="throughput")

        router.add_mount.assert_called_once_with(
            "/data",
            backend,
            readonly=True,
            admin_only=False,
            io_profile="throughput",
        )

    def test_mount_registers_hook_spec_observers(self) -> None:
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)

        assert dispatch.observer_count == 1

    def test_mount_registers_hook_spec_mount_hooks(self) -> None:
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)

        assert dispatch.mount_hook_count == 1

    def test_mount_calls_on_mount(self) -> None:
        """Mount hooks receive notification via KernelDispatch."""
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)

        hook = backend._mount_hook
        assert len(hook.calls) == 1
        assert hook.calls[0].mount_point == "/data"
        assert hook.calls[0].backend is backend

    def test_mount_no_hook_spec_still_routes(self) -> None:
        router, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.mount("/plain", backend)

        router.add_mount.assert_called_once()
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# unmount()
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_unregisters_hooks(self) -> None:
        router, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)
        assert dispatch.observer_count == 1

        # Setup router.get_mount to return the backend
        mount_info = MagicMock()
        mount_info.backend = backend
        router.get_mount.return_value = mount_info

        result = coord.unmount("/data")
        assert result is True
        assert dispatch.observer_count == 0
        assert dispatch.mount_hook_count == 0

    def test_unmount_calls_on_unmount(self) -> None:
        router, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        # Register an unmount hook directly
        unmount_hook = _FakeUnmountHook()
        dispatch.register_unmount_hook(unmount_hook)

        mount_info = MagicMock()
        mount_info.backend = backend
        router.get_mount.return_value = mount_info

        coord.unmount("/data")

        assert len(unmount_hook.calls) == 1
        assert unmount_hook.calls[0].mount_point == "/data"
        assert unmount_hook.calls[0].backend is backend

    def test_unmount_not_found_returns_false(self) -> None:
        router, _, coord = _make_coordinator()
        router.get_mount.return_value = None

        assert coord.unmount("/nonexistent") is False

    def test_unmount_catches_notification_exception(self) -> None:
        """on_unmount errors don't propagate (best-effort)."""
        router, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        class _FailingUnmountHook:
            def on_unmount(self, ctx: UnmountHookContext) -> None:
                raise RuntimeError("boom")

        dispatch.register_unmount_hook(_FailingUnmountHook())

        mount_info = MagicMock()
        mount_info.backend = backend
        router.get_mount.return_value = mount_info

        # Should not raise
        coord.unmount("/data")


# ---------------------------------------------------------------------------
# CAS wiring fix (#1320)
# ---------------------------------------------------------------------------


class TestCASWiringFix:
    def test_cas_hook_spec_has_no_observers(self) -> None:
        """CAS hook_spec() returns HookSpec with NO observers (empty tuple), only mount_hooks."""
        _, dispatch, coord = _make_coordinator()

        # Create a minimal CAS-like backend with hook_spec that has no observers
        mount_hook = _FakeMountHook()
        backend = MagicMock()
        backend.name = "cas-local"
        backend.hook_spec.return_value = HookSpec(observers=(), mount_hooks=(mount_hook,))

        coord.mount("/", backend)

        assert dispatch.observer_count == 0
        assert dispatch.mount_hook_count == 1
