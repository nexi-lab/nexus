"""Unit tests for DriverLifecycleCoordinator.

Tests mount/unmount lifecycle: skill backend registry + mount/unmount
event dispatch via Rust dispatch_observers notification.

The DLC stores only ``_skill_backends`` (backends with a ``skill_name``
attribute) for the virtual ``.readme/`` overlay.  All routing is owned
by the Rust kernel.

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
    """Minimal backend with name and skill_name (stored by register_skill_backend)."""

    def __init__(self, name: str = "fake") -> None:
        self.name = name
        self.skill_name = "test"


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
        self.skill_name = "test"
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

    The mock kernel provides ``has_mount`` (returns True) and
    ``kernel_unmount`` so unmount tests can exercise the full path.
    """
    kernel = MagicMock()
    kernel.has_mount.return_value = True
    dispatch = _TestDispatch()
    coord = DriverLifecycleCoordinator(dispatch, kernel=kernel)
    return kernel, dispatch, coord


# ---------------------------------------------------------------------------
# register_skill_backend() + dispatch_mount_event()
# ---------------------------------------------------------------------------


class TestMount:
    def test_register_skill_backend_stores_backend(self) -> None:
        """register_skill_backend stores backends with skill_name into _skill_backends."""
        _, _, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.register_skill_backend("/data", backend)

        canonical = canonicalize_path("/data", "root")
        assert canonical in coord._skill_backends
        assert coord._skill_backends[canonical] is backend

    def test_register_skill_backend_skips_without_skill_name(self) -> None:
        """Backends without skill_name are NOT stored in _skill_backends."""
        _, _, coord = _make_coordinator()
        backend = MagicMock()
        backend.skill_name = None

        coord.register_skill_backend("/data", backend)

        canonical = canonicalize_path("/data", "root")
        assert canonical not in coord._skill_backends

    def test_get_skill_backend_returns_stored(self) -> None:
        """get_skill_backend returns the backend stored by register_skill_backend."""
        _, _, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.register_skill_backend("/data", backend)

        canonical = canonicalize_path("/data", "root")
        assert coord.get_skill_backend(canonical) is backend

    def test_get_skill_backend_returns_none_for_missing(self) -> None:
        """get_skill_backend returns None for unknown mount points."""
        _, _, coord = _make_coordinator()
        assert coord.get_skill_backend("/root/nonexistent") is None

    def test_dispatch_mount_event_fires(self) -> None:
        """dispatch_mount_event dispatches a MOUNT event through Rust dispatch_observers."""
        from unittest.mock import patch

        _, dispatch, coord = _make_coordinator()

        with patch.object(dispatch, "dispatch_event") as mock_dispatch:
            coord.dispatch_mount_event("/data")
            mock_dispatch.assert_called_once_with("mount", "/data")

    def test_register_no_hook_spec_still_stores(self) -> None:
        """Backends with skill_name but no hook_spec still get stored."""
        _, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.register_skill_backend("/plain", backend)

        assert canonicalize_path("/plain", "root") in coord._skill_backends
        assert dispatch.observer_count == 0


# ---------------------------------------------------------------------------
# unmount()
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_removes_skill_backend(self) -> None:
        """unmount removes the skill backend entry and returns True."""
        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.register_skill_backend("/data", backend)
        assert dispatch.observer_count == 0

        result = coord.unmount("/data")
        assert result is True
        # After unmount the skill backend record is gone.
        assert canonicalize_path("/data", "root") not in coord._skill_backends
        assert dispatch.observer_count == 0

    def test_unmount_fires_unmount_event(self) -> None:
        """Unmount dispatches an UNMOUNT event through Rust dispatch_observers."""
        from unittest.mock import patch

        _, dispatch, coord = _make_coordinator()
        backend = _BackendWithHookSpec()

        coord.register_skill_backend("/data", backend)

        with patch.object(dispatch, "dispatch_event") as mock_dispatch:
            coord.unmount("/data")
            mock_dispatch.assert_called_once_with("unmount", "/data")

    def test_unmount_not_found_returns_false(self) -> None:
        """Unmount returns False when kernel reports no mount at that path."""
        kernel, _, coord = _make_coordinator()
        # Kernel says no mount exists at this path.
        kernel.has_mount.return_value = False
        assert coord.unmount("/nonexistent") is False

    def test_unmount_catches_dispatch_exception(self) -> None:
        """dispatch_event errors don't propagate (best-effort)."""
        _, dispatch, coord = _make_coordinator()
        backend = _FakeBackend()

        coord.register_skill_backend("/data", backend)

        # Force dispatch_event to raise
        dispatch.dispatch_event = MagicMock(side_effect=RuntimeError("boom"))

        # Should not raise
        coord.unmount("/data")


# ---------------------------------------------------------------------------
# CAS wiring fix (#1320)
# ---------------------------------------------------------------------------


class TestCASWiringFix:
    def test_cas_backend_registers_as_skill_backend(self) -> None:
        """CAS backends with skill_name are stored in _skill_backends."""
        _, dispatch, coord = _make_coordinator()

        mount_obs = _FakeMountObserver()
        backend = MagicMock()
        backend.name = "cas-local"
        backend.skill_name = "test"
        backend.hook_spec.return_value = HookSpec(observers=(mount_obs,))

        coord.register_skill_backend("/", backend)

        # register_observe is now a no-op -- observer_count always 0
        assert dispatch.observer_count == 0
        # But the backend is stored in _skill_backends
        canonical = canonicalize_path("/", "root")
        assert coord.get_skill_backend(canonical) is backend
