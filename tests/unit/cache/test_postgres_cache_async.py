"""Test PostgreSQL cache async concurrency safety (Issue #1524).

Verifies that PostgreSQL cache classes properly use asyncio.to_thread()
and don't deadlock. Uses SQLite in-memory for structural tests; concurrent
multi-thread tests require a real PostgreSQL instance.
"""

import asyncio

import pytest

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import StaticPool

    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False

@pytest.fixture
def pg_engine():
    """Create SQLite in-memory engine for testing."""
    if not SQLALCHEMY_AVAILABLE:
        pytest.skip("sqlalchemy not installed")

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS rebac_check_cache (
                cache_id TEXT,
                zone_id TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id TEXT NOT NULL,
                result INTEGER NOT NULL,
                computed_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP NOT NULL,
                UNIQUE(zone_id, subject_type, subject_id, permission, object_type, object_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tiger_cache (
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                zone_id TEXT NOT NULL,
                bitmap_data BLOB,
                revision INTEGER,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                UNIQUE(subject_type, subject_id, permission, resource_type, zone_id)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tiger_resource_map (
                resource_int_id INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                created_at TIMESTAMP,
                UNIQUE(resource_type, resource_id)
            )
        """))

    yield engine
    engine.dispose()

def _seed_permission(engine, subject_id: str = "alice", zone_id: str = "z1"):
    """Insert a permission cache entry directly via SQL."""
    import uuid
    from datetime import UTC, datetime, timedelta

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT OR REPLACE INTO rebac_check_cache
            (cache_id, zone_id, subject_type, subject_id, permission,
             object_type, object_id, result, computed_at, expires_at)
            VALUES (:cache_id, :zone_id, :st, :sid, :perm, :ot, :oid,
                    :result, :computed_at, :expires_at)
        """), {
            "cache_id": str(uuid.uuid4()),
            "zone_id": zone_id,
            "st": "user", "sid": subject_id,
            "perm": "read", "ot": "file", "oid": "/a.txt",
            "result": 1,
            "computed_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        })

def _seed_tiger(engine, subject_id: str = "alice"):
    """Insert a tiger cache entry directly."""
    from datetime import UTC, datetime

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT OR REPLACE INTO tiger_cache
            (subject_type, subject_id, permission, resource_type, zone_id,
             bitmap_data, revision, created_at, updated_at)
            VALUES (:st, :sid, :perm, :rt, :zid, :bitmap, :rev, :ca, :ua)
        """), {
            "st": "user", "sid": subject_id,
            "perm": "read", "rt": "file", "zid": "z1",
            "bitmap": b"\x01\x02", "rev": 1,
            "ca": datetime.now(UTC), "ua": datetime.now(UTC),
        })

def _seed_resource_map(engine, resource_id: str = "/test.txt"):
    """Insert a resource map entry directly."""
    from datetime import UTC, datetime

    with engine.begin() as conn:
        conn.execute(text("""
            INSERT OR IGNORE INTO tiger_resource_map
            (resource_type, resource_id, created_at)
            VALUES (:rt, :rid, :ca)
        """), {
            "rt": "file", "rid": resource_id,
            "ca": datetime.now(UTC),
        })

@pytest.mark.skipif(not SQLALCHEMY_AVAILABLE, reason="sqlalchemy not installed")
class TestPostgresPermissionCacheAsync:
    """Test permission cache async wrapping."""

    @pytest.mark.asyncio
    async def test_get_returns_cached_value(self, pg_engine):
        """get() should return cached permission via asyncio.to_thread."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice")

        result = await cache.get("user", "alice", "read", "file", "/a.txt", "z1")
        assert result is True

    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self, pg_engine):
        """get() miss should return None."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        result = await cache.get("user", "unknown", "read", "file", "/x.txt", "z1")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate_subject(self, pg_engine):
        """invalidate_subject() should delete matching entries."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice")
        count = await cache.invalidate_subject("user", "alice", "z1")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_invalidate_object(self, pg_engine):
        """invalidate_object() should delete matching entries."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice")
        count = await cache.invalidate_object("file", "/a.txt", "z1")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_clear_zone(self, pg_engine):
        """clear(zone_id) should delete zone entries."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice", "z1")
        count = await cache.clear(zone_id="z1")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_clear_all(self, pg_engine):
        """clear() without zone should delete all entries."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice", "z1")
        _seed_permission(pg_engine, "bob", "z2")
        count = await cache.clear()
        assert count >= 2

    @pytest.mark.asyncio
    async def test_health_check(self, pg_engine):
        """health_check() should return True for healthy engine."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        result = await cache.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_get_stats(self, pg_engine):
        """get_stats() should return dict with backend info."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        stats = await cache.get_stats()
        assert stats["backend"] == "postgres"
        assert "valid_entries" in stats

    @pytest.mark.asyncio
    async def test_sequential_operations_complete(self, pg_engine):
        """Sequential async operations should complete without deadlock."""
        from nexus.cache.postgres import PostgresPermissionCache

        cache = PostgresPermissionCache(pg_engine, ttl=300)
        _seed_permission(pg_engine, "alice")

        # Run sequential operations under a timeout
        async def sequential():
            await cache.get("user", "alice", "read", "file", "/a.txt", "z1")
            await cache.health_check()
            await cache.get_stats()
            await cache.invalidate_subject("user", "alice", "z1")
            await cache.clear()

        await asyncio.wait_for(sequential(), timeout=5.0)

@pytest.mark.skipif(not SQLALCHEMY_AVAILABLE, reason="sqlalchemy not installed")
class TestPostgresTigerCacheAsync:
    """Test Tiger cache async wrapping."""

    @pytest.mark.asyncio
    async def test_get_bitmap(self, pg_engine):
        """get_bitmap() should return cached data via asyncio.to_thread."""
        from nexus.cache.postgres import PostgresTigerCache

        cache = PostgresTigerCache(pg_engine)
        _seed_tiger(pg_engine, "alice")

        result = await cache.get_bitmap("user", "alice", "read", "file", "z1")
        assert result is not None
        bitmap_data, revision = result
        assert bitmap_data == b"\x01\x02"
        assert revision == 1

    @pytest.mark.asyncio
    async def test_get_bitmap_miss(self, pg_engine):
        """get_bitmap() miss should return None."""
        from nexus.cache.postgres import PostgresTigerCache

        cache = PostgresTigerCache(pg_engine)
        result = await cache.get_bitmap("user", "nobody", "read", "file", "z1")
        assert result is None

    @pytest.mark.asyncio
    async def test_invalidate(self, pg_engine):
        """invalidate() should delete matching entries."""
        from nexus.cache.postgres import PostgresTigerCache

        cache = PostgresTigerCache(pg_engine)
        _seed_tiger(pg_engine, "alice")
        count = await cache.invalidate(subject_type="user", subject_id="alice")
        assert count >= 1

    @pytest.mark.asyncio
    async def test_health_check(self, pg_engine):
        """health_check() should return True."""
        from nexus.cache.postgres import PostgresTigerCache

        cache = PostgresTigerCache(pg_engine)
        assert await cache.health_check() is True

@pytest.mark.skipif(not SQLALCHEMY_AVAILABLE, reason="sqlalchemy not installed")
class TestPostgresResourceMapCacheAsync:
    """Test resource map cache async wrapping."""

    @pytest.mark.asyncio
    async def test_get_int_id(self, pg_engine):
        """get_int_id() should return mapped ID."""
        from nexus.cache.postgres import PostgresResourceMapCache

        cache = PostgresResourceMapCache(pg_engine)
        _seed_resource_map(pg_engine, "/test.txt")
        result = await cache.get_int_id("file", "/test.txt", "z1")
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_int_id_miss(self, pg_engine):
        """get_int_id() miss should return None."""
        from nexus.cache.postgres import PostgresResourceMapCache

        cache = PostgresResourceMapCache(pg_engine)
        result = await cache.get_int_id("file", "/missing.txt", "z1")
        assert result is None
