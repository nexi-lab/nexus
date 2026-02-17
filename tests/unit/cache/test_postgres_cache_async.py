"""Async concurrency tests for PostgreSQL cache classes.

Verifies that asyncio.to_thread() wrapping allows concurrent cache operations
without deadlock. Uses SQLite in-memory engine for speed (no PostgreSQL needed).

Note: These tests exercise the async wrapper layer. The actual SQL queries
use PostgreSQL syntax (UPSERT, UNNEST) which won't run on SQLite, so we
test the async wrapping pattern and the concurrency safety, not the SQL.
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from nexus.cache.postgres import (
    PostgresPermissionCache,
    PostgresResourceMapCache,
    PostgresTigerCache,
)


@pytest.fixture
def mock_engine() -> MagicMock:
    """Create a mock SQLAlchemy Engine that simulates sync connect/begin."""
    engine = MagicMock()

    # Mock connect() context manager
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    engine.connect.return_value = mock_conn

    # Mock begin() context manager
    mock_begin = MagicMock()
    mock_begin.__enter__ = MagicMock(return_value=mock_begin)
    mock_begin.__exit__ = MagicMock(return_value=False)
    engine.begin.return_value = mock_begin

    # Default: SELECT returns no rows
    mock_result = MagicMock()
    mock_result.fetchone.return_value = None
    mock_result.rowcount = 0
    mock_conn.execute.return_value = mock_result
    mock_begin.execute.return_value = mock_result

    return engine


# ---------------------------------------------------------------------------
# PostgresPermissionCache async concurrency
# ---------------------------------------------------------------------------


class TestPostgresPermissionCacheAsync:
    """Test concurrent async operations on PostgresPermissionCache."""

    @pytest.mark.asyncio
    async def test_concurrent_gets_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent get() calls should complete within 5s."""
        cache = PostgresPermissionCache(mock_engine, ttl=300, denial_ttl=60)

        async def do_get(i: int) -> bool | None:
            return await cache.get("user", f"user-{i}", "read", "file", f"/file-{i}.txt", "zone1")

        results = await asyncio.wait_for(
            asyncio.gather(*[do_get(i) for i in range(10)]),
            timeout=5.0,
        )
        assert len(results) == 10
        assert all(r is None for r in results)

    @pytest.mark.asyncio
    async def test_concurrent_sets_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent set() calls should complete within 5s."""
        cache = PostgresPermissionCache(mock_engine, ttl=300, denial_ttl=60)

        async def do_set(i: int) -> None:
            await cache.set("user", f"user-{i}", "read", "file", f"/file-{i}.txt", True, "zone1")

        await asyncio.wait_for(
            asyncio.gather(*[do_set(i) for i in range(10)]),
            timeout=5.0,
        )

    @pytest.mark.asyncio
    async def test_concurrent_mixed_operations(self, mock_engine: MagicMock) -> None:
        """Mix of get/set/invalidate should complete without deadlock."""
        cache = PostgresPermissionCache(mock_engine)

        tasks = [
            cache.get("user", "alice", "read", "file", "/a.txt", "zone1"),
            cache.set("user", "bob", "write", "file", "/b.txt", True, "zone1"),
            cache.invalidate_subject("user", "alice", "zone1"),
            cache.invalidate_object("file", "/a.txt", "zone1"),
            cache.clear("zone1"),
            cache.health_check(),
            cache.get_stats(),
        ]

        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=5.0,
        )
        assert len(results) == 7

    @pytest.mark.asyncio
    async def test_health_check_returns_bool(self, mock_engine: MagicMock) -> None:
        """health_check should return True when engine is responsive."""
        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.execute.return_value = MagicMock()
        mock_engine.connect.return_value = mock_conn

        cache = PostgresPermissionCache(mock_engine)
        result = await cache.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_returns_false_on_error(self, mock_engine: MagicMock) -> None:
        """health_check should return False when engine raises."""
        mock_engine.connect.side_effect = Exception("connection failed")
        cache = PostgresPermissionCache(mock_engine)
        result = await cache.health_check()
        assert result is False


# ---------------------------------------------------------------------------
# PostgresTigerCache async concurrency
# ---------------------------------------------------------------------------


class TestPostgresTigerCacheAsync:
    """Test concurrent async operations on PostgresTigerCache."""

    @pytest.mark.asyncio
    async def test_concurrent_get_bitmap_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent get_bitmap() calls should complete within 5s."""
        cache = PostgresTigerCache(mock_engine)

        async def do_get(i: int) -> tuple[bytes, int] | None:
            return await cache.get_bitmap("user", f"user-{i}", "read", "file", "zone1")

        results = await asyncio.wait_for(
            asyncio.gather(*[do_get(i) for i in range(10)]),
            timeout=5.0,
        )
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_concurrent_set_bitmap_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent set_bitmap() calls should complete within 5s."""
        cache = PostgresTigerCache(mock_engine)

        async def do_set(i: int) -> None:
            await cache.set_bitmap("user", f"user-{i}", "read", "file", "zone1", b"\x01", i)

        await asyncio.wait_for(
            asyncio.gather(*[do_set(i) for i in range(10)]),
            timeout=5.0,
        )

    @pytest.mark.asyncio
    async def test_concurrent_invalidate_no_deadlock(self, mock_engine: MagicMock) -> None:
        """Multiple concurrent invalidate() calls should complete."""
        cache = PostgresTigerCache(mock_engine)

        tasks = [
            cache.invalidate(zone_id="zone1"),
            cache.invalidate(subject_type="user"),
            cache.invalidate(permission="read"),
            cache.invalidate(),
        ]

        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=5.0,
        )
        assert len(results) == 4

    @pytest.mark.asyncio
    async def test_health_check(self, mock_engine: MagicMock) -> None:
        cache = PostgresTigerCache(mock_engine)
        result = await cache.health_check()
        assert result is True


# ---------------------------------------------------------------------------
# PostgresResourceMapCache async concurrency
# ---------------------------------------------------------------------------


class TestPostgresResourceMapCacheAsync:
    """Test concurrent async operations on PostgresResourceMapCache."""

    @pytest.mark.asyncio
    async def test_concurrent_get_int_id_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent get_int_id() calls should complete within 5s."""
        cache = PostgresResourceMapCache(mock_engine)

        async def do_get(i: int) -> int | None:
            return await cache.get_int_id("file", f"/file-{i}.txt", "zone1")

        results = await asyncio.wait_for(
            asyncio.gather(*[do_get(i) for i in range(10)]),
            timeout=5.0,
        )
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_concurrent_set_int_id_no_deadlock(self, mock_engine: MagicMock) -> None:
        """10 concurrent set_int_id() calls should complete within 5s."""
        cache = PostgresResourceMapCache(mock_engine)

        async def do_set(i: int) -> None:
            await cache.set_int_id("file", f"/file-{i}.txt", "zone1", i)

        await asyncio.wait_for(
            asyncio.gather(*[do_set(i) for i in range(10)]),
            timeout=5.0,
        )

    @pytest.mark.asyncio
    async def test_uses_asyncio_to_thread(self, mock_engine: MagicMock) -> None:
        """Verify that async methods delegate to asyncio.to_thread."""
        cache = PostgresResourceMapCache(mock_engine)

        with patch("nexus.cache.postgres.asyncio.to_thread", wraps=asyncio.to_thread) as mock_thread:
            await cache.get_int_id("file", "/a.txt", "zone1")
            mock_thread.assert_called_once()


# ---------------------------------------------------------------------------
# Cross-class concurrent operations
# ---------------------------------------------------------------------------


class TestCrossCacheConcurrency:
    """Test concurrent operations across different cache classes."""

    @pytest.mark.asyncio
    async def test_all_three_caches_concurrent(self, mock_engine: MagicMock) -> None:
        """Operations across all 3 PG caches should not deadlock."""
        perm = PostgresPermissionCache(mock_engine)
        tiger = PostgresTigerCache(mock_engine)
        resmap = PostgresResourceMapCache(mock_engine)

        tasks = [
            perm.get("user", "alice", "read", "file", "/a.txt", "zone1"),
            perm.health_check(),
            tiger.get_bitmap("user", "alice", "read", "file", "zone1"),
            tiger.health_check(),
            resmap.get_int_id("file", "/a.txt", "zone1"),
        ]

        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=5.0,
        )
        assert len(results) == 5
