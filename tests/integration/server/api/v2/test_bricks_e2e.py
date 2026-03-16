"""E2E integration tests for brick lifecycle REST API (Issue #1704, #2060).

Tests the full stack: FastAPI app → bricks router → real BrickLifecycleManager
with mock brick instances. Validates:
- Health endpoint shows correct brick states after boot
- Runtime hot-swap (mount/unmount) via REST API
- Graceful shutdown in reverse DAG order
- Mount latency < 100ms, boot < 5s
- Lifecycle events in log output
- Drift detection and brick reset (Issue #2060)
"""

import logging
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.protocols.brick_lifecycle import (
    BrickLifecycleProtocol,
    BrickReconcileOutcome,
    DriftAction,
    ReconcileContext,
)
from nexus.server.api.v2.routers.bricks import (
    _get_lifecycle_manager,
    _get_reconciler,
    health_router,
    router,
)
from nexus.server.dependencies import require_admin
from nexus.system_services.lifecycle.brick_lifecycle import BrickLifecycleManager
from nexus.system_services.lifecycle.brick_reconciler import BrickReconciler

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


def _make_app_with_manager(
    manager: BrickLifecycleManager,
    *,
    reconciler: BrickReconciler | None = None,
) -> tuple[FastAPI, TestClient]:
    """Create a test FastAPI app wired to a real BrickLifecycleManager."""
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(router)
    app.dependency_overrides[_get_lifecycle_manager] = lambda: manager
    app.dependency_overrides[require_admin] = lambda: {"is_admin": True}
    if reconciler is not None:
        app.dependency_overrides[_get_reconciler] = lambda: reconciler
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


# ---------------------------------------------------------------------------
# E2E: Drift detection + reset (Issue #2060)
# ---------------------------------------------------------------------------


class TestBricksE2EDriftDetection:
    """Test spec/status drift detection via REST API (Issue #2060)."""

    @pytest.mark.asyncio
    async def test_drift_report_no_drift(self) -> None:
        """Drift endpoint returns empty report when all bricks converged."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        _, client = _make_app_with_manager(manager, reconciler=reconciler)

        resp = client.get("/api/v2/bricks/drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_bricks"] == 1
        assert data["drifted"] == 0
        assert data["actions_taken"] == 0
        assert data["drifts"] == []

    @pytest.mark.asyncio
    async def test_drift_report_detects_failed_brick(self) -> None:
        """Drift endpoint detects FAILED brick that should be ACTIVE."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("startup error"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        _, client = _make_app_with_manager(manager, reconciler=reconciler)

        resp = client.get("/api/v2/bricks/drift")
        assert resp.status_code == 200
        data = resp.json()
        assert data["drifted"] == 1
        assert data["drifts"][0]["brick_name"] == "search"
        assert data["drifts"][0]["spec_state"] == "enabled"
        assert data["drifts"][0]["actual_state"] == "failed"
        assert data["drifts"][0]["action"] == "reset"

    @pytest.mark.asyncio
    async def test_drift_is_read_only(self) -> None:
        """GET /drift must NOT take corrective actions."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        reconciler = BrickReconciler(lifecycle_manager=manager)
        _, client = _make_app_with_manager(manager, reconciler=reconciler)

        # Drift should show failed brick
        resp = client.get("/api/v2/bricks/drift")
        assert resp.json()["drifted"] == 1

        # Brick should STILL be failed (no auto-healing from GET)
        status = manager.get_status("search")
        assert status.state.value == "failed"


class TestBricksE2EReset:
    """Test brick reset via REST API (Issue #2060)."""

    @pytest.mark.asyncio
    async def test_reset_failed_brick(self) -> None:
        """POST /reset transitions FAILED brick to REGISTERED."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=RuntimeError("fail"))
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount_all()

        assert manager.get_status("search").state.value == "failed"

        _, client = _make_app_with_manager(manager)

        resp = client.post("/api/v2/bricks/search/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "search"
        assert data["action"] == "reset"
        assert data["state"] == "registered"

        # Verify brick is now REGISTERED
        assert manager.get_status("search").state.value == "registered"

    @pytest.mark.asyncio
    async def test_reset_nonexistent_returns_404(self) -> None:
        manager = BrickLifecycleManager()
        _, client = _make_app_with_manager(manager)

        resp = client.post("/api/v2/bricks/ghost/reset")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_reset_active_brick_returns_409(self) -> None:
        """Resetting a non-FAILED brick returns 409 Conflict."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("search")
        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")

        assert manager.get_status("search").state.value == "active"

        _, client = _make_app_with_manager(manager)

        resp = client.post("/api/v2/bricks/search/reset")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_reset_then_remount(self) -> None:
        """Reset a FAILED brick, then successfully remount."""
        manager = BrickLifecycleManager()
        call_count = 0

        async def _flaky_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first attempt fails")

        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=_flaky_start)
        manager.register("search", brick, protocol_name="SearchProtocol")

        # First mount fails
        await manager.mount_all()
        assert manager.get_status("search").state.value == "failed"

        _, client = _make_app_with_manager(manager)

        # Reset
        resp = client.post("/api/v2/bricks/search/reset")
        assert resp.status_code == 200

        # Remount succeeds
        resp = client.post("/api/v2/bricks/search/mount")
        assert resp.status_code == 200
        assert resp.json()["state"] == "active"


class TestBricksE2ESelfHealing:
    """Test reconciler self-healing via full stack (Issue #2060)."""

    @pytest.mark.asyncio
    async def test_reconciler_self_heals_failed_brick(self) -> None:
        """Reconciler automatically resets and remounts a FAILED brick."""
        manager = BrickLifecycleManager()
        call_count = 0

        async def _flaky_start() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")

        brick = _make_lifecycle_brick("search")
        brick.start = AsyncMock(side_effect=_flaky_start)
        manager.register("search", brick, protocol_name="SearchProtocol")

        # First mount fails
        await manager.mount_all()
        assert manager.get_status("search").state.value == "failed"

        # Reconciler heals it
        reconciler = BrickReconciler(lifecycle_manager=manager)
        result = await reconciler.reconcile()

        assert result.drifted >= 1
        assert result.actions_taken >= 1
        assert manager.get_status("search").state.value == "active"

    @pytest.mark.asyncio
    async def test_reconciler_respects_max_retries(self) -> None:
        """Reconciler stops retrying after max_retries."""
        manager = BrickLifecycleManager()
        brick = _make_lifecycle_brick("broken")
        brick.start = AsyncMock(side_effect=RuntimeError("permanent failure"))
        manager.register("broken", brick, protocol_name="BrokenP")

        await manager.mount_all()
        assert manager.get_status("broken").state.value == "failed"

        reconciler = BrickReconciler(lifecycle_manager=manager, max_retries=2)

        # Attempt 1 and 2 — try to heal
        # Clear backoff between passes so retries aren't delayed by exponential backoff
        for _ in range(2):
            reconciler._next_retry_after.clear()
            await reconciler.reconcile()

        # Attempt 3 — should skip (max_retries=2 exceeded)
        result = await reconciler.reconcile()
        skip_drifts = [d for d in result.drifts if d.action is DriftAction.SKIP]
        assert len(skip_drifts) == 1
        assert "exceeded" in skip_drifts[0].detail.lower()


# ---------------------------------------------------------------------------
# E2E: Per-brick reconcile protocol (Issue #2059)
# ---------------------------------------------------------------------------


class TestBricksE2EPerBrickReconcile:
    """Test per-brick reconcile via full stack (Issue #2059)."""

    @pytest.mark.asyncio
    async def test_brick_self_heals_on_second_pass(self) -> None:
        """Brick with reconcile() self-heals on second reconcile pass."""
        manager = BrickLifecycleManager()
        call_count = 0

        async def _reconcile(ctx: ReconcileContext) -> BrickReconcileOutcome:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return BrickReconcileOutcome(requeue=True)
            return BrickReconcileOutcome()  # healthy

        brick = AsyncMock()
        brick.start = AsyncMock(return_value=None)
        brick.stop = AsyncMock(return_value=None)
        brick.health_check = AsyncMock(return_value=True)
        brick.reconcile = AsyncMock(side_effect=_reconcile)
        brick.__class__.__name__ = "SearchBrick"

        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)

        # First pass: requeue
        await reconciler.reconcile()
        outcomes = reconciler.last_reconcile_outcomes
        assert len(outcomes) == 1
        assert outcomes[0][1].requeue is True

        # Clear backoff for second pass
        reconciler._next_retry_after.clear()

        # Second pass: healthy
        await reconciler.reconcile()
        outcomes = reconciler.last_reconcile_outcomes
        assert len(outcomes) == 1
        assert outcomes[0][1].requeue is False

        # Brick should still be active
        assert manager.get_status("search").state.value == "active"

    @pytest.mark.asyncio
    async def test_brick_reconcile_error_transitions_to_failed(self) -> None:
        """Brick with reconcile() returning error → FAILED."""
        manager = BrickLifecycleManager()

        async def _reconcile(ctx: ReconcileContext) -> BrickReconcileOutcome:
            return BrickReconcileOutcome(error="Index corrupted beyond repair")

        brick = AsyncMock()
        brick.start = AsyncMock(return_value=None)
        brick.stop = AsyncMock(return_value=None)
        brick.health_check = AsyncMock(return_value=True)
        brick.reconcile = AsyncMock(side_effect=_reconcile)
        brick.__class__.__name__ = "SearchBrick"

        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()

        status = manager.get_status("search")
        assert status.state.value == "failed"
        assert "corrupted" in status.error

    @pytest.mark.asyncio
    async def test_drift_endpoint_shows_reconcile_outcomes(self) -> None:
        """GET /drift includes reconcile_outcomes in response."""
        manager = BrickLifecycleManager()

        async def _reconcile(ctx: ReconcileContext) -> BrickReconcileOutcome:
            return BrickReconcileOutcome(requeue=True)

        brick = AsyncMock()
        brick.start = AsyncMock(return_value=None)
        brick.stop = AsyncMock(return_value=None)
        brick.health_check = AsyncMock(return_value=True)
        brick.reconcile = AsyncMock(side_effect=_reconcile)
        brick.__class__.__name__ = "SearchBrick"

        manager.register("search", brick, protocol_name="SearchProtocol")
        await manager.mount("search")

        reconciler = BrickReconciler(lifecycle_manager=manager)
        await reconciler.reconcile()  # Populate cache

        _, client = _make_app_with_manager(manager, reconciler=reconciler)

        resp = client.get("/api/v2/bricks/drift")
        assert resp.status_code == 200
        data = resp.json()
        assert "reconcile_outcomes" in data
        assert len(data["reconcile_outcomes"]) == 1
        assert data["reconcile_outcomes"][0]["brick_name"] == "search"
        assert data["reconcile_outcomes"][0]["requeue"] is True
