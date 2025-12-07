"""Tests for AsyncReBACManager.

These tests verify async ReBAC manager functionality.
"""

from unittest.mock import MagicMock

import pytest

from nexus.core.async_rebac_manager import (
    AsyncReBACManager,
    create_async_engine_from_url,
)


class TestAsyncReBACManager:
    """Test AsyncReBACManager functionality."""

    @pytest.fixture
    def mock_engine(self) -> MagicMock:
        """Create mock async engine."""
        engine = MagicMock()
        engine.url = "sqlite+aiosqlite:///test.db"
        return engine

    def test_init_with_defaults(self, mock_engine: MagicMock) -> None:
        """Test manager initialization with default params."""
        manager = AsyncReBACManager(mock_engine)
        assert manager.engine == mock_engine
        assert manager.cache_ttl_seconds == 300
        assert manager.max_depth == 50
        assert manager._l1_cache is not None

    def test_init_without_l1_cache(self, mock_engine: MagicMock) -> None:
        """Test manager initialization without L1 cache."""
        manager = AsyncReBACManager(mock_engine, enable_l1_cache=False)
        assert manager._l1_cache is None

    def test_init_with_custom_params(self, mock_engine: MagicMock) -> None:
        """Test manager initialization with custom params."""
        manager = AsyncReBACManager(
            engine=mock_engine,
            cache_ttl_seconds=600,
            max_depth=100,
            l1_cache_size=5000,
            l1_cache_ttl=120,
        )
        assert manager.cache_ttl_seconds == 600
        assert manager.max_depth == 100

    def test_is_postgresql_false(self, mock_engine: MagicMock) -> None:
        """Test PostgreSQL detection for SQLite."""
        manager = AsyncReBACManager(mock_engine)
        assert manager._is_postgresql() is False

    def test_is_postgresql_true(self) -> None:
        """Test PostgreSQL detection for PostgreSQL."""
        engine = MagicMock()
        engine.url = "postgresql+asyncpg://localhost/test"
        manager = AsyncReBACManager(engine)
        assert manager._is_postgresql() is True

    def test_get_namespace_not_loaded(self, mock_engine: MagicMock) -> None:
        """Test getting namespace before loading returns None."""
        manager = AsyncReBACManager(mock_engine)
        assert manager.get_namespace("file") is None

    def test_get_l1_cache_stats_with_cache(self, mock_engine: MagicMock) -> None:
        """Test getting L1 cache stats when cache is enabled."""
        manager = AsyncReBACManager(mock_engine, enable_l1_cache=True)
        stats = manager.get_l1_cache_stats()
        # Stats should have max_size key (from ReBACPermissionCache.get_stats())
        assert isinstance(stats, dict)
        assert "max_size" in stats

    def test_get_l1_cache_stats_without_cache(self, mock_engine: MagicMock) -> None:
        """Test getting L1 cache stats when cache is disabled."""
        manager = AsyncReBACManager(mock_engine, enable_l1_cache=False)
        stats = manager.get_l1_cache_stats()
        assert stats == {}


class TestCreateAsyncEngineFromUrl:
    """Test create_async_engine_from_url function."""

    def test_postgresql_url(self) -> None:
        """Test creating engine from PostgreSQL URL."""
        url = "postgresql://user:pass@localhost/db"
        engine = create_async_engine_from_url(url)
        assert "asyncpg" in str(engine.url)

    def test_sqlite_url(self) -> None:
        """Test creating engine from SQLite URL."""
        url = "sqlite:///test.db"
        engine = create_async_engine_from_url(url)
        assert "aiosqlite" in str(engine.url)


class TestAsyncReBACManagerCacheOps:
    """Test cache operations in AsyncReBACManager."""

    @pytest.fixture
    def manager_with_cache(self) -> AsyncReBACManager:
        """Create manager with L1 cache enabled."""
        engine = MagicMock()
        engine.url = "sqlite+aiosqlite:///test.db"
        return AsyncReBACManager(engine, enable_l1_cache=True)

    def test_l1_cache_direct_get_miss(self, manager_with_cache: AsyncReBACManager) -> None:
        """Test L1 cache miss using direct cache access."""
        assert manager_with_cache._l1_cache is not None
        # Cache uses positional args: subject_type, subject_id, permission, object_type, object_id, tenant_id
        result = manager_with_cache._l1_cache.get(
            "user", "alice", "read", "file", "/test.txt", "default"
        )
        # Cache miss returns None
        assert result is None

    def test_l1_cache_direct_set_and_get(self, manager_with_cache: AsyncReBACManager) -> None:
        """Test L1 cache set and get using direct cache access."""
        assert manager_with_cache._l1_cache is not None

        # Set cache entry (positional args: subject_type, subject_id, permission, object_type, object_id, result, tenant_id)
        manager_with_cache._l1_cache.set(
            "user", "alice", "read", "file", "/test.txt", True, "default"
        )

        # Should hit cache now
        result = manager_with_cache._l1_cache.get(
            "user", "alice", "read", "file", "/test.txt", "default"
        )
        assert result is True
