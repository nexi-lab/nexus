"""Regression test: _check_rebac handles PipeRouteResult without erroring.

`PathRouter.route()` returns ``PipeRouteResult`` for DT_PIPE inodes and
``StreamRouteResult`` for DT_STREAM. Neither carries ``backend`` or
``backend_path``; the old enforcer code called ``route.backend`` unconditionally
and emitted a warning + stack trace for every IPC path checked.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

from nexus.bricks.rebac.enforcer import PermissionEnforcer
from nexus.contracts.types import OperationContext, Permission
from nexus.core.router import PipeRouteResult, StreamRouteResult


def _enforcer_with_router(router: MagicMock) -> PermissionEnforcer:
    rebac_manager = MagicMock()
    # rebac_check returns False so _check_rebac_sequential / _batched short-circuit
    # after our branch — we only care that no warning fires during routing.
    rebac_manager.rebac_check = MagicMock(return_value=False)
    rebac_manager.rebac_check_bulk = MagicMock(return_value={})
    return PermissionEnforcer(
        rebac_manager=rebac_manager,
        router=router,
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


def test_pipe_route_result_does_not_trigger_warning(caplog) -> None:
    router = MagicMock()
    router.route = MagicMock(
        return_value=PipeRouteResult(path="/root/pipes/x", metastore=MagicMock())
    )
    enf = _enforcer_with_router(router)

    with caplog.at_level(logging.WARNING, logger="nexus.bricks.rebac.enforcer"):
        enf._check_rebac("/root/pipes/x", Permission.READ, _ctx())

    # The old code path would log a warning "Failed to route path=... for object type"
    # with an AttributeError traceback. None of that should appear now.
    assert not any("Failed to route" in r.message for r in caplog.records), (
        f"unexpected warning(s): {[r.message for r in caplog.records]}"
    )


def test_stream_route_result_does_not_trigger_warning(caplog) -> None:
    router = MagicMock()
    router.route = MagicMock(
        return_value=StreamRouteResult(path="/root/streams/y", metastore=MagicMock())
    )
    enf = _enforcer_with_router(router)

    with caplog.at_level(logging.WARNING, logger="nexus.bricks.rebac.enforcer"):
        enf._check_rebac("/root/streams/y", Permission.READ, _ctx())

    assert not any("Failed to route" in r.message for r in caplog.records), (
        f"unexpected warning(s): {[r.message for r in caplog.records]}"
    )


def test_route_with_backend_still_uses_mapper(monkeypatch, caplog) -> None:
    # Sanity check: a route result with both backend + backend_path still goes
    # through ObjectTypeMapper, so this fix doesn't regress the file path.
    from nexus.core.router import RouteResult

    fake_backend = MagicMock(name="backend")
    fake_backend.get_object_type = MagicMock(return_value="file")
    fake_backend.name = "localfs"

    router = MagicMock()
    router.route = MagicMock(
        return_value=RouteResult(
            backend=fake_backend,
            metastore=MagicMock(),
            backend_path="/foo",
            mount_point="/",
        )
    )
    enf = _enforcer_with_router(router)

    enf._check_rebac("/root/foo", Permission.READ, _ctx())

    fake_backend.get_object_type.assert_called_once_with("/foo")
