"""Regression test: _check_rebac handles IPC paths without erroring.

PathRouter was deleted in §12 Phase F3. The enforcer now uses
``kernel.route()`` + ``dlc`` directly. IPC paths (DT_PIPE / DT_STREAM)
may not be routable via mount LPM; kernel.route() raises ValueError
for them, which the enforcer catches gracefully.

This test verifies that IPC paths do not cause unexpected warnings or
errors in _check_rebac.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext, Permission


def _enforcer_with_kernel(kernel: MagicMock, dlc: MagicMock | None = None) -> PermissionEnforcer:
    rebac_manager = MagicMock()
    # rebac_check returns False so _check_rebac_sequential / _batched short-circuit
    # after our branch — we only care that no warning fires during routing.
    rebac_manager.rebac_check = MagicMock(return_value=False)
    rebac_manager.rebac_check_bulk = MagicMock(return_value={})
    return PermissionEnforcer(
        rebac_manager=rebac_manager,
        kernel=kernel,
        dlc=dlc,
        allow_admin_bypass=False,
    )


def _ctx() -> OperationContext:
    return OperationContext(
        user_id="u",
        subject_type="user",
        subject_id="u",
        zone_id="root",
        is_admin=False,
        groups=[],
    )


def test_pipe_path_does_not_trigger_warning(caplog) -> None:
    """IPC pipe paths that are not routable should not cause warnings.

    kernel.route() raises ValueError for unmounted pipe paths. The enforcer
    catches ValueError silently (no warning) and falls back to "file" type.
    """
    kernel = MagicMock()
    kernel.route = MagicMock(side_effect=ValueError("no mount"))
    enf = _enforcer_with_kernel(kernel)

    with caplog.at_level(logging.WARNING, logger="nexus.bricks.rebac.enforcer"):
        result = enf._check_rebac("/root/pipes/x", Permission.READ, _ctx())

    # ValueError is caught silently — no warning should appear.
    assert not any("Failed to route" in r.message for r in caplog.records), (
        f"unexpected warning(s): {[r.message for r in caplog.records]}"
    )
    # Enforcer returns a result (not crash)
    assert isinstance(result, bool)


def test_stream_path_does_not_trigger_warning(caplog) -> None:
    """IPC stream paths that are not routable should not cause warnings."""
    kernel = MagicMock()
    kernel.route = MagicMock(side_effect=ValueError("no mount"))
    enf = _enforcer_with_kernel(kernel)

    with caplog.at_level(logging.WARNING, logger="nexus.bricks.rebac.enforcer"):
        result = enf._check_rebac("/root/streams/y", Permission.READ, _ctx())

    assert not any("Failed to route" in r.message for r in caplog.records), (
        f"unexpected warning(s): {[r.message for r in caplog.records]}"
    )
    assert isinstance(result, bool)


def test_route_with_backend_still_uses_mapper(caplog) -> None:
    """A route result with both backend + backend_path still goes
    through ObjectTypeMapper, so this fix doesn't regress the file path."""
    fake_backend = MagicMock(name="backend")
    fake_backend.get_object_type = MagicMock(return_value="file")
    fake_backend.name = "localfs"

    # Mock kernel.route() to return a Rust-like route result
    rust_result = MagicMock()
    rust_result.mount_point = "/"
    rust_result.backend_path = "/foo"

    kernel = MagicMock()
    kernel.route = MagicMock(return_value=rust_result)

    # Mock DLC to return mount info with the fake backend
    dlc = MagicMock()
    mount_info = MagicMock()
    mount_info.backend = fake_backend
    dlc.get_mount_info_canonical = MagicMock(return_value=mount_info)

    enf = _enforcer_with_kernel(kernel, dlc=dlc)

    enf._check_rebac("/root/foo", Permission.READ, _ctx())

    fake_backend.get_object_type.assert_called_once_with("/foo")
