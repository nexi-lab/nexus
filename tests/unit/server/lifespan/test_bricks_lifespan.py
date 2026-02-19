"""Tests for brick lifecycle startup/shutdown in server lifespan (Issue #1704)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.lifespan.bricks import shutdown_bricks, startup_bricks
from nexus.services.protocols.brick_lifecycle import BrickHealthReport


def _make_app(*, nexus_fs: object | None = None) -> MagicMock:
    """Create a minimal FastAPI-like app stub with state."""
    app = MagicMock()
    app.state = SimpleNamespace()
    if nexus_fs is not None:
        app.state.nexus_fs = nexus_fs
    return app


def _make_health_report(total: int = 3, active: int = 3, failed: int = 0) -> BrickHealthReport:
    return BrickHealthReport(total=total, active=active, failed=failed, bricks=())


class TestStartupBricks:
    """Tests for startup_bricks()."""

    @pytest.mark.asyncio
    async def test_calls_mount_all(self) -> None:
        """startup_bricks should call manager.mount_all()."""
        manager = MagicMock()
        manager.mount_all = AsyncMock(return_value=_make_health_report())

        sys_services = SimpleNamespace(brick_lifecycle_manager=manager)
        nx = SimpleNamespace(_system_services=sys_services)
        app = _make_app(nexus_fs=nx)

        result = await startup_bricks(app)

        manager.mount_all.assert_awaited_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_no_nexus_fs_is_safe(self) -> None:
        """startup_bricks should no-op when nexus_fs is not set."""
        app = _make_app()
        result = await startup_bricks(app)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_manager_is_safe(self) -> None:
        """startup_bricks should no-op when lifecycle manager is None."""
        sys_services = SimpleNamespace(brick_lifecycle_manager=None)
        nx = SimpleNamespace(_system_services=sys_services)
        app = _make_app(nexus_fs=nx)

        result = await startup_bricks(app)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_system_services_is_safe(self) -> None:
        """startup_bricks should no-op when _system_services is None."""
        nx = SimpleNamespace(_system_services=None)
        app = _make_app(nexus_fs=nx)

        result = await startup_bricks(app)
        assert result == []


class TestShutdownBricks:
    """Tests for shutdown_bricks()."""

    @pytest.mark.asyncio
    async def test_calls_unmount_all(self) -> None:
        """shutdown_bricks should call manager.unmount_all()."""
        manager = MagicMock()
        manager.unmount_all = AsyncMock(return_value=_make_health_report(active=0))

        sys_services = SimpleNamespace(brick_lifecycle_manager=manager)
        nx = SimpleNamespace(_system_services=sys_services)
        app = _make_app(nexus_fs=nx)

        await shutdown_bricks(app)

        manager.unmount_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_nexus_fs_is_safe(self) -> None:
        """shutdown_bricks should no-op when nexus_fs is not set."""
        app = _make_app()
        await shutdown_bricks(app)  # Should not raise

    @pytest.mark.asyncio
    async def test_no_manager_is_safe(self) -> None:
        """shutdown_bricks should no-op when lifecycle manager is None."""
        sys_services = SimpleNamespace(brick_lifecycle_manager=None)
        nx = SimpleNamespace(_system_services=sys_services)
        app = _make_app(nexus_fs=nx)

        await shutdown_bricks(app)  # Should not raise
