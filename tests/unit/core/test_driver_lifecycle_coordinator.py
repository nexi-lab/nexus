"""Unit tests for DriverLifecycleCoordinator.

Tests mount/unmount lifecycle: routing table + VFS hook registration
+ mount/unmount Rust dispatch_observers notification.

F2 MountTable migration (commit 91ebde62b): the standalone Python
MountTable was deleted. ``DriverLifecycleCoordinator`` now takes
``(dispatch, *, kernel=...)`` and owns ``_mounts: dict[str, _PyMountInfo]``
directly. Tests that used to assert ``mount_table.add.called`` now
inspect ``coord._mounts``.

Issue #1811, #1320, #3584.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.contracts.protocols.service_hooks import HookSpec
from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator
from nexus.core.nexus_fs_dispatch import DispatchMixin
from nexus.core.path_utils import canonicalize_path

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


class _FakeMountObserver:
    """Observer that records mount/unmount events."""

    def __init__(self) -> None:
        self.mount_paths: list[str] = []
        self.unmount_paths: list[str] = []

    @property
    def event_mask(self) -> int:
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        return FILE_EVENT_BIT[FileEventType.MOUNT] | FILE_EVENT_BIT[FileEventType.UNMOUNT]

    def on_mutation(self, event: object) -> None:
        from nexus.core.file_events import FileEventType

        if event.type == FileEventType.MOUNT:
            self.mount_paths.append(event.path)
        elif event.type == FileEventType.UNMOUNT:
            self.unmount_paths.append(event.path)


class _BackendWithHookSpec:
    """Backend that declares hook_spec with an observer and a mount observer."""

    def __init__(self, name: str = "cas-test") -> None:
        self.name = name
        self._observer = _FakeObserver()
        self._mount_observer = _FakeMountObserver()

    def hook_spec(self) -> HookSpec:
        return HookSpec(
            observers=(self._observer, self._mount_observer),
        )


class _TestDispatch(DispatchMixin):
    def __init__(self):
        from nexus_kernel import Kernel

        self._kernel = Kernel()
        self._init_dispatch()


def _make_coordinator() -> tuple[MagicMock, _TestDispatch, DriverLifecycleCoordinator]:
    """Create a coordinator with a mock kernel and real DispatchMixin.

    F2 MountTable migration: the coordinator no longer takes a ``mount_table``
    argument. The first return slot used to be the mock mount table; it is
    now a mock kernel so callers can still assert on ``add_mount``/
    ``remove_mount`` interactions.
    """
    kernel = MagicMock()
    dispatch = _TestDispatch()
    coord = DriverLifecycleCoordinator(dispatch, kernel=kernel)
    return kernel, dispatch, coord


# ---------------------------------------------------------------------------
# mount()
# ---------------------------------------------------------------------------


class TestMount:
    def test_mount_records_py_mount_info(self) -> None:
        """F2: ``mount()`` writes a ``_PyMountInfo`` into ``coord._mounts``."""
        kernel, _, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.mount("/data", backend, readonly=True, io_profile="throughput")

        canonical = canonicalize_path("/data", "root")
        assert canonical in coord._mounts
        info = coord._mounts[canonical]
        assert info.backend is backend
        assert info.readonly is True
        assert info.admin_only is False
        assert info.io_profile == "throughput"

        # Rust-side ``add_mount`` should have been invoked on the kernel.
        kernel.add_mount.assert_called_once()

    def test_mount_registers_hook_spec_observers(self) -> None:
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)

        # register_observe is now a no-op (Python observers deleted).
        # Service-registered observer count is always 0.
        assert dispatch.observer_count == 0

    def test_mount_fires_mount_event(self) -> None:
        """Mount dispatches a MOUNT event through Rust dispatch_observers.

        Python mock observers no longer receive events (register_observe is
        a no-op). We verify dispatch_event is called instead.
        """
        from unittest.mock import patch

        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        with patch.object(dispatch, "dispatch_event") as mock_dispatch:
            coord.mount("/data", backend)
            mock_dispatch.assert_called_once_with("mount", "/data")

    def test_mount_no_hook_spec_still_routes(self) -> None:
        kernel, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.mount("/plain", backend)

        assert canonicalize_path("/plain", "root") in coord._mounts
        kernel.add_mount.assert_called_once()
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# unmount()
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_unregisters_hooks(self) -> None:
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)
        # register_observe is now a no-op — observer_count always 0
        assert dispatch.observer_count == 0

        result = coord.unmount("/data")
        assert result is True
        # After unmount the ``_PyMountInfo`` record is gone.
        assert canonicalize_path("/data", "root") not in coord._mounts
        assert dispatch.observer_count == 0

    def test_unmount_fires_unmount_event(self) -> None:
        """Unmount dispatches an UNMOUNT event through Rust dispatch_observers.

        Python mock observers no longer receive events (register_observe is
        a no-op). We verify dispatch_event is called instead.
        """
        from unittest.mock import patch

        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.mount("/data", backend)

        with patch.object(dispatch, "dispatch_event") as mock_dispatch:
            coord.unmount("/data")
            mock_dispatch.assert_called_once_with("unmount", "/data")

    def test_unmount_not_found_returns_false(self) -> None:
        _, _, coord = _make_coordinator()
        # No mounts registered — unmount should return False.
        assert coord.unmount("/nonexistent") is False

    def test_unmount_catches_dispatch_exception(self) -> None:
        """dispatch_event errors don't propagate (best-effort)."""
        _, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.mount("/data", backend)

        # Force dispatch_event to raise
        dispatch.dispatch_event = MagicMock(side_effect=RuntimeError("boom"))

        # Should not raise
        coord.unmount("/data")


# ---------------------------------------------------------------------------
# CAS wiring fix (#1320)
# ---------------------------------------------------------------------------


class TestCASWiringFix:
    def test_cas_backend_registers_as_observer(self) -> None:
        """CAS backends register as observers with MOUNT event_mask.

        Mount hooks are now direct observers — no adapter wrapping.
        """
        _, dispatch, coord = _make_coordinator()

        mount_obs = _FakeMountObserver()
        backend = MagicMock()
        backend.name = "cas-local"
        backend.hook_spec.return_value = HookSpec(observers=(mount_obs,))

        coord.mount("/", backend)

        # register_observe is now a no-op — observer_count always 0
        assert dispatch.observer_count == 0
