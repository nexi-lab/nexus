"""Tests for brick lifecycle startup/shutdown in server lifespan (Issue #1704)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.protocols.brick_lifecycle import BrickHealthReport
from nexus.server.lifespan.bricks import shutdown_bricks, startup_bricks
from nexus.server.lifespan.services_container import LifespanServices


def _make_svc(
    *,
    nexus_fs: object | None = None,
    brick_lifecycle_manager: object | None = None,
    brick_reconciler: object | None = None,
) -> LifespanServices:
    """Create a minimal LifespanServices stub with given fields."""
    return LifespanServices(
        nexus_fs=nexus_fs,
        brick_lifecycle_manager=brick_lifecycle_manager,
        brick_reconciler=brick_reconciler,
    )


def _make_health_report(total: int = 3, active: int = 3, failed: int = 0) -> BrickHealthReport:
    return BrickHealthReport(total=total, active=active, failed=failed, bricks=())


class TestStartupBricks:
    """Tests for startup_bricks()."""

    @pytest.mark.asyncio
    async def test_calls_mount_all(self) -> None:
        """startup_bricks should call manager.mount_all()."""
        manager = MagicMock()
        manager.mount_all = AsyncMock(return_value=_make_health_report())

        app = MagicMock()
        svc = _make_svc(nexus_fs=MagicMock(), brick_lifecycle_manager=manager)
        result = await startup_bricks(app, svc)

        manager.mount_all.assert_awaited_once()
        assert result == []

    @pytest.mark.asyncio
    async def test_no_nexus_fs_is_safe(self) -> None:
        """startup_bricks should no-op when nexus_fs is not set."""
        app = MagicMock()
        svc = _make_svc()
        result = await startup_bricks(app, svc)
        assert result == []

    @pytest.mark.asyncio
    async def test_no_manager_is_safe(self) -> None:
        """startup_bricks should no-op when lifecycle manager is None."""
        app = MagicMock()
        svc = _make_svc(nexus_fs=MagicMock(), brick_lifecycle_manager=None)
        result = await startup_bricks(app, svc)
        assert result == []

    @pytest.mark.asyncio
    async def test_starts_reconciler(self) -> None:
        """startup_bricks should start the brick reconciler."""
        manager = MagicMock()
        manager.mount_all = AsyncMock(return_value=_make_health_report())
        reconciler = MagicMock()
        reconciler.start = AsyncMock()

        app = MagicMock()
        svc = _make_svc(
            nexus_fs=MagicMock(),
            brick_lifecycle_manager=manager,
            brick_reconciler=reconciler,
        )
        await startup_bricks(app, svc)
        reconciler.start.assert_awaited_once()


class TestShutdownBricks:
    """Tests for shutdown_bricks()."""

    @pytest.mark.asyncio
    async def test_calls_unmount_all(self) -> None:
        """shutdown_bricks should call manager.unmount_all()."""
        manager = MagicMock()
        manager.unmount_all = AsyncMock(return_value=_make_health_report(active=0))

        app = MagicMock()
        svc = _make_svc(nexus_fs=MagicMock(), brick_lifecycle_manager=manager)
        await shutdown_bricks(app, svc)

        manager.unmount_all.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_nexus_fs_is_safe(self) -> None:
        """shutdown_bricks should no-op when nexus_fs is not set."""
        app = MagicMock()
        svc = _make_svc()
        await shutdown_bricks(app, svc)  # Should not raise

    @pytest.mark.asyncio
    async def test_no_manager_is_safe(self) -> None:
        """shutdown_bricks should no-op when lifecycle manager is None."""
        app = MagicMock()
        svc = _make_svc(nexus_fs=MagicMock(), brick_lifecycle_manager=None)
        await shutdown_bricks(app, svc)  # Should not raise

    @pytest.mark.asyncio
    async def test_unmounts_without_manual_reconciler_stop(self) -> None:
        """shutdown_bricks should unmount (reconciler stop handled by coordinator)."""
        manager = MagicMock()
        manager.unmount_all = AsyncMock(return_value=_make_health_report(active=0))

        app = MagicMock()
        svc = _make_svc(
            nexus_fs=MagicMock(),
            brick_lifecycle_manager=manager,
        )
        await shutdown_bricks(app, svc)

        manager.unmount_all.assert_awaited_once()
