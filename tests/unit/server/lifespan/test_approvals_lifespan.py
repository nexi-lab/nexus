"""Lifespan wiring for the approvals brick — feature-flag respect + idempotent shutdown."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from nexus.server.lifespan.approvals import (
    _approvals_enabled,
    shutdown_approvals,
    startup_approvals,
)
from nexus.server.lifespan.services_container import LifespanServices

if TYPE_CHECKING:
    from fastapi import FastAPI


class _FakeApp:
    """Minimal FastAPI stand-in: only exposes `.state` (a SimpleNamespace)."""

    def __init__(self) -> None:
        self.state = SimpleNamespace()
        # Mimic the canonical create_app() shape — async_session_factory is
        # set during create_app() before lifespan runs.
        self.state.async_session_factory = None
        self.included_routers: list[object] = []

    def include_router(self, router: object) -> None:
        self.included_routers.append(router)


def _make_app() -> "FastAPI":
    """Build a FakeApp typed as FastAPI for the startup_approvals signature."""
    return cast("FastAPI", _FakeApp())


def _make_svc(database_url: str | None = None) -> LifespanServices:
    """Construct a real LifespanServices with only the fields approvals reads."""
    return LifespanServices(database_url=database_url, profile_tuning=None)


class TestApprovalsEnabledFlag:
    def test_unset_is_disabled(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            assert _approvals_enabled() is False

    def test_one_truthy(self) -> None:
        for v in ("1", "true", "TRUE", "yes", "Yes"):
            with patch.dict("os.environ", {"NEXUS_APPROVALS_ENABLED": v}, clear=False):
                assert _approvals_enabled() is True

    def test_zero_falsy(self) -> None:
        for v in ("0", "false", "no", ""):
            with patch.dict("os.environ", {"NEXUS_APPROVALS_ENABLED": v}, clear=False):
                assert _approvals_enabled() is False


@pytest.mark.asyncio
class TestStartupApprovalsDisabled:
    async def test_attaches_none_gate_and_no_op_stack(self) -> None:
        app = _make_app()
        with patch.dict("os.environ", {}, clear=True):
            tasks = await startup_approvals(app, _make_svc())

        assert tasks == []
        # Canonical shape: a stack object with service=None, gate=None.
        assert app.state.approvals_stack is not None
        assert app.state.approvals_stack.service is None
        assert app.state.approvals_stack.gate is None
        # PolicyGate is None — hooks treat this as "approvals disabled".
        assert app.state.policy_gate is None
        # Diag router NOT registered when disabled.
        assert cast(_FakeApp, app).included_routers == []

    async def test_shutdown_is_safe_on_disabled_stack(self) -> None:
        app = _make_app()
        with patch.dict("os.environ", {}, clear=True):
            await startup_approvals(app, _make_svc())
        # Must not raise.
        await shutdown_approvals(app, _make_svc())


@pytest.mark.asyncio
class TestStartupApprovalsEnabledGuards:
    async def test_skips_when_async_session_factory_missing(self) -> None:
        app = _make_app()
        # async_session_factory stays None
        with patch.dict("os.environ", {"NEXUS_APPROVALS_ENABLED": "1"}, clear=False):
            tasks = await startup_approvals(app, _make_svc(database_url="postgresql://x@y/z"))

        assert tasks == []
        assert app.state.approvals_stack is None
        assert app.state.policy_gate is None
        assert cast(_FakeApp, app).included_routers == []

    async def test_skips_when_database_url_missing(self) -> None:
        app = _make_app()
        # async_session_factory present, but no database_url => no asyncpg pool possible
        app.state.async_session_factory = object()
        with patch.dict("os.environ", {"NEXUS_APPROVALS_ENABLED": "1"}, clear=False):
            tasks = await startup_approvals(app, _make_svc(database_url=None))

        assert tasks == []
        assert app.state.approvals_stack is None
        assert app.state.policy_gate is None
