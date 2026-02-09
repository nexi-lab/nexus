"""Unit tests for Lock REST API endpoints (Issue #1110).

These tests use FastAPI TestClient with a mocked LockManagerProtocol,
verifying the full endpoint code path: request parsing -> business logic
-> response serialization.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from nexus.core.distributed_lock import ExtendResult, HolderInfo, LockInfo
from nexus.server.fastapi_server import (
    LockHolderResponse,
    LockInfoMutex,
    LockInfoSemaphore,
    LockListResponse,
    LockResponse,
    LockStatusResponse,
)

# =============================================================================
# Fixtures
# =============================================================================


def _make_lock_info(
    path: str = "/test/file.txt",
    mode: str = "mutex",
    max_holders: int = 1,
    lock_id: str = "lock-id-abc123",
    fence_token: int = 42,
) -> LockInfo:
    """Create a LockInfo for testing."""
    return LockInfo(
        path=path,
        mode=mode,
        max_holders=max_holders,
        holders=[
            HolderInfo(
                lock_id=lock_id,
                holder_info="test-holder",
                acquired_at=1700000000.0,
                expires_at=1700000030.0,
            )
        ],
        fence_token=fence_token,
    )


@pytest.fixture
def mock_lock_manager():
    """Create a mock implementing LockManagerProtocol."""
    manager = AsyncMock()
    manager.acquire = AsyncMock(return_value="lock-id-abc123")
    manager.release = AsyncMock(return_value=True)
    manager.extend = AsyncMock(return_value=ExtendResult(success=True, lock_info=_make_lock_info()))
    manager.get_lock_info = AsyncMock(return_value=_make_lock_info())
    manager.is_locked = AsyncMock(return_value=True)
    manager.list_locks = AsyncMock(return_value=[])
    manager.force_release = AsyncMock(return_value=True)
    manager.health_check = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def mock_nexus_fs(mock_lock_manager):
    """Create mock NexusFS with lock manager."""
    fs = MagicMock()
    fs._has_distributed_locks = MagicMock(return_value=True)
    fs._lock_manager = mock_lock_manager
    return fs


@pytest.fixture
def client(mock_nexus_fs):
    """Create a FastAPI TestClient with mocked lock manager."""
    from nexus.server import fastapi_server as fas

    original_nexus_fs = fas._app_state.nexus_fs
    original_api_key = fas._app_state.api_key

    try:
        app = fas.create_app(mock_nexus_fs, api_key="test-api-key")
        yield TestClient(app), mock_nexus_fs._lock_manager

    finally:
        fas._app_state.nexus_fs = original_nexus_fs
        fas._app_state.api_key = original_api_key


# =============================================================================
# Acquire Lock Endpoint Tests
# =============================================================================


class TestAcquireLock:
    """Test POST /api/locks endpoint."""

    def test_acquire_mutex_lock_success(self, client):
        """Test successful mutex lock acquisition."""
        test_client, lock_manager = client
        lock_manager.acquire.return_value = "lock-id-12345"
        lock_manager.get_lock_info.return_value = _make_lock_info(
            lock_id="lock-id-12345", fence_token=1
        )

        response = test_client.post(
            "/api/locks",
            json={"path": "/test/file.txt", "timeout": 10, "ttl": 30},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["lock_id"] == "lock-id-12345"
        assert data["path"] == "/test/file.txt"
        assert data["mode"] == "mutex"
        assert data["max_holders"] == 1
        assert data["ttl"] == 30
        assert "expires_at" in data
        assert "fence_token" in data

    def test_acquire_semaphore_lock_success(self, client):
        """Test successful semaphore lock acquisition."""
        test_client, lock_manager = client
        lock_manager.acquire.return_value = "sem-lock-id"
        lock_manager.get_lock_info.return_value = _make_lock_info(
            mode="semaphore", max_holders=5, lock_id="sem-lock-id", fence_token=2
        )

        response = test_client.post(
            "/api/locks",
            json={"path": "/shared/room", "timeout": 5, "ttl": 60, "max_holders": 5},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 201
        data = response.json()
        assert data["mode"] == "semaphore"
        assert data["max_holders"] == 5

    def test_acquire_lock_timeout(self, client):
        """Test lock acquisition timeout returns 409."""
        test_client, lock_manager = client
        lock_manager.acquire.return_value = None

        response = test_client.post(
            "/api/locks",
            json={"path": "/busy/file.txt", "timeout": 1, "ttl": 30},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 409
        assert "timeout" in response.json()["detail"].lower()

    def test_acquire_non_blocking_failure(self, client):
        """Test non-blocking mode returns 409 immediately."""
        test_client, lock_manager = client
        lock_manager.acquire.return_value = None

        response = test_client.post(
            "/api/locks",
            json={"path": "/busy/file.txt", "timeout": 10, "ttl": 30, "blocking": False},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 409
        assert "non-blocking" in response.json()["detail"].lower()
        # Verify timeout was set to 0 for non-blocking
        lock_manager.acquire.assert_called_once()
        call_kwargs = lock_manager.acquire.call_args
        assert call_kwargs.kwargs.get("timeout") == 0.0 or call_kwargs[1].get("timeout") == 0.0

    def test_acquire_without_auth_returns_401(self, client):
        """Test that unauthenticated request returns 401."""
        test_client, _ = client
        response = test_client.post(
            "/api/locks",
            json={"path": "/test/file.txt"},
        )
        assert response.status_code == 401

    def test_acquire_path_normalization(self, client):
        """Test that paths without leading slash are normalized."""
        test_client, lock_manager = client
        lock_manager.acquire.return_value = "lock-123"
        lock_manager.get_lock_info.return_value = _make_lock_info(fence_token=1)

        response = test_client.post(
            "/api/locks",
            json={"path": "no-leading-slash/file.txt"},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 201
        assert response.json()["path"] == "/no-leading-slash/file.txt"

    def test_acquire_ttl_validation_too_high(self, client):
        """Test that TTL > max is rejected with 422."""
        test_client, _ = client

        response = test_client.post(
            "/api/locks",
            json={"path": "/test/file.txt", "ttl": 100000},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 422

    def test_acquire_ttl_validation_too_low(self, client):
        """Test that TTL < 1 is rejected with 422."""
        test_client, _ = client

        response = test_client.post(
            "/api/locks",
            json={"path": "/test/file.txt", "ttl": 0},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 422

    def test_acquire_max_holders_validation(self, client):
        """Test that max_holders < 1 is rejected with 422."""
        test_client, _ = client

        response = test_client.post(
            "/api/locks",
            json={"path": "/test/file.txt", "max_holders": 0},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 422


# =============================================================================
# Get Lock Status Endpoint Tests
# =============================================================================


class TestGetLockStatus:
    """Test GET /api/locks/{path} endpoint."""

    def test_get_status_locked_mutex(self, client):
        """Test status check on a mutex-locked path."""
        test_client, lock_manager = client
        lock_manager.get_lock_info.return_value = _make_lock_info(
            path="/test/file.txt", fence_token=10
        )

        response = test_client.get(
            "/api/locks/test/file.txt",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is True
        assert data["path"] == "/test/file.txt"
        assert data["lock_info"]["mode"] == "mutex"
        assert data["lock_info"]["lock_id"] == "lock-id-abc123"
        assert "fence_token" in data["lock_info"]

    def test_get_status_locked_semaphore(self, client):
        """Test status check on a semaphore-locked path."""
        test_client, lock_manager = client
        lock_manager.get_lock_info.return_value = LockInfo(
            path="/shared/room",
            mode="semaphore",
            max_holders=5,
            holders=[
                HolderInfo("h1", "holder1", 1700000000.0, 1700000030.0),
                HolderInfo("h2", "holder2", 1700000001.0, 1700000031.0),
            ],
            fence_token=20,
        )

        response = test_client.get(
            "/api/locks/shared/room",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is True
        assert data["lock_info"]["mode"] == "semaphore"
        assert data["lock_info"]["current_holders"] == 2
        assert data["lock_info"]["max_holders"] == 5

    def test_get_status_unlocked(self, client):
        """Test status check on an unlocked path."""
        test_client, lock_manager = client
        lock_manager.get_lock_info.return_value = None

        response = test_client.get(
            "/api/locks/test/unlocked.txt",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["locked"] is False
        assert data["lock_info"] is None


# =============================================================================
# Release Lock Endpoint Tests
# =============================================================================


class TestReleaseLock:
    """Test DELETE /api/locks/{path} endpoint."""

    def test_release_success(self, client):
        """Test successful lock release."""
        test_client, lock_manager = client
        lock_manager.release.return_value = True

        response = test_client.delete(
            "/api/locks/test/file.txt?lock_id=lock-id-123",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        assert response.json()["released"] is True

    def test_release_wrong_owner(self, client):
        """Test release with wrong lock_id returns 403."""
        test_client, lock_manager = client
        lock_manager.release.return_value = False

        response = test_client.delete(
            "/api/locks/test/file.txt?lock_id=wrong-id",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 403

    def test_force_release_requires_admin(self, client):
        """Test that force=true requires admin privileges."""
        test_client, _ = client

        # Use wrong API key to simulate non-admin auth
        response = test_client.delete(
            "/api/locks/test/file.txt?lock_id=any-id&force=true",
            headers={"Authorization": "Bearer wrong-api-key"},
        )

        # Wrong API key returns 401 unauthorized
        assert response.status_code == 401

    def test_force_release_not_found(self, client):
        """Test force release when lock doesn't exist returns 404."""
        test_client, lock_manager = client
        lock_manager.force_release.return_value = False

        # API key auth is admin, so force release is allowed
        # but returns 404 when lock doesn't exist
        response = test_client.delete(
            "/api/locks/test/file.txt?lock_id=any-id&force=true",
            headers={"Authorization": "Bearer test-api-key"},
        )
        assert response.status_code == 404
        assert "No lock found" in response.json()["detail"]


# =============================================================================
# Extend Lock Endpoint Tests
# =============================================================================


class TestExtendLock:
    """Test PATCH /api/locks/{path} endpoint."""

    def test_extend_success(self, client):
        """Test successful lock extension."""
        test_client, lock_manager = client
        lock_manager.extend.return_value = ExtendResult(
            success=True,
            lock_info=_make_lock_info(fence_token=5),
        )

        response = test_client.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "lock-id-123", "ttl": 60},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["lock_id"] == "lock-id-123"
        assert data["ttl"] == 60
        assert data["mode"] == "mutex"
        assert "fence_token" in data
        assert "expires_at" in data

    def test_extend_wrong_owner(self, client):
        """Test extend with wrong lock_id returns 403."""
        test_client, lock_manager = client
        lock_manager.extend.return_value = ExtendResult(success=False)

        response = test_client.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "wrong-id", "ttl": 60},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 403

    def test_extend_returns_semaphore_info(self, client):
        """Test extend returns correct mode for semaphore lock."""
        test_client, lock_manager = client
        lock_manager.extend.return_value = ExtendResult(
            success=True,
            lock_info=LockInfo(
                path="/shared/room",
                mode="semaphore",
                max_holders=3,
                holders=[
                    HolderInfo("lock-id-456", "holder", 1700000000.0, 1700000060.0),
                ],
                fence_token=7,
            ),
        )

        response = test_client.patch(
            "/api/locks/shared/room",
            json={"lock_id": "lock-id-456", "ttl": 60},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        assert response.json()["mode"] == "semaphore"
        assert response.json()["max_holders"] == 3

    def test_extend_ttl_validation(self, client):
        """Test that extend TTL is validated."""
        test_client, _ = client

        response = test_client.patch(
            "/api/locks/test/file.txt",
            json={"lock_id": "lock-id-123", "ttl": 0},
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 422


# =============================================================================
# List Locks Endpoint Tests
# =============================================================================


class TestListLocks:
    """Test GET /api/locks endpoint."""

    def test_list_locks_empty(self, client):
        """Test listing when no locks exist."""
        test_client, lock_manager = client
        lock_manager.list_locks.return_value = []

        response = test_client.get(
            "/api/locks",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 0
        assert data["locks"] == []

    def test_list_locks_with_results(self, client):
        """Test listing with active locks."""
        test_client, lock_manager = client
        lock_manager.list_locks.return_value = [
            _make_lock_info(path="/file1.txt", lock_id="lock1", fence_token=1),
            LockInfo(
                path="/file2.txt",
                mode="semaphore",
                max_holders=3,
                holders=[
                    HolderInfo("sem1", "h1", 1700000000.0, 1700000030.0),
                    HolderInfo("sem2", "h2", 1700000001.0, 1700000031.0),
                ],
                fence_token=2,
            ),
        ]

        response = test_client.get(
            "/api/locks",
            headers={"Authorization": "Bearer test-api-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["count"] == 2
        assert data["locks"][0]["mode"] == "mutex"
        assert data["locks"][1]["mode"] == "semaphore"
        assert data["locks"][1]["current_holders"] == 2


# =============================================================================
# Lock Manager Availability Tests
# =============================================================================


class TestLockManagerAvailability:
    """Test behavior when lock manager is not configured."""

    def test_acquire_returns_503_without_lock_manager(self):
        """Test that POST /api/locks returns 503 when lock manager unavailable."""
        from nexus.server import fastapi_server as fas

        original = fas._app_state.nexus_fs
        try:
            mock_fs = MagicMock()
            mock_fs._has_distributed_locks = MagicMock(return_value=False)
            fas._app_state.nexus_fs = mock_fs
            fas._app_state.api_key = "test-api-key"

            app = fas.create_app(mock_fs, api_key="test-api-key")

            test_client = TestClient(app)
            response = test_client.post(
                "/api/locks",
                json={"path": "/test/file.txt"},
                headers={"Authorization": "Bearer test-api-key"},
            )
            assert response.status_code == 503
        finally:
            fas._app_state.nexus_fs = original


# =============================================================================
# Response Model Tests
# =============================================================================


class TestLockResponseModels:
    """Test response model construction and validation."""

    def test_lock_response_mutex(self):
        """Test LockResponse for mutex."""
        resp = LockResponse(
            lock_id="abc-123",
            path="/test/file.txt",
            mode="mutex",
            max_holders=1,
            ttl=30,
            expires_at="2025-01-01T00:00:30+00:00",
            fence_token=42,
        )
        assert resp.mode == "mutex"
        assert resp.fence_token == 42

    def test_lock_response_semaphore(self):
        """Test LockResponse for semaphore."""
        resp = LockResponse(
            lock_id="sem-456",
            path="/shared/room",
            mode="semaphore",
            max_holders=5,
            ttl=60,
            expires_at="2025-01-01T00:01:00+00:00",
            fence_token=43,
        )
        assert resp.mode == "semaphore"
        assert resp.max_holders == 5

    def test_lock_info_mutex_model(self):
        """Test LockInfoMutex model."""
        info = LockInfoMutex(
            lock_id="abc-123",
            holder_info="agent:test",
            acquired_at=1700000000.0,
            expires_at=1700000030.0,
            fence_token=1,
        )
        assert info.mode == "mutex"
        assert info.max_holders == 1

    def test_lock_info_semaphore_model(self):
        """Test LockInfoSemaphore model."""
        info = LockInfoSemaphore(
            max_holders=5,
            holders=[
                LockHolderResponse(
                    lock_id="h1",
                    acquired_at=1700000000.0,
                    expires_at=1700000030.0,
                ),
            ],
            current_holders=1,
            fence_token=2,
        )
        assert info.mode == "semaphore"
        assert info.current_holders == 1

    def test_lock_status_response_locked(self):
        """Test LockStatusResponse when locked."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=True,
            lock_info=LockInfoMutex(
                lock_id="abc-123",
                acquired_at=1700000000.0,
                expires_at=1700000030.0,
                fence_token=1,
            ),
        )
        assert status.locked is True
        assert status.lock_info.mode == "mutex"

    def test_lock_status_response_unlocked(self):
        """Test LockStatusResponse when not locked."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=False,
            lock_info=None,
        )
        assert status.locked is False

    def test_lock_list_response(self):
        """Test LockListResponse construction."""
        resp = LockListResponse(
            locks=[
                LockInfoMutex(
                    lock_id="l1",
                    acquired_at=1700000000.0,
                    expires_at=1700000030.0,
                    fence_token=1,
                ),
            ],
            count=1,
        )
        assert resp.count == 1
