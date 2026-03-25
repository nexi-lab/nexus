"""Live server E2E test for brick lifecycle API (Issue #1704, #2060).

Builds the real FastAPI app with v2 router registry, wires a real
BrickLifecycleManager with permissions-aware NexusFS mock, and tests
the brick endpoints end-to-end.

This validates:
- Bricks router loads in the real v2 registry
- Endpoints work through the real middleware stack (VersionHeader, Correlation)
- No permission issues for admin/unauthenticated access
- No performance regressions
- Lifecycle events in logs
- Drift detection and reset through live stack (Issue #2060)
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
from nexus.server.dependencies import require_admin
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler

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


def _build_live_app(
    manager: BrickLifecycleManager,
    *,
    reconciler: BrickReconciler | None = None,
) -> FastAPI:
    """Build a realistic FastAPI app with the full v2 router registry.

    Mirrors what create_app() does for router registration, but skips
    NexusFS/auth setup which requires infrastructure not available in tests.
    """
    app = FastAPI(title="nexus-test")

    # Wire up minimal app state that the bricks router dependency expects.
    # The _get_system_service dependency accesses:
    #   request.app.state.nexus_fs.service("brick_lifecycle_manager")
    _service_map = {
        "brick_lifecycle_manager": manager,
        "brick_reconciler": reconciler,
    }
    nexus_fs_mock = MagicMock()
    nexus_fs_mock.service = MagicMock(side_effect=lambda name: _service_map.get(name))
    app.state.nexus_fs = nexus_fs_mock

    # Override admin auth so endpoints are accessible without real auth
    app.dependency_overrides[require_admin] = lambda: {"is_admin": True}

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
        assert bricks_entry.endpoint_count == 5
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

    @pytest.mark.asyncio
    async def test_drift_endpoint_latency(self) -> None:
        """Drift detection with 20 bricks should respond under 50ms."""
        manager = BrickLifecycleManager()
        for i in range(20):
            b = _make_lifecycle_brick(f"b{i}")
            manager.register(f"b{i}", b, protocol_name=f"P{i}")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        app = _build_live_app(manager, reconciler=reconciler)
        client = TestClient(app)

        # Warm up
        client.get("/api/v2/bricks/drift")

        # Measure
        times = []
        for _ in range(10):
            start = time.perf_counter()
            resp = client.get("/api/v2/bricks/drift")
            elapsed_ms = (time.perf_counter() - start) * 1000
            assert resp.status_code == 200
            times.append(elapsed_ms)

        avg_ms = sum(times) / len(times)
        assert avg_ms < 50, f"Avg drift latency {avg_ms:.1f}ms exceeds 50ms"


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


class TestLiveServerDriftAndReset:
    """Test drift detection and reset through live server stack (Issue #2060)."""

    @pytest.mark.asyncio
    async def test_drift_through_middleware(self) -> None:
        """GET /drift works through real middleware stack."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        app = _build_live_app(manager, reconciler=reconciler)
        client = TestClient(app)

        resp = client.get("/api/v2/bricks/drift")
        assert resp.status_code == 200
        assert resp.headers.get("x-api-version") == "2.0"

        data = resp.json()
        assert data["drifted"] == 1
        assert data["drifts"][0]["brick_name"] == "search"
        assert data["drifts"][0]["action"] == "reset"

    @pytest.mark.asyncio
    async def test_reset_through_middleware(self) -> None:
        """POST /reset works through real middleware stack."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        app = _build_live_app(manager)
        client = TestClient(app)

        resp = client.post("/api/v2/bricks/search/reset")
        assert resp.status_code == 200
        assert resp.headers.get("x-api-version") == "2.0"
        assert resp.json()["state"] == "registered"

    @pytest.mark.asyncio
    async def test_full_self_healing_flow(self) -> None:
        """Full E2E flow: fail → drift detected → reset → remount succeeds."""
        manager = BrickLifecycleManager()
        call_count = 0

        async def _flaky_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")

        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=_flaky_start)
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        app = _build_live_app(manager, reconciler=reconciler)
        client = TestClient(app)

        # 1. Verify brick is failed
        resp = client.get("/api/v2/bricks/search")
        assert resp.json()["state"] == "failed"

        # 2. Drift shows the problem
        resp = client.get("/api/v2/bricks/drift")
        assert resp.json()["drifted"] == 1

        # 3. Reset via API
        resp = client.post("/api/v2/bricks/search/reset")
        assert resp.json()["state"] == "registered"

        # 4. Remount succeeds
        resp = client.post("/api/v2/bricks/search/mount")
        assert resp.json()["state"] == "active"

        # 5. No more drift
        resp = client.get("/api/v2/bricks/drift")
        assert resp.json()["drifted"] == 0

        # 6. Health is green
        resp = client.get("/api/v2/bricks/health")
        assert resp.json()["active"] == 1
        assert resp.json()["failed"] == 0
