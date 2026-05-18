"""Unit tests for DriverLifecycleCoordinator.

Tests the thin Python unmount-event-broadcaster: unmount lifecycle
dispatch + kernel-delegated mount_points query.  The Rust kernel is the
single source of truth for routing and mount existence; Python DLC just
fires the ``unmount`` KernelDispatch event after a Rust unmount completes.

Issue #1811, #1320, #3584.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator

# The DLC.unmount() method does ``from nexus_runtime import PyOperationContext``
# at call-time.  In unit tests the Rust extension may not be installed, so we
# inject a lightweight stub into ``sys.modules`` so the import succeeds.
if "nexus_runtime" not in sys.modules:
    _stub = types.ModuleType("nexus_runtime")
    _stub.__dict__["PyOperationContext"] = type(
        "PyOperationContext", (), {"__init__": lambda self, **kw: None}
    )
    sys.modules["nexus_runtime"] = _stub

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DT_MOUNT = 2  # nexus.contracts.metadata.DT_MOUNT


class _MockDispatch:
    """Lightweight mock for DispatchMixin (no real nexus_runtime needed)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dispatch_event(self, event_type: str, path: str) -> None:
        self.calls.append((event_type, path))


def _make_coordinator(
    *,
    has_mount: bool = True,
) -> tuple[MagicMock, _MockDispatch, DriverLifecycleCoordinator]:
    """Create a coordinator with a mock kernel and _MockDispatch."""
    kernel = MagicMock()
    # sys_stat returns dict with entry_type=DT_MOUNT when mount exists
    if has_mount:
        kernel.sys_stat.return_value = {"entry_type": DT_MOUNT}
    else:
        kernel.sys_stat.side_effect = FileNotFoundError("not found")
    dispatch = _MockDispatch()
    coord = DriverLifecycleCoordinator(dispatch, kernel=kernel)
    return kernel, dispatch, coord


# ---------------------------------------------------------------------------
# unmount()
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_dispatches_event_and_calls_kernel(self) -> None:
        """unmount fires UNMOUNT event and calls sys_unlink."""
        kernel, dispatch, coord = _make_coordinator()

        result = coord.unmount("/data")

        assert result is True
        assert ("unmount", "/data") in dispatch.calls
        kernel.sys_unlink.assert_called_once()

    def test_unmount_returns_false_when_no_mount(self) -> None:
        """unmount returns False when kernel reports no mount at that path."""
        kernel, dispatch, coord = _make_coordinator(has_mount=False)

        result = coord.unmount("/nonexistent")

        assert result is False
        assert len(dispatch.calls) == 0
        kernel.sys_unlink.assert_not_called()

    def test_unmount_catches_dispatch_exception(self) -> None:
        """dispatch_event errors don't propagate (best-effort notification)."""
        kernel, _, coord = _make_coordinator()
        # Replace dispatch with one that raises
        coord._dispatch = MagicMock()
        coord._dispatch.dispatch_event.side_effect = RuntimeError("boom")

        # Should not raise
        result = coord.unmount("/data")
        assert result is True
        kernel.sys_unlink.assert_called_once()

    def test_unmount_invalid_path_returns_false(self) -> None:
        """unmount returns False for paths that fail normalization."""
        _, _, coord = _make_coordinator()

        # Empty string fails normalize_path
        result = coord.unmount("")
        assert result is False

    def test_unmount_with_zone_id(self) -> None:
        """unmount passes zone_id through to kernel sys_stat + sys_unlink."""
        kernel, dispatch, coord = _make_coordinator()

        coord.unmount("/data", zone_id="zone-a")

        kernel.sys_stat.assert_called_once_with("/data", "zone-a")
        kernel.sys_unlink.assert_called_once()


# ---------------------------------------------------------------------------
# mount_points()
# ---------------------------------------------------------------------------


class TestMountPoints:
    def test_mount_points_delegates_to_kernel(self) -> None:
        """mount_points delegates to kernel.get_top_level_mounts()."""
        kernel, _, coord = _make_coordinator()
        kernel.get_top_level_mounts.return_value = ["/", "/workspace", "/shared"]

        result = coord.mount_points()

        assert isinstance(result, list)
        assert result == ["/", "/shared", "/workspace"]
        kernel.get_top_level_mounts.assert_called_once()

    def test_mount_points_returns_empty_when_no_kernel(self) -> None:
        """mount_points returns [] when kernel is None."""
        dispatch = _MockDispatch()
        coord = DriverLifecycleCoordinator(dispatch, kernel=None)

        assert coord.mount_points() == []

    def test_mount_points_sorted(self) -> None:
        """mount_points returns sorted user-facing paths."""
        kernel, _, coord = _make_coordinator()
        kernel.get_top_level_mounts.return_value = ["/workspace", "/archives"]

        result = coord.mount_points()

        assert result == sorted(result)
