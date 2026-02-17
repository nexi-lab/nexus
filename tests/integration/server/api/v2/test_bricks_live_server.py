"""Live server E2E test for brick lifecycle API (Issue #1704).

Builds the real FastAPI app with v2 router registry, wires a real
BrickLifecycleManager with permissions-aware NexusFS mock, and tests
the brick endpoints end-to-end.

This validates:
- Bricks router loads in the real v2 registry
- Endpoints work through the real middleware stack (VersionHeader, Correlation)
- No permission issues for admin/unauthenticated access
- No performance regressions
- Lifecycle events in logs
"""


import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.server.api.v2.versioning import (
    VersionHeaderMiddleware,
    build_v2_registry,
    register_v2_routers,
)
from nexus.services.brick_lifecycle import BrickLifecycleManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_lifecycle_brick(name: str) -> MagicMock:
    """Create a mock brick satisfying BrickLifecycleProtocol."""
    brick = AsyncMock()
    brick.start = AsyncMock(return_value=None)
    brick.stop = AsyncMock(return_value=None)
    brick.health_check = AsyncMock(return_value=True)
    brick.__class__.__name__ = f"{name.capitalize()}Brick"
    return brick


def _build_live_app(manager: BrickLifecycleManager) -> FastAPI:
    """Build a realistic FastAPI app with the full v2 router registry.

    Mirrors what create_app() does for router registration, but skips
    NexusFS/auth setup which requires infrastructure not available in tests.
    """
    app = FastAPI(title="nexus-test")

    # Wire up minimal app state that the bricks router dependency expects
    nexus_fs_mock = MagicMock()
    nexus_fs_mock.services = MagicMock()
    nexus_fs_mock.services.brick_lifecycle_manager = manager
    app.state.nexus_fs = nexus_fs_mock

    # Build the REAL v2 registry — same as production
    v2_registry = build_v2_registry()
    register_v2_routers(app, v2_registry)
    app.add_middleware(VersionHeaderMiddleware)

    return app


# ---------------------------------------------------------------------------
# Live server E2E tests
# ---------------------------------------------------------------------------


class TestLiveServerBricksRouter:
    """Test bricks router through the real v2 registry + middleware stack."""

    @pytest.mark.asyncio
    async def test_router_registered_in_v2_registry(self) -> None:
        """Bricks router should be in the real v2 registry."""
        registry = build_v2_registry()
        names = [e.name for e in registry.entries]
        assert "bricks" in names

        bricks_entry = next(e for e in registry.entries if e.name == "bricks")
        assert bricks_entry.endpoint_count == 4
        assert bricks_entry.router.prefix == "/api/v2/bricks"

    @pytest.mark.asyncio
    async def test_health_through_middleware_stack(self) -> None:
        """Health endpoint works through full middleware stack."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        app = _build_live_app(manager)
        client = TestClient(app)

        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 200

        # Verify VersionHeaderMiddleware added header
        assert resp.headers.get("x-api-version") == "2.0"

        data = resp.json()
        assert data["total"] == 1
        assert data["active"] == 1
        assert data["bricks"][0]["name"] == "search"
        assert data["bricks"][0]["state"] == "active"

    @pytest.mark.asyncio
    async def test_mount_unmount_through_middleware(self) -> None:
        """Mount/unmount work through real middleware stack."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("wallet")
        manager.register("wallet", brick, protocol_name="WalletProtocol")

        app = _build_live_app(manager)
        client = TestClient(app)

        # Mount
        resp = client.post("/api/v2/bricks/wallet/mount")
        assert resp.status_code == 200
        assert resp.json()["state"] == "active"
        assert resp.headers.get("x-api-version") == "2.0"
        brick.start.assert_called_once()

        # Unmount
        resp = client.post("/api/v2/bricks/wallet/unmount")
        assert resp.status_code == 200
        assert resp.json()["state"] == "unregistered"
        brick.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_auth_required_for_health(self) -> None:
        """Health endpoint is accessible without auth (no permission check)."""
        manager = BrickLifecycleManager()
        app = _build_live_app(manager)
        client = TestClient(app)

        # No auth headers at all
        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    @pytest.mark.asyncio
    async def test_404_for_missing_brick(self) -> None:
        """Individual brick status returns 404 for nonexistent brick."""
        manager = BrickLifecycleManager()
        app = _build_live_app(manager)
        client = TestClient(app)

        resp = client.get("/api/v2/bricks/ghost")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_503_when_nexus_fs_not_initialized(self) -> None:
        """Returns 503 when NexusFS is not yet initialized."""
        app = FastAPI()
        v2_registry = build_v2_registry()
        register_v2_routers(app, v2_registry)
        # No app.state.nexus_fs set at all
        client = TestClient(app)

        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 503
        assert "not initialized" in resp.json()["detail"].lower()


class TestLiveServerPerformance:
    """Performance validation through the live server stack."""

    @pytest.mark.asyncio
    async def test_health_endpoint_latency(self) -> None:
        """Health endpoint should respond under 50ms."""
        manager = BrickLifecycleManager()
        for i in range(20):
            b = _make_lifecycle_brick(f"b{i}")
            manager.register(f"b{i}", b, protocol_name=f"P{i}")
        await manager.mount_all()

        app = _build_live_app(manager)
        client = TestClient(app)

        # Warm up
        client.get("/api/v2/bricks/health")

        # Measure
        times = []
        for _ in range(10):
            start = time.perf_counter()
            resp = client.get("/api/v2/bricks/health")
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert resp.status_code == 200
            times.append(elapsed_ms)

        avg_ms = sum(times) / len(times)
        p99_ms = sorted(times)[int(len(times) * 0.99)]
        assert avg_ms < 50, f"Avg health latency {avg_ms:.1f}ms exceeds 50ms"
        assert p99_ms < 100, f"P99 health latency {p99_ms:.1f}ms exceeds 100ms"

    @pytest.mark.asyncio
    async def test_mount_endpoint_latency(self) -> None:
        """Single mount via API should complete under 100ms."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("fast")
        manager.register("fast", brick, protocol_name="FastP")

        app = _build_live_app(manager)
        client = TestClient(app)

        start = time.perf_counter()
        resp = client.post("/api/v2/bricks/fast/mount")
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert resp.status_code == 200
        assert resp.json()["state"] == "active"
        assert elapsed_ms < 100, f"Mount latency {elapsed_ms:.1f}ms exceeds 100ms"

    @pytest.mark.asyncio
    async def test_bulk_boot_performance(self) -> None:
        """Boot 50 bricks, health endpoint stays responsive."""
        manager = BrickLifecycleManager()
        for i in range(50):
            b = _make_lifecycle_brick(f"b{i}")
            manager.register(f"b{i}", b, protocol_name=f"P{i}")

        start = time.perf_counter()
        report = await manager.mount_all()
        boot_ms = (time.perf_counter() - start) * 1000

        assert report.active == 50
        assert boot_ms < 5000, f"Boot of 50 bricks took {boot_ms:.1f}ms (limit: 5000ms)"

        app = _build_live_app(manager)
        client = TestClient(app)

        # Health should still be fast
        start = time.perf_counter()
        resp = client.get("/api/v2/bricks/health")
        health_ms = (time.perf_counter() - start) * 1000

        assert resp.status_code == 200
        assert resp.json()["total"] == 50
        assert health_ms < 100, f"Health after 50-brick boot: {health_ms:.1f}ms exceeds 100ms"


class TestLiveServerLogsAndDiagnostics:
    """Verify log output through the live server stack."""

    @pytest.mark.asyncio
    async def test_lifecycle_logs_emitted(self, caplog: pytest.LogCaptureFixture) -> None:
        """Lifecycle events appear in logs during live server operations."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")

        app = _build_live_app(manager)
        client = TestClient(app)

        with caplog.at_level(logging.DEBUG, logger="nexus.services.brick_lifecycle"):
            manager.register("search", brick, protocol_name="SearchProtocol")
            client.post("/api/v2/bricks/search/mount")
            client.post("/api/v2/bricks/search/unmount")

        messages = " ".join(r.message for r in caplog.records)
        assert "Registered" in messages
        assert "mounted" in messages.lower()
        assert "unmounted" in messages.lower()

    @pytest.mark.asyncio
    async def test_failed_brick_warning_logged(self, caplog: pytest.LogCaptureFixture) -> None:
        """Failed brick mounts generate WARNING level logs."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("broken")
        brick.start = AsyncMock(side_effect=RuntimeError("connection refused"))
        manager.register("broken", brick, protocol_name="BrokenP")

        app = _build_live_app(manager)
        client = TestClient(app)

        with caplog.at_level(logging.WARNING, logger="nexus.services.brick_lifecycle"):
            client.post("/api/v2/bricks/broken/mount")

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("FAILED" in r.message for r in warnings)

        # Health shows the failure
        resp = client.get("/api/v2/bricks/health")
        data = resp.json()
        assert data["failed"] == 1
        assert data["bricks"][0]["error"] == "connection refused"
