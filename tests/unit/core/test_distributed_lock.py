"""Unit tests for distributed lock manager.

Tests cover:
- RedisLockManager (mocked Redis)
- Lock acquisition, release, extend
- Lock ownership verification
- TTL-based expiration
- Factory functions

Related: Issue #1106 Block 2
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.core.distributed_lock import (
    EXTEND_SCRIPT,
    RELEASE_SCRIPT,
    DistributedLockManager,
    LockManagerBase,
    LockManagerProtocol,
    RedisLockManager,
    create_lock_manager,
    get_distributed_lock_manager,
    set_distributed_lock_manager,
)

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_redis_client():
    """Create a mock DragonflyClient."""
    client = MagicMock()
    client.client = MagicMock()
    client.health_check = AsyncMock(return_value=True)

    # Setup async methods on the inner client
    client.client.set = AsyncMock(return_value=True)
    client.client.get = AsyncMock(return_value=None)
    client.client.delete = AsyncMock(return_value=1)
    client.client.exists = AsyncMock(return_value=0)
    client.client.ttl = AsyncMock(return_value=30)
    client.client.script_load = AsyncMock(side_effect=["release_sha", "extend_sha"])
    client.client.evalsha = AsyncMock(return_value=1)

    return client


@pytest.fixture
def lock_manager(mock_redis_client):
    """Create a RedisLockManager with mocked client."""
    return RedisLockManager(mock_redis_client)


# =============================================================================
# RedisLockManager Tests
# =============================================================================


class TestRedisLockManager:
    """Tests for RedisLockManager with mocked Redis."""

    def test_lock_key_format(self, lock_manager):
        """Test lock key generation."""
        key = lock_manager._lock_key("tenant1", "/inbox/test.txt")
        assert key == "nexus:lock:tenant1:/inbox/test.txt"

        key = lock_manager._lock_key("default", "/file.txt")
        assert key == "nexus:lock:default:/file.txt"

    @pytest.mark.asyncio
    async def test_acquire_success(self, lock_manager, mock_redis_client):
        """Test successful lock acquisition."""
        mock_redis_client.client.set = AsyncMock(return_value=True)

        lock_id = await lock_manager.acquire("tenant1", "/file.txt", timeout=5.0, ttl=30.0)

        assert lock_id is not None
        assert isinstance(lock_id, str)
        assert len(lock_id) == 36  # UUID format

        # Verify set was called with correct arguments
        mock_redis_client.client.set.assert_called_once()
        call_args = mock_redis_client.client.set.call_args
        assert call_args[0][0] == "nexus:lock:tenant1:/file.txt"
        assert call_args[0][1] == lock_id
        assert call_args[1]["nx"] is True
        assert call_args[1]["px"] == 30000  # TTL in milliseconds

    @pytest.mark.asyncio
    async def test_acquire_timeout(self, lock_manager, mock_redis_client):
        """Test lock acquisition timeout."""
        # Lock is held by another client
        mock_redis_client.client.set = AsyncMock(return_value=False)

        lock_id = await lock_manager.acquire("tenant1", "/file.txt", timeout=0.2, ttl=30.0)

        assert lock_id is None
        # Should have tried multiple times
        assert mock_redis_client.client.set.call_count > 1

    @pytest.mark.asyncio
    async def test_acquire_retry_succeeds(self, lock_manager, mock_redis_client):
        """Test lock acquisition succeeds after retry."""
        # First attempt fails, second succeeds
        mock_redis_client.client.set = AsyncMock(side_effect=[False, True])

        lock_id = await lock_manager.acquire("tenant1", "/file.txt", timeout=5.0, ttl=30.0)

        assert lock_id is not None
        assert mock_redis_client.client.set.call_count == 2

    @pytest.mark.asyncio
    async def test_release_success(self, lock_manager, mock_redis_client):
        """Test successful lock release."""
        # evalsha returns 1 for successful release
        mock_redis_client.client.evalsha = AsyncMock(return_value=1)

        released = await lock_manager.release("lock-id-123", "tenant1", "/file.txt")

        assert released is True
        mock_redis_client.client.script_load.assert_called()
        mock_redis_client.client.evalsha.assert_called_once()

    @pytest.mark.asyncio
    async def test_release_not_owned(self, lock_manager, mock_redis_client):
        """Test release fails if not owner."""
        # evalsha returns 0 if lock not owned
        mock_redis_client.client.evalsha = AsyncMock(return_value=0)

        released = await lock_manager.release("wrong-lock-id", "tenant1", "/file.txt")

        assert released is False

    @pytest.mark.asyncio
    async def test_release_expired(self, lock_manager, mock_redis_client):
        """Test release fails if lock expired."""
        # evalsha returns 0 if lock doesn't exist
        mock_redis_client.client.evalsha = AsyncMock(return_value=0)

        released = await lock_manager.release("expired-lock-id", "tenant1", "/file.txt")

        assert released is False

    @pytest.mark.asyncio
    async def test_extend_success(self, lock_manager, mock_redis_client):
        """Test successful lock extension."""
        # evalsha returns 1 for successful extend
        mock_redis_client.client.evalsha = AsyncMock(return_value=1)

        extended = await lock_manager.extend("lock-id-123", "tenant1", "/file.txt", ttl=60.0)

        assert extended is True
        mock_redis_client.client.evalsha.assert_called_once()

    @pytest.mark.asyncio
    async def test_extend_not_owned(self, lock_manager, mock_redis_client):
        """Test extend fails if not owner."""
        mock_redis_client.client.evalsha = AsyncMock(return_value=0)

        extended = await lock_manager.extend("wrong-lock-id", "tenant1", "/file.txt")

        assert extended is False

    @pytest.mark.asyncio
    async def test_extend_expired(self, lock_manager, mock_redis_client):
        """Test extend fails if lock expired."""
        mock_redis_client.client.evalsha = AsyncMock(return_value=0)

        extended = await lock_manager.extend("expired-lock-id", "tenant1", "/file.txt")

        assert extended is False

    @pytest.mark.asyncio
    async def test_is_locked_true(self, lock_manager, mock_redis_client):
        """Test is_locked returns True when lock exists."""
        mock_redis_client.client.exists = AsyncMock(return_value=1)

        result = await lock_manager.is_locked("tenant1", "/file.txt")

        assert result is True
        mock_redis_client.client.exists.assert_called_with("nexus:lock:tenant1:/file.txt")

    @pytest.mark.asyncio
    async def test_is_locked_false(self, lock_manager, mock_redis_client):
        """Test is_locked returns False when no lock."""
        mock_redis_client.client.exists = AsyncMock(return_value=0)

        result = await lock_manager.is_locked("tenant1", "/file.txt")

        assert result is False

    @pytest.mark.asyncio
    async def test_get_lock_info_exists(self, lock_manager, mock_redis_client):
        """Test getting lock info when lock exists."""
        mock_redis_client.client.get = AsyncMock(return_value=b"lock-id-123")
        mock_redis_client.client.ttl = AsyncMock(return_value=25)

        info = await lock_manager.get_lock_info("tenant1", "/file.txt")

        assert info is not None
        assert info["lock_id"] == "lock-id-123"
        assert info["ttl"] == 25
        assert info["tenant_id"] == "tenant1"
        assert info["path"] == "/file.txt"

    @pytest.mark.asyncio
    async def test_get_lock_info_not_exists(self, lock_manager, mock_redis_client):
        """Test getting lock info when no lock."""
        mock_redis_client.client.get = AsyncMock(return_value=None)

        info = await lock_manager.get_lock_info("tenant1", "/file.txt")

        assert info is None

    @pytest.mark.asyncio
    async def test_force_release_exists(self, lock_manager, mock_redis_client):
        """Test force release when lock exists."""
        mock_redis_client.client.delete = AsyncMock(return_value=1)

        released = await lock_manager.force_release("tenant1", "/file.txt")

        assert released is True
        mock_redis_client.client.delete.assert_called_with("nexus:lock:tenant1:/file.txt")

    @pytest.mark.asyncio
    async def test_force_release_not_exists(self, lock_manager, mock_redis_client):
        """Test force release when no lock."""
        mock_redis_client.client.delete = AsyncMock(return_value=0)

        released = await lock_manager.force_release("tenant1", "/file.txt")

        assert released is False

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, lock_manager, mock_redis_client):
        """Test health check when Redis is healthy."""
        result = await lock_manager.health_check()

        assert result is True
        mock_redis_client.health_check.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, lock_manager, mock_redis_client):
        """Test health check when Redis is unhealthy."""
        mock_redis_client.health_check = AsyncMock(return_value=False)

        result = await lock_manager.health_check()

        assert result is False


class TestLuaScripts:
    """Tests for Lua scripts correctness."""

    def test_release_script_format(self):
        """Test release script has correct structure."""
        assert "redis.call" in RELEASE_SCRIPT
        assert "KEYS[1]" in RELEASE_SCRIPT
        assert "ARGV[1]" in RELEASE_SCRIPT
        assert "get" in RELEASE_SCRIPT
        assert "del" in RELEASE_SCRIPT

    def test_extend_script_format(self):
        """Test extend script has correct structure."""
        assert "redis.call" in EXTEND_SCRIPT
        assert "KEYS[1]" in EXTEND_SCRIPT
        assert "ARGV[1]" in EXTEND_SCRIPT
        assert "ARGV[2]" in EXTEND_SCRIPT
        assert "get" in EXTEND_SCRIPT
        assert "expire" in EXTEND_SCRIPT


# =============================================================================
# Lock Workflow Tests
# =============================================================================


class TestLockWorkflow:
    """Tests for complete lock workflows."""

    @pytest.mark.asyncio
    async def test_acquire_release_workflow(self, lock_manager, mock_redis_client):
        """Test complete acquire-release workflow."""
        mock_redis_client.client.set = AsyncMock(return_value=True)
        mock_redis_client.client.evalsha = AsyncMock(return_value=1)

        # Acquire
        lock_id = await lock_manager.acquire("tenant1", "/file.txt")
        assert lock_id is not None

        # Release
        released = await lock_manager.release(lock_id, "tenant1", "/file.txt")
        assert released is True

    @pytest.mark.asyncio
    async def test_acquire_extend_release_workflow(self, lock_manager, mock_redis_client):
        """Test acquire-extend-release workflow (heartbeat pattern)."""
        mock_redis_client.client.set = AsyncMock(return_value=True)
        mock_redis_client.client.evalsha = AsyncMock(return_value=1)

        # Acquire
        lock_id = await lock_manager.acquire("tenant1", "/file.txt", ttl=10.0)
        assert lock_id is not None

        # Extend (heartbeat)
        extended = await lock_manager.extend(lock_id, "tenant1", "/file.txt", ttl=10.0)
        assert extended is True

        # Extend again
        extended = await lock_manager.extend(lock_id, "tenant1", "/file.txt", ttl=10.0)
        assert extended is True

        # Release
        released = await lock_manager.release(lock_id, "tenant1", "/file.txt")
        assert released is True

    @pytest.mark.asyncio
    async def test_double_acquire_fails(self, lock_manager, mock_redis_client):
        """Test that double acquire fails (lock is exclusive)."""
        # First acquire succeeds, subsequent attempts fail
        # With exponential backoff, more attempts may be made, so provide many False values
        mock_redis_client.client.set = AsyncMock(
            side_effect=[True] + [False] * 50  # First succeeds, rest fail
        )

        lock_id1 = await lock_manager.acquire("tenant1", "/file.txt", timeout=5.0)
        assert lock_id1 is not None

        lock_id2 = await lock_manager.acquire("tenant1", "/file.txt", timeout=0.2)
        assert lock_id2 is None


# =============================================================================
# Factory and Singleton Tests
# =============================================================================


class TestLockManagerFactory:
    """Tests for lock manager factory function."""

    def test_create_redis_lock_manager(self, mock_redis_client):
        """Test creating Redis lock manager via factory."""
        manager = create_lock_manager(backend="redis", redis_client=mock_redis_client)

        assert isinstance(manager, RedisLockManager)
        assert isinstance(manager, LockManagerBase)

    def test_create_redis_requires_client(self):
        """Test that Redis backend requires redis_client."""
        with pytest.raises(ValueError, match="redis_client is required"):
            create_lock_manager(backend="redis")

    def test_unsupported_backend(self, mock_redis_client):
        """Test error for unsupported backend."""
        with pytest.raises(ValueError, match="Unsupported lock manager backend"):
            create_lock_manager(backend="unknown", redis_client=mock_redis_client)


class TestDistributedLockManagerSingleton:
    """Tests for distributed lock manager singleton management."""

    def test_get_set_distributed_lock_manager(self, mock_redis_client):
        """Test setting and getting distributed lock manager."""
        # Initially None
        set_distributed_lock_manager(None)
        assert get_distributed_lock_manager() is None

        # Set a manager
        manager = RedisLockManager(mock_redis_client)
        set_distributed_lock_manager(manager)
        assert get_distributed_lock_manager() is manager

        # Clear
        set_distributed_lock_manager(None)
        assert get_distributed_lock_manager() is None

    def test_distributed_lock_manager_alias(self, mock_redis_client):
        """Test that DistributedLockManager is an alias for RedisLockManager."""
        assert DistributedLockManager is RedisLockManager

        manager = DistributedLockManager(mock_redis_client)
        assert isinstance(manager, RedisLockManager)


# =============================================================================
# Protocol Compliance Tests
# =============================================================================


class TestLockManagerProtocol:
    """Tests for LockManagerProtocol compliance."""

    def test_redis_lock_manager_implements_protocol(self, mock_redis_client):
        """Test that RedisLockManager implements LockManagerProtocol."""
        manager = RedisLockManager(mock_redis_client)

        # Check protocol compliance via runtime_checkable
        assert isinstance(manager, LockManagerProtocol)

        # Check all required methods exist
        assert hasattr(manager, "acquire")
        assert hasattr(manager, "release")
        assert hasattr(manager, "extend")
        assert hasattr(manager, "health_check")


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestLockManagerErrorHandling:
    """Tests for lock manager error handling."""

    @pytest.mark.asyncio
    async def test_release_handles_redis_error(self, lock_manager, mock_redis_client):
        """Test release handles Redis errors gracefully."""
        mock_redis_client.client.evalsha = AsyncMock(side_effect=Exception("Redis error"))

        released = await lock_manager.release("lock-id", "tenant1", "/file.txt")

        assert released is False

    @pytest.mark.asyncio
    async def test_extend_handles_redis_error(self, lock_manager, mock_redis_client):
        """Test extend handles Redis errors gracefully."""
        mock_redis_client.client.evalsha = AsyncMock(side_effect=Exception("Redis error"))

        extended = await lock_manager.extend("lock-id", "tenant1", "/file.txt")

        assert extended is False

    @pytest.mark.asyncio
    async def test_health_check_handles_error(self, lock_manager, mock_redis_client):
        """Test health check handles errors gracefully."""
        mock_redis_client.health_check = AsyncMock(side_effect=Exception("Connection error"))

        result = await lock_manager.health_check()

        assert result is False


# =============================================================================
# Default Values Tests
# =============================================================================


class TestLockManagerDefaults:
    """Tests for lock manager default values."""

    def test_default_ttl(self):
        """Test default TTL value."""
        assert LockManagerBase.DEFAULT_TTL == 30.0

    def test_default_timeout(self):
        """Test default timeout value."""
        assert LockManagerBase.DEFAULT_TIMEOUT == 30.0

    def test_retry_backoff_params(self, mock_redis_client):
        """Test retry backoff parameters (exponential with jitter)."""
        manager = RedisLockManager(mock_redis_client)
        assert manager.RETRY_BASE_INTERVAL == 0.05  # Start at 50ms
        assert manager.RETRY_MAX_INTERVAL == 1.0  # Cap at 1s
        assert manager.RETRY_MULTIPLIER == 2.0  # Double each retry
        assert manager.RETRY_JITTER == 0.5  # 50% jitter

    def test_lock_prefix(self, mock_redis_client):
        """Test lock key prefix."""
        manager = RedisLockManager(mock_redis_client)
        assert manager.LOCK_PREFIX == "nexus:lock"
