"""Protocol-conformance tests for PostgreSQL cache implementations (#1251).

Tests PostgresPermissionCache, PostgresTigerCache, and PostgresResourceMapCache
against real PostgreSQL to verify SQL correctness, TTL behavior, zone isolation,
and Protocol conformance.

Requirements:
    - PostgreSQL running at postgresql://postgres:nexus@localhost:5432/nexus
    - Start with: docker compose -f docker-compose.demo.yml up postgres -d

Run tests with:
    pytest tests/integration/test_postgres_cache.py -v
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Generator
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from nexus.cache.postgres import (
    PostgresPermissionCache,
    PostgresResourceMapCache,
    PostgresTigerCache,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

PG_URL = os.environ.get(
    "TEST_POSTGRES_URL",
    "postgresql://postgres:nexus@localhost:5432/nexus",
)


def _pg_is_available() -> bool:
    """Check if PostgreSQL is reachable."""
    try:
        engine = create_engine(PG_URL)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _pg_is_available(),
        reason="PostgreSQL not available. Start with: docker compose -f docker-compose.demo.yml up postgres -d",
    ),
]


@pytest.fixture(scope="module")
def pg_engine() -> Generator[Engine, None, None]:
    """Create a shared PostgreSQL engine for the test module.

    Drops and recreates cache tables to ensure schema matches current models.
    Only touches cache tables — not the full database.
    """
    engine = create_engine(PG_URL)
    from nexus.storage.models import Base

    # Get only the cache table objects from the ORM
    cache_tables = [
        Base.metadata.tables[name]
        for name in ("rebac_check_cache", "tiger_cache", "tiger_resource_map")
        if name in Base.metadata.tables
    ]

    # Drop and recreate to ensure schema matches current models
    Base.metadata.drop_all(engine, tables=cache_tables)
    Base.metadata.create_all(engine, tables=cache_tables)

    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(pg_engine: Engine) -> None:
    """Clean cache tables before each test for isolation."""
    with pg_engine.begin() as conn:
        conn.execute(text("DELETE FROM rebac_check_cache"))
        conn.execute(text("DELETE FROM tiger_cache"))
        # Don't delete tiger_resource_map — auto-increment IDs are global


# ---------------------------------------------------------------------------
# PostgresPermissionCache Tests
# ---------------------------------------------------------------------------


class TestPostgresPermissionCache:
    """Protocol-conformance tests for PostgresPermissionCache."""

    @pytest.fixture
    def cache(self, pg_engine: Engine) -> PostgresPermissionCache:
        return PostgresPermissionCache(engine=pg_engine, ttl=300, denial_ttl=60)

    async def test_cache_miss_returns_none(self, cache: PostgresPermissionCache) -> None:
        result = await cache.get("user", "alice", "read", "file", "/docs/a.md", "zone1")
        assert result is None

    async def test_set_and_get_grant(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/docs/a.md", True, "zone1")
        result = await cache.get("user", "alice", "read", "file", "/docs/a.md", "zone1")
        assert result is True

    async def test_set_and_get_denial(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "bob", "write", "file", "/secret", False, "zone1")
        result = await cache.get("user", "bob", "write", "file", "/secret", "zone1")
        assert result is False

    async def test_ttl_expiry(self, pg_engine: Engine) -> None:
        """Entries with expired TTL should not be returned."""
        # Insert with TTL=1 second
        short_cache = PostgresPermissionCache(engine=pg_engine, ttl=1, denial_ttl=1)
        await short_cache.set("user", "alice", "read", "file", "/tmp", True, "zone1")

        # Should be available immediately
        result = await short_cache.get("user", "alice", "read", "file", "/tmp", "zone1")
        assert result is True

        # Wait for expiry
        await asyncio.sleep(1.5)

        # Should be expired
        result = await short_cache.get("user", "alice", "read", "file", "/tmp", "zone1")
        assert result is None

    async def test_upsert_overwrites(self, cache: PostgresPermissionCache) -> None:
        """Setting the same key twice should update the result."""
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        assert await cache.get("user", "alice", "read", "file", "/a", "zone1") is True

        await cache.set("user", "alice", "read", "file", "/a", False, "zone1")
        assert await cache.get("user", "alice", "read", "file", "/a", "zone1") is False

    async def test_invalidate_subject(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        await cache.set("user", "alice", "write", "file", "/b", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/a", True, "zone1")

        deleted = await cache.invalidate_subject("user", "alice", "zone1")
        assert deleted == 2

        # Bob untouched
        assert await cache.get("user", "bob", "read", "file", "/a", "zone1") is True
        # Alice gone
        assert await cache.get("user", "alice", "read", "file", "/a", "zone1") is None

    async def test_invalidate_object(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/shared", True, "zone1")
        await cache.set("user", "bob", "write", "file", "/shared", True, "zone1")
        await cache.set("user", "alice", "read", "file", "/private", True, "zone1")

        deleted = await cache.invalidate_object("file", "/shared", "zone1")
        assert deleted == 2

        # /private untouched
        assert await cache.get("user", "alice", "read", "file", "/private", "zone1") is True

    async def test_invalidate_subject_object(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        await cache.set("user", "alice", "write", "file", "/a", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/a", True, "zone1")

        deleted = await cache.invalidate_subject_object("user", "alice", "file", "/a", "zone1")
        assert deleted == 2  # alice's read + write on /a

        # Bob untouched
        assert await cache.get("user", "bob", "read", "file", "/a", "zone1") is True

    async def test_clear_zone(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/b", True, "zone1")
        await cache.set("user", "alice", "read", "file", "/c", True, "zone2")

        deleted = await cache.clear(zone_id="zone1")
        assert deleted == 2

        # zone2 untouched
        assert await cache.get("user", "alice", "read", "file", "/c", "zone2") is True

    async def test_clear_all(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        await cache.set("user", "bob", "read", "file", "/b", True, "zone2")

        deleted = await cache.clear()
        assert deleted == 2

    async def test_zone_isolation(self, cache: PostgresPermissionCache) -> None:
        """Zone1 entries must not be visible from zone2 queries (P0 security)."""
        await cache.set("user", "alice", "read", "file", "/secret", True, "zone1")

        # Same subject/permission/object but different zone — must be None
        result = await cache.get("user", "alice", "read", "file", "/secret", "zone2")
        assert result is None

    async def test_get_stats(self, cache: PostgresPermissionCache) -> None:
        await cache.set("user", "alice", "read", "file", "/a", True, "zone1")
        stats = await cache.get_stats()
        assert stats["backend"] == "postgres"
        assert stats["valid_entries"] >= 1

    async def test_health_check(self, cache: PostgresPermissionCache) -> None:
        assert await cache.health_check() is True


# ---------------------------------------------------------------------------
# PostgresTigerCache Tests
# ---------------------------------------------------------------------------


class TestPostgresTigerCache:
    """Protocol-conformance tests for PostgresTigerCache."""

    @pytest.fixture
    def cache(self, pg_engine: Engine) -> PostgresTigerCache:
        return PostgresTigerCache(engine=pg_engine)

    async def test_cache_miss_returns_none(self, cache: PostgresTigerCache) -> None:
        result = await cache.get_bitmap("user", "unknown", "read", "file", "zone1")
        assert result is None

    async def test_set_and_get_bitmap(self, cache: PostgresTigerCache) -> None:
        bitmap = b"\x01\x02\x03\x04\x05"
        revision = 42

        await cache.set_bitmap("user", "alice", "read", "file", "zone1", bitmap, revision)
        result = await cache.get_bitmap("user", "alice", "read", "file", "zone1")

        assert result is not None
        data, rev = result
        assert data == bitmap
        assert rev == 42

    async def test_upsert_overwrites_bitmap(self, cache: PostgresTigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"old", 1)
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"new", 2)

        result = await cache.get_bitmap("user", "alice", "read", "file", "zone1")
        assert result is not None
        data, rev = result
        assert data == b"new"
        assert rev == 2

    async def test_invalidate_by_subject(self, cache: PostgresTigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"bm1", 1)
        await cache.set_bitmap("user", "alice", "write", "file", "zone1", b"bm2", 2)
        await cache.set_bitmap("user", "bob", "read", "file", "zone1", b"bm3", 3)

        deleted = await cache.invalidate(subject_type="user", subject_id="alice")
        assert deleted == 2

        # Bob untouched
        assert await cache.get_bitmap("user", "bob", "read", "file", "zone1") is not None

    async def test_invalidate_by_permission(self, cache: PostgresTigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"bm1", 1)
        await cache.set_bitmap("user", "alice", "write", "file", "zone1", b"bm2", 2)

        deleted = await cache.invalidate(permission="read")
        assert deleted == 1

        # write untouched
        assert await cache.get_bitmap("user", "alice", "write", "file", "zone1") is not None

    async def test_invalidate_all(self, cache: PostgresTigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"bm1", 1)
        await cache.set_bitmap("user", "bob", "write", "file", "zone2", b"bm2", 2)

        deleted = await cache.invalidate()
        assert deleted == 2

    async def test_zone_isolation(self, cache: PostgresTigerCache) -> None:
        await cache.set_bitmap("user", "alice", "read", "file", "zone1", b"secret", 1)

        result = await cache.get_bitmap("user", "alice", "read", "file", "zone2")
        assert result is None

    async def test_health_check(self, cache: PostgresTigerCache) -> None:
        assert await cache.health_check() is True


# ---------------------------------------------------------------------------
# PostgresResourceMapCache Tests
# ---------------------------------------------------------------------------


class TestPostgresResourceMapCache:
    """Protocol-conformance tests for PostgresResourceMapCache."""

    @pytest.fixture
    def cache(self, pg_engine: Engine) -> PostgresResourceMapCache:
        return PostgresResourceMapCache(engine=pg_engine)

    async def test_cache_miss_returns_none(self, cache: PostgresResourceMapCache) -> None:
        result = await cache.get_int_id("file", "/nonexistent", "zone1")
        assert result is None

    async def test_set_and_get_int_id(self, cache: PostgresResourceMapCache) -> None:
        # set_int_id triggers INSERT ... ON CONFLICT DO NOTHING (auto-increment)
        await cache.set_int_id("file", "/test/resource_map_1.txt", "zone1", 0)

        result = await cache.get_int_id("file", "/test/resource_map_1.txt", "zone1")
        assert result is not None
        assert isinstance(result, int)
        assert result > 0

    async def test_get_int_ids_bulk(self, cache: PostgresResourceMapCache) -> None:
        # Insert a few resources
        await cache.set_int_id("file", "/test/bulk_a.txt", "zone1", 0)
        await cache.set_int_id("file", "/test/bulk_b.txt", "zone1", 0)

        resources: list[tuple[str, str, str]] = [
            ("file", "/test/bulk_a.txt", "zone1"),
            ("file", "/test/bulk_b.txt", "zone1"),
            ("file", "/test/nonexistent.txt", "zone1"),
        ]

        results = await cache.get_int_ids_bulk(resources)

        assert len(results) == 3
        assert results[("file", "/test/bulk_a.txt", "zone1")] is not None
        assert results[("file", "/test/bulk_b.txt", "zone1")] is not None
        assert results[("file", "/test/nonexistent.txt", "zone1")] is None

    async def test_set_int_ids_bulk(self, cache: PostgresResourceMapCache) -> None:
        mappings: dict[tuple[str, str, str], int] = {
            ("file", "/test/bulk_set_a.txt", "zone1"): 0,
            ("file", "/test/bulk_set_b.txt", "zone1"): 0,
        }
        await cache.set_int_ids_bulk(mappings)

        result_a = await cache.get_int_id("file", "/test/bulk_set_a.txt", "zone1")
        result_b = await cache.get_int_id("file", "/test/bulk_set_b.txt", "zone1")
        assert result_a is not None
        assert result_b is not None

    async def test_bulk_empty_input(self, cache: PostgresResourceMapCache) -> None:
        results = await cache.get_int_ids_bulk([])
        assert results == {}

        await cache.set_int_ids_bulk({})  # Should not raise

    async def test_idempotent_insert(self, cache: PostgresResourceMapCache) -> None:
        """Inserting the same resource twice should return the same int_id."""
        await cache.set_int_id("file", "/test/idempotent.txt", "zone1", 0)
        id1 = await cache.get_int_id("file", "/test/idempotent.txt", "zone1")

        # Insert again — ON CONFLICT DO NOTHING
        await cache.set_int_id("file", "/test/idempotent.txt", "zone1", 0)
        id2 = await cache.get_int_id("file", "/test/idempotent.txt", "zone1")

        assert id1 == id2


# ---------------------------------------------------------------------------
# CacheFactory Integration Tests
# ---------------------------------------------------------------------------


class TestCacheFactoryPostgresFallback:
    """Verify CacheFactory returns PostgreSQL implementations when configured."""

    async def test_factory_postgres_fallback(self, pg_engine: Engine) -> None:
        from nexus.cache.factory import CacheFactory
        from nexus.cache.settings import CacheSettings

        settings = CacheSettings(cache_backend="auto", dragonfly_url=None)
        factory = CacheFactory(settings, postgres_engine=pg_engine)
        await factory.initialize()

        assert factory.is_using_postgres is True
        assert factory.backend_name == "PostgreSQL"

        # Domain caches should be PostgreSQL-backed
        perm = factory.get_permission_cache()
        assert isinstance(perm, PostgresPermissionCache)

        tiger = factory.get_tiger_cache()
        assert isinstance(tiger, PostgresTigerCache)

        resmap = factory.get_resource_map_cache()
        assert isinstance(resmap, PostgresResourceMapCache)

        # Verify they actually work
        await perm.set("user", "alice", "read", "file", "/a", True, "zone1")
        assert await perm.get("user", "alice", "read", "file", "/a", "zone1") is True

        # Health check
        health: dict[str, Any] = await factory.health_check()
        assert health["healthy"] is True
        assert health["backend"] == "PostgreSQL"

        await factory.shutdown()

    async def test_factory_no_engine_uses_null(self) -> None:
        """Without postgres_engine or Dragonfly, factory uses NullCacheStore."""
        from nexus.cache.factory import CacheFactory
        from nexus.cache.settings import CacheSettings
        from nexus.core.cache_store import NullCacheStore

        settings = CacheSettings(cache_backend="auto", dragonfly_url=None)
        factory = CacheFactory(settings)
        await factory.initialize()

        assert factory.is_using_postgres is False
        assert isinstance(factory.cache_store, NullCacheStore)

        await factory.shutdown()
