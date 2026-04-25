"""Unit tests for DriverLifecycleCoordinator.

Tests the thin Python bookkeeping layer: unmount lifecycle dispatch,
kernel-delegated queries (resolve_path, mount_points), and backend_key
formatting.

The Rust kernel is the single source of truth for routing and mount
existence.  Python DLC dispatches events and delegates queries.

Issue #1811, #1320, #3584.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockDispatch:
    """Lightweight mock for DispatchMixin (no real nexus_kernel needed)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def dispatch_event(self, event_type: str, path: str) -> None:
        self.calls.append((event_type, path))


def _make_coordinator(
    *,
    has_mount: bool = True,
    self_address: str | None = None,
) -> tuple[MagicMock, _MockDispatch, DriverLifecycleCoordinator]:
    """Create a coordinator with a mock kernel and _MockDispatch."""
    kernel = MagicMock()
    kernel.has_mount.return_value = has_mount
    dispatch = _MockDispatch()
    coord = DriverLifecycleCoordinator(
        dispatch,
        kernel=kernel,
        self_address=self_address,
    )
    return kernel, dispatch, coord


# ---------------------------------------------------------------------------
# unmount()
# ---------------------------------------------------------------------------


class TestUnmount:
    def test_unmount_dispatches_event_and_calls_kernel(self) -> None:
        """unmount fires UNMOUNT event and calls kernel_unmount."""
        kernel, dispatch, coord = _make_coordinator()

        result = coord.unmount("/data")

        assert result is True
        assert ("unmount", "/data") in dispatch.calls
        kernel.kernel_unmount.assert_called_once()

    def test_unmount_returns_false_when_no_mount(self) -> None:
        """unmount returns False when kernel reports no mount at that path."""
        kernel, dispatch, coord = _make_coordinator(has_mount=False)

        result = coord.unmount("/nonexistent")

        assert result is False
        assert len(dispatch.calls) == 0
        kernel.kernel_unmount.assert_not_called()

    def test_unmount_catches_dispatch_exception(self) -> None:
        """dispatch_event errors don't propagate (best-effort notification)."""
        kernel, _, coord = _make_coordinator()
        # Replace dispatch with one that raises
        coord._dispatch = MagicMock()
        coord._dispatch.dispatch_event.side_effect = RuntimeError("boom")

        # Should not raise
        result = coord.unmount("/data")
        assert result is True
        kernel.kernel_unmount.assert_called_once()

    def test_unmount_invalid_path_returns_false(self) -> None:
        """unmount returns False for paths that fail normalization."""
        _, _, coord = _make_coordinator()

        # Empty string fails normalize_path
        result = coord.unmount("")
        assert result is False

    def test_unmount_with_zone_id(self) -> None:
        """unmount passes zone_id through to kernel."""
        kernel, dispatch, coord = _make_coordinator()

        coord.unmount("/data", zone_id="zone-a")

        kernel.has_mount.assert_called_once_with("/data", "zone-a")
        kernel.kernel_unmount.assert_called_once_with("/data", "zone-a")


# ---------------------------------------------------------------------------
# resolve_path()
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_resolve_path_delegates_to_kernel_route(self) -> None:
        """resolve_path delegates to kernel.route() and returns tuple."""
        kernel, _, coord = _make_coordinator()

        route_result = MagicMock()
        route_result.backend_name = "cas-local"
        route_result.backend_path = "/file.txt"
        route_result.mount_point = "/root/workspace"
        kernel.route.return_value = route_result

        result = coord.resolve_path("/workspace/file.txt")

        assert result is not None
        backend_name, backend_path, user_mp = result
        assert backend_name == "cas-local"
        assert backend_path == "/file.txt"
        kernel.route.assert_called_once()

    def test_resolve_path_returns_none_when_no_kernel(self) -> None:
        """resolve_path returns None when kernel is None."""
        dispatch = _MockDispatch()
        coord = DriverLifecycleCoordinator(dispatch, kernel=None)

        assert coord.resolve_path("/workspace/file.txt") is None

    def test_resolve_path_returns_none_on_route_error(self) -> None:
        """resolve_path returns None when kernel.route raises."""
        kernel, _, coord = _make_coordinator()
        kernel.route.side_effect = ValueError("no mount")

        assert coord.resolve_path("/nowhere/file.txt") is None


# ---------------------------------------------------------------------------
# mount_points()
# ---------------------------------------------------------------------------


class TestMountPoints:
    def test_mount_points_delegates_to_kernel(self) -> None:
        """mount_points delegates to kernel.get_mount_points()."""
        kernel, _, coord = _make_coordinator()
        kernel.get_mount_points.return_value = ["/root/workspace", "/root/shared"]

        result = coord.mount_points()

        assert isinstance(result, list)
        kernel.get_mount_points.assert_called_once()

    def test_mount_points_returns_empty_when_no_kernel(self) -> None:
        """mount_points returns [] when kernel is None."""
        dispatch = _MockDispatch()
        coord = DriverLifecycleCoordinator(dispatch, kernel=None)

        assert coord.mount_points() == []

    def test_mount_points_sorted(self) -> None:
        """mount_points returns sorted user-facing paths."""
        kernel, _, coord = _make_coordinator()
        kernel.get_mount_points.return_value = ["/root/workspace", "/root/archives"]

        result = coord.mount_points()

        assert result == sorted(result)


# ---------------------------------------------------------------------------
# backend_key()
# ---------------------------------------------------------------------------


class TestBackendKey:
    def test_backend_key_name_only(self) -> None:
        """backend_key with no mount_point returns just the name."""
        _, _, coord = _make_coordinator()
        backend = MagicMock()
        backend.name = "cas-local"

        assert coord.backend_key(backend) == "cas-local"

    def test_backend_key_with_mount_point(self) -> None:
        """backend_key with mount_point returns name:mount."""
        _, _, coord = _make_coordinator()
        backend = MagicMock()
        backend.name = "cas-local"

        assert coord.backend_key(backend, "/workspace") == "cas-local:/workspace"

    def test_backend_key_root_mount_omitted(self) -> None:
        """backend_key with mount_point='/' returns just the name."""
        _, _, coord = _make_coordinator()
        backend = MagicMock()
        backend.name = "cas-local"

        assert coord.backend_key(backend, "/") == "cas-local"

    def test_backend_key_with_self_address(self) -> None:
        """backend_key appends @address for federated nodes."""
        _, _, coord = _make_coordinator(self_address="node-1:9090")
        backend = MagicMock()
        backend.name = "cas-local"

        assert coord.backend_key(backend) == "cas-local@node-1:9090"

    def test_backend_key_with_mount_and_address(self) -> None:
        """backend_key with both mount_point and self_address."""
        _, _, coord = _make_coordinator(self_address="node-1:9090")
        backend = MagicMock()
        backend.name = "cas-local"

        assert coord.backend_key(backend, "/workspace") == "cas-local:/workspace@node-1:9090"
