"""Unit tests for the bricks lifecycle API router (Issue #1704).

Tests all 4 endpoints using FastAPI TestClient with a mock
BrickLifecycleManager.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.contracts.protocols.brick_lifecycle import (
    BrickHealthReport,
    BrickState,
    BrickStatus,
)
from nexus.server.api.v2.routers.bricks import (
    _get_lifecycle_manager,
    health_router,
    router,
)
from nexus.server.dependencies import require_admin

# ---------------------------------------------------------------------------
# App setup — isolated test app with dependency overrides
# ---------------------------------------------------------------------------

_test_app = FastAPI()
_test_app.include_router(health_router)
_test_app.include_router(router)


def _make_mock_manager() -> MagicMock:
    """Create a mock BrickLifecycleManager with sensible defaults."""
    manager = MagicMock()

    # Default health report: empty
    manager.health.return_value = BrickHealthReport(total=0, active=0, failed=0, bricks=())
    # Default get_status: None (brick not found)
    manager.get_status.return_value = None
    # mount/unmount are async in the real manager
    manager.mount = AsyncMock()
    manager.unmount = AsyncMock()

    return manager


_mock_manager = _make_mock_manager()


def _override_manager() -> MagicMock:
    return _mock_manager


_test_app.dependency_overrides[_get_lifecycle_manager] = _override_manager

# Override require_admin for existing functional tests (simulates authenticated admin)
_admin_result = {"authenticated": True, "is_admin": True, "subject_id": "test-admin"}
_test_app.dependency_overrides[require_admin] = lambda: _admin_result

client = TestClient(_test_app)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    """Reset mock between tests."""
    _mock_manager.reset_mock()
    _mock_manager.health.return_value = BrickHealthReport(total=0, active=0, failed=0, bricks=())
    _mock_manager.get_status.return_value = None
    _mock_manager.get_status.side_effect = None
    _mock_manager.mount = AsyncMock()
    _mock_manager.unmount = AsyncMock()


def _make_status(
    name: str = "test_brick",
    state: BrickState = BrickState.ACTIVE,
    protocol_name: str = "TestProtocol",
    error: str | None = None,
    started_at: float | None = 100.0,
    stopped_at: float | None = None,
) -> BrickStatus:
    """Helper to create a BrickStatus."""
    return BrickStatus(
        name=name,
        state=state,
        protocol_name=protocol_name,
        error=error,
        started_at=started_at,
        stopped_at=stopped_at,
    )


# ---------------------------------------------------------------------------
# GET /api/v2/bricks/health
# ---------------------------------------------------------------------------


class TestBrickHealth:
    """Tests for GET /api/v2/bricks/health."""

    def test_empty_health(self) -> None:
        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["active"] == 0
        assert data["failed"] == 0
        assert data["bricks"] == []

    def test_health_with_bricks(self) -> None:
        statuses = (
            _make_status("search", BrickState.ACTIVE, "SearchProtocol"),
            _make_status("rag", BrickState.ACTIVE, "RAGProtocol"),
            _make_status("wallet", BrickState.FAILED, "WalletProtocol", error="timeout"),
        )
        _mock_manager.health.return_value = BrickHealthReport(
            total=3, active=2, failed=1, bricks=statuses
        )

        resp = client.get("/api/v2/bricks/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["active"] == 2
        assert data["failed"] == 1
        assert len(data["bricks"]) == 3
        # Check individual brick
        names = [b["name"] for b in data["bricks"]]
        assert "search" in names
        assert "wallet" in names

    def test_health_brick_state_serialized_as_value(self) -> None:
        statuses = (_make_status("x", BrickState.REGISTERED, "XP"),)
        _mock_manager.health.return_value = BrickHealthReport(
            total=1, active=0, failed=0, bricks=statuses
        )
        resp = client.get("/api/v2/bricks/health")
        data = resp.json()
        assert data["bricks"][0]["state"] == "registered"


# ---------------------------------------------------------------------------
# GET /api/v2/bricks/{name}
# ---------------------------------------------------------------------------


class TestBrickStatus:
    """Tests for GET /api/v2/bricks/{name}."""

    def test_brick_not_found(self) -> None:
        _mock_manager.get_status.return_value = None
        resp = client.get("/api/v2/bricks/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["detail"].lower()

    def test_brick_found(self) -> None:
        status = _make_status("search", BrickState.ACTIVE, "SearchProtocol")
        _mock_manager.get_status.return_value = status

        resp = client.get("/api/v2/bricks/search")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "search"
        assert data["state"] == "active"
        assert data["protocol_name"] == "SearchProtocol"
        assert data["error"] is None
        assert data["started_at"] == 100.0

    def test_brick_with_error(self) -> None:
        status = _make_status("broken", BrickState.FAILED, "BrokenProtocol", error="db down")
        _mock_manager.get_status.return_value = status

        resp = client.get("/api/v2/bricks/broken")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "failed"
        assert data["error"] == "db down"


# ---------------------------------------------------------------------------
# POST /api/v2/bricks/{name}/mount
# ---------------------------------------------------------------------------


class TestMountBrick:
    """Tests for POST /api/v2/bricks/{name}/mount."""

    def test_mount_not_found(self) -> None:
        _mock_manager.get_status.return_value = None
        resp = client.post("/api/v2/bricks/missing/mount")
        assert resp.status_code == 404

    @pytest.mark.anyio
    def test_mount_success(self) -> None:
        # Before mount: REGISTERED; after mount: ACTIVE
        _mock_manager.get_status.side_effect = [
            _make_status("search", BrickState.REGISTERED, "SearchProtocol"),
            _make_status("search", BrickState.ACTIVE, "SearchProtocol"),
        ]
        resp = client.post("/api/v2/bricks/search/mount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "search"
        assert data["action"] == "mount"
        assert data["state"] == "active"
        assert data["error"] is None
        _mock_manager.mount.assert_called_once_with("search")
        # Reset side_effect
        _mock_manager.get_status.side_effect = None

    @pytest.mark.anyio
    def test_mount_with_failure(self) -> None:
        _mock_manager.get_status.side_effect = [
            _make_status("bad", BrickState.REGISTERED, "BP"),
            _make_status("bad", BrickState.FAILED, "BP", error="start failed"),
        ]
        resp = client.post("/api/v2/bricks/bad/mount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "failed"
        assert data["error"] == "start failed"
        _mock_manager.get_status.side_effect = None


# ---------------------------------------------------------------------------
# POST /api/v2/bricks/{name}/unmount
# ---------------------------------------------------------------------------


class TestUnmountBrick:
    """Tests for POST /api/v2/bricks/{name}/unmount."""

    def test_unmount_not_found(self) -> None:
        _mock_manager.get_status.return_value = None
        resp = client.post("/api/v2/bricks/missing/unmount")
        assert resp.status_code == 404

    @pytest.mark.anyio
    def test_unmount_success(self) -> None:
        _mock_manager.get_status.side_effect = [
            _make_status("search", BrickState.ACTIVE, "SearchProtocol"),
            _make_status("search", BrickState.UNMOUNTED, "SearchProtocol", stopped_at=200.0),
        ]
        resp = client.post("/api/v2/bricks/search/unmount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "search"
        assert data["action"] == "unmount"
        assert data["state"] == "unmounted"
        _mock_manager.unmount.assert_called_once_with("search")
        _mock_manager.get_status.side_effect = None

    @pytest.mark.anyio
    def test_unmount_with_failure(self) -> None:
        _mock_manager.get_status.side_effect = [
            _make_status("bad", BrickState.ACTIVE, "BP"),
            _make_status("bad", BrickState.FAILED, "BP", error="stop failed"),
        ]
        resp = client.post("/api/v2/bricks/bad/unmount")
        assert resp.status_code == 200
        data = resp.json()
        assert data["state"] == "failed"
        assert data["error"] == "stop failed"
        _mock_manager.get_status.side_effect = None


# ---------------------------------------------------------------------------
# Manager unavailability (503)
# ---------------------------------------------------------------------------


class TestManagerUnavailable:
    """Test 503 responses when lifecycle manager is not available."""

    def test_health_without_manager(self) -> None:
        app = FastAPI()
        app.include_router(health_router)
        # No dependency override — _get_lifecycle_manager will fail
        # We need to mock the app state
        c = TestClient(app)
        resp = c.get("/api/v2/bricks/health")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Auth enforcement (Issue #2048)
# ---------------------------------------------------------------------------


class TestAuthRequired:
    """Verify admin endpoints reject unauthenticated requests."""

    def _make_auth_app(self) -> tuple[FastAPI, TestClient]:
        """Create an app with real auth (api_key set, so open-access is off)."""
        app = FastAPI()
        app.include_router(health_router)
        app.include_router(router)
        app.dependency_overrides[_get_lifecycle_manager] = _override_manager
        # Set api_key so open-access mode is off (triggers real auth)
        app.state.api_key = "test-secret-key"
        app.state.auth_provider = None
        return app, TestClient(app)

    def test_unauthenticated_returns_401(self) -> None:
        """Admin endpoints without auth should return 401."""
        _, c = self._make_auth_app()

        # Admin endpoints should return 401
        assert c.get("/api/v2/bricks/test_brick").status_code == 401
        assert c.post("/api/v2/bricks/test_brick/mount").status_code == 401
        assert c.post("/api/v2/bricks/test_brick/unmount").status_code == 401

    def test_health_endpoint_remains_public(self) -> None:
        """Health endpoint does not require auth (K8s probes)."""
        _, c = self._make_auth_app()

        resp = c.get("/api/v2/bricks/health")
        assert resp.status_code == 200
