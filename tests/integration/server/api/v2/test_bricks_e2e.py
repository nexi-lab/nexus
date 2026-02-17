"""E2E integration tests for brick lifecycle REST API (Issue #1704).

Tests the full stack: FastAPI app → bricks router → real BrickLifecycleManager
with mock brick instances. Validates:
- Health endpoint shows correct brick states after boot
- Runtime hot-swap (mount/unmount) via REST API
- Graceful shutdown in reverse DAG order
- Mount latency < 100ms, boot < 5s
- Lifecycle events in log output
"""

from __future__ import annotations

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.routers.bricks import _get_lifecycle_manager, router
from nexus.services.brick_lifecycle import BrickLifecycleManager
from nexus.services.protocols.brick_lifecycle import BrickLifecycleProtocol, BrickState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str) -> MagicMock:
    """Create a mock brick that satisfies BrickLifecycleProtocol."""
    brick = AsyncMock()
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


def _make_app_with_manager(manager: BrickLifecycleManager) -> tuple[FastAPI, TestClient]:
    """Create a test FastAPI app wired to a real BrickLifecycleManager."""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[_get_lifecycle_manager] = lambda: manager
    client = TestClient(app)
    return app, client


# ---------------------------------------------------------------------------
# E2E: Full boot → health → hot-swap → shutdown
# ---------------------------------------------------------------------------


class TestBricksE2EBootAndHealth:
    """Test boot sequence and health reporting via REST API."""

    @pytest.mark.asyncio
    async def test_boot_health_shows_all_active(self) -> None:
        """After mount_all, health endpoint shows all bricks ACTIVE."""
        manager = BrickLifecycleManager()

        search = _make_lifecycle_brick("search")
        rag = _make_lifecycle_brick("rag")
        wallet = _make_lifecycle_brick("wallet")

        manager.register("search", search, protocol_name="SearchProtocol")
        manager.register("rag", rag, protocol_name="RAGProtocol", depends_on=("search",))
        manager.register("wallet", wallet, protocol_name="WalletProtocol")

        # Boot — mount all
        report = await manager.mount_all()
        assert report.active == 3

        _, client = _make_app_with_manager(manager)

        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["active"] == 3
        assert data["failed"] == 0

        # All bricks should be ACTIVE
        states = {b["name"]: b["state"] for b in data["bricks"]}
        assert states["search"] == "active"
        assert states["rag"] == "active"
        assert states["wallet"] == "active"

    @pytest.mark.asyncio
    async def test_boot_latency_under_5s(self) -> None:
        """Mount of 10 bricks should complete under 5 seconds."""
        manager = BrickLifecycleManager()

        for i in range(10):
            brick = _make_lifecycle_brick(f"brick_{i}")
            manager.register(f"brick_{i}", brick, protocol_name=f"Proto{i}")

        start = time.monotonic()
        report = await manager.mount_all()
        elapsed = time.monotonic() - start

        assert report.active == 10
        assert elapsed < 5.0, f"Boot took {elapsed:.2f}s (limit: 5s)"

    @pytest.mark.asyncio
    async def test_individual_mount_latency_under_100ms(self) -> None:
        """Single brick mount should complete under 100ms."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("fast")
        manager.register("fast", brick, protocol_name="FastProtocol")

        start = time.monotonic()
        await manager.mount("fast")
        elapsed = time.monotonic() - start

        assert elapsed < 0.1, f"Single mount took {elapsed * 1000:.2f}ms (limit: 100ms)"

    @pytest.mark.asyncio
    async def test_boot_with_failed_brick(self) -> None:
        """Health shows mixed states when one brick fails."""
        manager = BrickLifecycleManager()

        good = _make_lifecycle_brick("good")
        bad = _make_lifecycle_brick("bad")
        bad.start = AsyncMock(side_effect=RuntimeError("db unavailable"))

        manager.register("good", good, protocol_name="GP")
        manager.register("bad", bad, protocol_name="BP")

        await manager.mount_all()

        _, client = _make_app_with_manager(manager)
        resp = client.get("/api/v2/bricks/health")
        data = resp.json()

        assert data["total"] == 2
        assert data["active"] == 1
        assert data["failed"] == 1

        # Verify per-brick states
        bricks_by_name = {b["name"]: b for b in data["bricks"]}
        assert bricks_by_name["good"]["state"] == "active"
        assert bricks_by_name["bad"]["state"] == "failed"
        assert "db unavailable" in bricks_by_name["bad"]["error"]


class TestBricksE2EHotSwap:
    """Test runtime mount/unmount via REST API."""

    @pytest.mark.asyncio
    async def test_hot_swap_mount_unmount(self) -> None:
        """Mount and unmount a single brick via API."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")

        _, client = _make_app_with_manager(manager)

        # Initially: REGISTERED
        resp = client.get("/api/v2/bricks/search")
        assert resp.json()["state"] == "registered"

        # Mount via API
        resp = client.post("/api/v2/bricks/search/mount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "mount"
        assert data["state"] == "active"
        brick.start.assert_called_once()

        # Verify health after mount
        resp = client.get("/api/v2/bricks/health")
        assert resp.json()["active"] == 1

        # Unmount via API
        resp = client.post("/api/v2/bricks/search/unmount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["action"] == "unmount"
        assert data["state"] == "unregistered"
        brick.stop.assert_called_once()

        # Verify health after unmount
        resp = client.get("/api/v2/bricks/health")
        assert resp.json()["active"] == 0

    @pytest.mark.asyncio
    async def test_mount_nonexistent_returns_404(self) -> None:
        manager = BrickLifecycleManager()
        _, client = _make_app_with_manager(manager)

        resp = client.post("/api/v2/bricks/ghost/mount")
        assert resp.status_code == 404


class TestBricksE2EGracefulShutdown:
    """Verify graceful shutdown unmounts in reverse DAG order."""

    @pytest.mark.asyncio
    async def test_shutdown_reverse_order(self) -> None:
        """Bricks should unmount in reverse dependency order."""
        manager = BrickLifecycleManager()

        stop_order: list[str] = []

        def _make_tracked_brick(name: str) -> MagicMock:
            brick = _make_lifecycle_brick(name)

            async def _track_stop() -> None:
                stop_order.append(name)

            brick.stop = AsyncMock(side_effect=_track_stop)
            return brick

        infra = _make_tracked_brick("infra")
        search = _make_tracked_brick("search")
        rag = _make_tracked_brick("rag")

        manager.register("infra", infra, protocol_name="InfraP")
        manager.register("search", search, protocol_name="SearchP", depends_on=("infra",))
        manager.register("rag", rag, protocol_name="RAGP", depends_on=("search",))

        # Boot
        await manager.mount_all()
        assert manager.health().active == 3

        # Shutdown
        await manager.unmount_all()
        assert manager.health().active == 0

        # Verify reverse order: rag first (depends on search), search second, infra last
        assert stop_order.index("rag") < stop_order.index("search")
        assert stop_order.index("search") < stop_order.index("infra")


class TestBricksE2ELifecycleLogs:
    """Verify lifecycle events appear in log output."""

    @pytest.mark.asyncio
    async def test_mount_unmount_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        """Key lifecycle transitions should be logged."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")

        with caplog.at_level(logging.DEBUG, logger="nexus.services.brick_lifecycle"):
            manager.register("search", brick, protocol_name="SearchProtocol")
            await manager.mount("search")
            await manager.unmount("search")

        messages = " ".join(r.message for r in caplog.records)

        # Registration
        assert "Registered" in messages
        # Mount
        assert "mounted" in messages.lower()
        # Unmount
        assert "unmounted" in messages.lower()

    @pytest.mark.asyncio
    async def test_failed_brick_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Failed brick mounts should log at WARNING level."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("broken")
        brick.start = AsyncMock(side_effect=RuntimeError("connection refused"))
        manager.register("broken", brick, protocol_name="BrokenP")

        with caplog.at_level(logging.WARNING, logger="nexus.services.brick_lifecycle"):
            await manager.mount_all()

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("FAILED" in r.message for r in warnings)
        assert any("connection refused" in r.message for r in warnings)


class TestBricksE2EProtocolCompliance:
    """Verify BrickLifecycleProtocol is properly checked."""

    @pytest.mark.asyncio
    async def test_stateless_brick_mounts_without_start(self) -> None:
        """A brick that doesn't implement BrickLifecycleProtocol should skip start()."""
        manager = BrickLifecycleManager()

        # Plain object — NOT satisfying BrickLifecycleProtocol
        stateless = MagicMock(spec=[])  # Empty spec — no start/stop/health_check
        manager.register("stateless", stateless, protocol_name="StatelessP")

        _, client = _make_app_with_manager(manager)

        # Mount — should succeed without calling start()
        resp = client.post("/api/v2/bricks/stateless/mount")
        assert resp.status_code == 200
        assert resp.json()["state"] == "active"

    @pytest.mark.asyncio
    async def test_lifecycle_brick_calls_start_stop(self) -> None:
        """A brick implementing BrickLifecycleProtocol should have start/stop called."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("lifecycle")
        assert isinstance(brick, BrickLifecycleProtocol)

        manager.register("lifecycle", brick, protocol_name="LP")

        await manager.mount("lifecycle")
        brick.start.assert_called_once()

        await manager.unmount("lifecycle")
        brick.stop.assert_called_once()
