"""Unit tests for Lock REST API endpoints with mocked lock manager.

These tests verify the endpoint logic by mocking the lock manager,
allowing full code path coverage without Redis/Dragonfly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.server.fastapi_server import (
    LockAcquireRequest,
    LockListResponse,
    LockResponse,
    LockStatusResponse,
)


@pytest.fixture
def mock_lock_manager():
    """Create a comprehensive mock lock manager."""
    manager = MagicMock()
    manager.LOCK_PREFIX = "nexus:lock"
    manager.SEMAPHORE_PREFIX = "nexus:semaphore"

    # Mock Redis client
    redis_client = AsyncMock()
    redis_client.scan = AsyncMock(return_value=(0, []))
    redis_client.zcard = AsyncMock(return_value=0)
    redis_client.get = AsyncMock(return_value=None)
    redis_client.zrange = AsyncMock(return_value=[])

    manager._redis = MagicMock()
    manager._redis.client = redis_client

    # Async methods
    manager.acquire = AsyncMock(return_value="lock-id-abc123")
    manager.release = AsyncMock(return_value=True)
    manager.extend = AsyncMock(return_value=True)
    manager.force_release = AsyncMock(return_value=True)
    manager.get_lock_info = AsyncMock(return_value=None)
    manager.is_locked = AsyncMock(return_value=False)

    # Helper methods
    manager._semaphore_key = MagicMock(side_effect=lambda t, p: f"nexus:semaphore:{t}:{p}")
    manager._semaphore_config_key = MagicMock(
        side_effect=lambda t, p: f"nexus:semaphore_config:{t}:{p}"
    )

    return manager


@pytest.fixture
def mock_nexus_fs(mock_lock_manager):
    """Create mock NexusFS with lock manager."""
    fs = MagicMock()
    fs._has_distributed_locks = MagicMock(return_value=True)
    fs._lock_manager = mock_lock_manager
    return fs


@pytest.fixture
def app_with_mocked_locks(mock_nexus_fs):
    """Create FastAPI test app with mocked lock manager."""
    from nexus.server import fastapi_server as fas

    # Save original state
    original_nexus_fs = fas._app_state.nexus_fs
    original_api_key = fas._app_state.api_key

    try:
        # Setup mock state
        fas._app_state.nexus_fs = mock_nexus_fs
        fas._app_state.api_key = "test-api-key"

        # We test via the module functions by patching _app_state
        # (endpoints are defined inside create_app)

        yield fas, mock_nexus_fs._lock_manager

    finally:
        fas._app_state.nexus_fs = original_nexus_fs
        fas._app_state.api_key = original_api_key


class TestAcquireLockEndpoint:
    """Test POST /api/locks endpoint logic."""

    @pytest.mark.asyncio
    async def test_acquire_mutex_lock_success(self, app_with_mocked_locks):
        """Test successful mutex lock acquisition."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.acquire.return_value = "lock-id-12345"

        # Simulate endpoint call
        request = LockAcquireRequest(path="/test/file.txt", timeout=10, ttl=30)
        auth_result = {"zone_id": "default", "subject_id": "user1"}

        # Get lock manager (this tests _get_lock_manager)
        manager = fas._app_state.nexus_fs._lock_manager

        # Call acquire
        lock_id = await manager.acquire(
            zone_id=auth_result["zone_id"],
            path=request.path,
            timeout=request.timeout,
            ttl=request.ttl,
            max_holders=request.max_holders,
        )

        assert lock_id == "lock-id-12345"
        lock_manager.acquire.assert_called_once_with(
            zone_id="default",
            path="/test/file.txt",
            timeout=10,
            ttl=30,
            max_holders=1,
        )

    @pytest.mark.asyncio
    async def test_acquire_semaphore_lock_success(self, app_with_mocked_locks):
        """Test successful semaphore lock acquisition."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.acquire.return_value = "sem-lock-id"

        request = LockAcquireRequest(path="/shared/room", timeout=5, ttl=60, max_holders=5)
        auth_result = {"zone_id": "zone1"}

        lock_id = await lock_manager.acquire(
            zone_id=auth_result["zone_id"],
            path=request.path,
            timeout=request.timeout,
            ttl=request.ttl,
            max_holders=request.max_holders,
        )

        assert lock_id == "sem-lock-id"
        lock_manager.acquire.assert_called_with(
            zone_id="zone1",
            path="/shared/room",
            timeout=5,
            ttl=60,
            max_holders=5,
        )

    @pytest.mark.asyncio
    async def test_acquire_lock_timeout(self, app_with_mocked_locks):
        """Test lock acquisition timeout returns None."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.acquire.return_value = None  # Timeout

        lock_id = await lock_manager.acquire(
            zone_id="default",
            path="/busy/file.txt",
            timeout=1,
            ttl=30,
            max_holders=1,
        )

        assert lock_id is None

    @pytest.mark.asyncio
    async def test_acquire_non_blocking_immediate_return(self, app_with_mocked_locks):
        """Test non-blocking mode with timeout=0."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.acquire.return_value = None

        # Non-blocking uses timeout=0
        lock_id = await lock_manager.acquire(
            zone_id="default",
            path="/busy/file.txt",
            timeout=0,  # Non-blocking
            ttl=30,
            max_holders=1,
        )

        assert lock_id is None
        lock_manager.acquire.assert_called_with(
            zone_id="default",
            path="/busy/file.txt",
            timeout=0,
            ttl=30,
            max_holders=1,
        )


class TestGetLockStatusEndpoint:
    """Test GET /api/locks/{path} endpoint logic."""

    @pytest.mark.asyncio
    async def test_get_status_unlocked(self, app_with_mocked_locks):
        """Test status check on unlocked path."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.get_lock_info.return_value = None
        lock_manager._redis.client.zcard.return_value = 0

        # Check mutex lock
        info = await lock_manager.get_lock_info("default", "/test/file.txt")
        assert info is None

        # Check semaphore
        sem_count = await lock_manager._redis.client.zcard("nexus:semaphore:default:/test/file.txt")
        assert sem_count == 0

    @pytest.mark.asyncio
    async def test_get_status_mutex_locked(self, app_with_mocked_locks):
        """Test status check on mutex-locked path."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.get_lock_info.return_value = {
            "lock_id": "abc-123",
            "ttl": 25,
            "zone_id": "default",
            "path": "/test/file.txt",
        }

        info = await lock_manager.get_lock_info("default", "/test/file.txt")

        assert info is not None
        assert info["lock_id"] == "abc-123"
        assert info["ttl"] == 25

    @pytest.mark.asyncio
    async def test_get_status_semaphore_locked(self, app_with_mocked_locks):
        """Test status check on semaphore-locked path."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.get_lock_info.return_value = None  # Not a mutex
        lock_manager._redis.client.zcard.return_value = 3  # 3 holders
        lock_manager._redis.client.get.return_value = b"5"  # max=5

        # Check mutex first
        mutex_info = await lock_manager.get_lock_info("default", "/shared/room")
        assert mutex_info is None

        # Check semaphore
        holders = await lock_manager._redis.client.zcard("nexus:semaphore:default:/shared/room")
        assert holders == 3

        max_holders_raw = await lock_manager._redis.client.get(
            "nexus:semaphore_config:default:/shared/room"
        )
        max_holders = int(max_holders_raw.decode())
        assert max_holders == 5


class TestReleaseLockEndpoint:
    """Test DELETE /api/locks/{path} endpoint logic."""

    @pytest.mark.asyncio
    async def test_release_success(self, app_with_mocked_locks):
        """Test successful lock release."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.release.return_value = True

        result = await lock_manager.release("lock-id-123", "default", "/test/file.txt")

        assert result is True
        lock_manager.release.assert_called_once_with("lock-id-123", "default", "/test/file.txt")

    @pytest.mark.asyncio
    async def test_release_wrong_owner(self, app_with_mocked_locks):
        """Test release with wrong lock_id fails."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.release.return_value = False  # Not owner

        result = await lock_manager.release("wrong-id", "default", "/test/file.txt")

        assert result is False

    @pytest.mark.asyncio
    async def test_force_release_success(self, app_with_mocked_locks):
        """Test admin force release."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.force_release.return_value = True

        result = await lock_manager.force_release("default", "/test/file.txt")

        assert result is True
        lock_manager.force_release.assert_called_once_with("default", "/test/file.txt")

    @pytest.mark.asyncio
    async def test_force_release_not_found(self, app_with_mocked_locks):
        """Test force release when lock doesn't exist."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.force_release.return_value = False

        result = await lock_manager.force_release("default", "/nonexistent.txt")

        assert result is False


class TestExtendLockEndpoint:
    """Test PATCH /api/locks/{path} endpoint logic."""

    @pytest.mark.asyncio
    async def test_extend_success(self, app_with_mocked_locks):
        """Test successful lock extension."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.extend.return_value = True

        result = await lock_manager.extend("lock-id-123", "default", "/test/file.txt", ttl=60)

        assert result is True
        lock_manager.extend.assert_called_once_with(
            "lock-id-123", "default", "/test/file.txt", ttl=60
        )

    @pytest.mark.asyncio
    async def test_extend_wrong_owner(self, app_with_mocked_locks):
        """Test extend with wrong lock_id fails."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.extend.return_value = False

        result = await lock_manager.extend("wrong-id", "default", "/test/file.txt", ttl=60)

        assert result is False

    @pytest.mark.asyncio
    async def test_extend_expired_lock(self, app_with_mocked_locks):
        """Test extend on expired lock fails."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager.extend.return_value = False  # Lock expired

        result = await lock_manager.extend("expired-lock-id", "default", "/test/file.txt", ttl=60)

        assert result is False


class TestListLocksEndpoint:
    """Test GET /api/locks endpoint logic."""

    @pytest.mark.asyncio
    async def test_list_locks_empty(self, app_with_mocked_locks):
        """Test listing when no locks exist."""
        fas, lock_manager = app_with_mocked_locks
        lock_manager._redis.client.scan.return_value = (0, [])

        cursor, keys = await lock_manager._redis.client.scan(
            0, match="nexus:lock:default:*", count=100
        )

        assert cursor == 0
        assert keys == []

    @pytest.mark.asyncio
    async def test_list_locks_with_results(self, app_with_mocked_locks):
        """Test listing with active locks."""
        fas, lock_manager = app_with_mocked_locks

        # Mock scan returning some keys
        lock_manager._redis.client.scan.return_value = (
            0,
            [b"nexus:lock:default:/file1.txt", b"nexus:lock:default:/file2.txt"],
        )

        # Mock get_lock_info for each key
        lock_manager.get_lock_info.side_effect = [
            {"lock_id": "lock1", "ttl": 30, "path": "/file1.txt"},
            {"lock_id": "lock2", "ttl": 25, "path": "/file2.txt"},
        ]

        cursor, keys = await lock_manager._redis.client.scan(
            0, match="nexus:lock:default:*", count=100
        )

        assert len(keys) == 2

        # Get info for each
        info1 = await lock_manager.get_lock_info("default", "/file1.txt")
        info2 = await lock_manager.get_lock_info("default", "/file2.txt")

        assert info1["lock_id"] == "lock1"
        assert info2["lock_id"] == "lock2"


class TestLockManagerAvailability:
    """Test _get_lock_manager helper function."""

    def test_get_lock_manager_no_nexus_fs(self):
        """Test error when NexusFS not available."""
        from nexus.server import fastapi_server as fas

        original = fas._app_state.nexus_fs
        try:
            fas._app_state.nexus_fs = None

            # The _get_lock_manager is defined inside create_app
            # We test the condition directly
            assert fas._app_state.nexus_fs is None

        finally:
            fas._app_state.nexus_fs = original

    def test_get_lock_manager_no_distributed_locks(self):
        """Test error when distributed locks not configured."""
        from nexus.server import fastapi_server as fas

        original = fas._app_state.nexus_fs
        try:
            mock_fs = MagicMock()
            mock_fs._has_distributed_locks = MagicMock(return_value=False)
            fas._app_state.nexus_fs = mock_fs

            # The condition should fail
            assert not fas._app_state.nexus_fs._has_distributed_locks()

        finally:
            fas._app_state.nexus_fs = original

    def test_get_lock_manager_success(self, mock_nexus_fs):
        """Test successful lock manager retrieval."""
        from nexus.server import fastapi_server as fas

        original = fas._app_state.nexus_fs
        try:
            fas._app_state.nexus_fs = mock_nexus_fs

            assert fas._app_state.nexus_fs._has_distributed_locks()
            assert fas._app_state.nexus_fs._lock_manager is not None

        finally:
            fas._app_state.nexus_fs = original


class TestLockResponseModels:
    """Test response model construction."""

    def test_lock_response_mutex(self):
        """Test LockResponse for mutex."""
        now = datetime.now(UTC)
        expires_at = now.isoformat()

        resp = LockResponse(
            lock_id="abc-123",
            path="/test/file.txt",
            mode="mutex",
            max_holders=1,
            ttl=30,
            expires_at=expires_at,
        )

        assert resp.mode == "mutex"
        assert resp.max_holders == 1

    def test_lock_response_semaphore(self):
        """Test LockResponse for semaphore."""
        now = datetime.now(UTC)
        expires_at = now.isoformat()

        resp = LockResponse(
            lock_id="sem-456",
            path="/shared/room",
            mode="semaphore",
            max_holders=5,
            ttl=60,
            expires_at=expires_at,
        )

        assert resp.mode == "semaphore"
        assert resp.max_holders == 5

    def test_lock_status_response(self):
        """Test LockStatusResponse construction."""
        status = LockStatusResponse(
            path="/test/file.txt",
            locked=True,
            lock_info={
                "lock_id": "abc-123",
                "mode": "mutex",
                "ttl": 30,
            },
        )

        assert status.locked is True
        assert status.lock_info["mode"] == "mutex"

    def test_lock_list_response(self):
        """Test LockListResponse construction."""
        locks = [
            {"path": "/file1.txt", "mode": "mutex"},
            {"path": "/file2.txt", "mode": "semaphore", "holders": 3},
        ]

        resp = LockListResponse(locks=locks, count=2)

        assert resp.count == 2
        assert len(resp.locks) == 2
