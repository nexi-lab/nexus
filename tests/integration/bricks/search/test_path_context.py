"""Tests for path_contexts store and cache (Issue #3773)."""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from nexus.bricks.search.path_context import (
    PathContextCache,
    PathContextStore,
)

CREATE_TABLE_SQL = """
CREATE TABLE path_contexts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id TEXT NOT NULL DEFAULT 'root',
    path_prefix TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(zone_id, path_prefix)
)
"""


@pytest_asyncio.fixture
async def async_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(CREATE_TABLE_SQL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def store(async_session_factory):
    return PathContextStore(async_session_factory=async_session_factory, db_type="sqlite")


class TestPathContextStoreUpsert:
    @pytest.mark.asyncio
    async def test_insert_then_read(self, store: PathContextStore) -> None:
        await store.upsert("root", "src/nexus/bricks/search", "Hybrid search brick")
        records = await store.list("root")
        assert len(records) == 1
        assert records[0].zone_id == "root"
        assert records[0].path_prefix == "src/nexus/bricks/search"
        assert records[0].description == "Hybrid search brick"

    @pytest.mark.asyncio
    async def test_upsert_replaces_description(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        await store.upsert("root", "src", "second")
        records = await store.list("root")
        assert len(records) == 1
        assert records[0].description == "second"

    @pytest.mark.asyncio
    async def test_delete_returns_true_when_removed(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        assert await store.delete("root", "src") is True
        assert await store.list("root") == []

    @pytest.mark.asyncio
    async def test_delete_returns_false_when_missing(self, store: PathContextStore) -> None:
        assert await store.delete("root", "nonexistent") is False

    @pytest.mark.asyncio
    async def test_zones_are_isolated(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root desc")
        await store.upsert("other", "src", "other desc")
        root = await store.list("root")
        other = await store.list("other")
        assert len(root) == 1 and root[0].description == "root desc"
        assert len(other) == 1 and other[0].description == "other desc"

    @pytest.mark.asyncio
    async def test_list_all_zones(self, store: PathContextStore) -> None:
        await store.upsert("root", "a", "a")
        await store.upsert("other", "b", "b")
        records = await store.list(zone_id=None)
        assert len(records) == 2

    @pytest.mark.asyncio
    async def test_max_updated_at_tracks_writes(self, store: PathContextStore) -> None:
        assert await store.max_updated_at("root") is None
        await store.upsert("root", "src", "first")
        stamp1 = await store.max_updated_at("root")
        assert stamp1 is not None
        await store.upsert("root", "src", "second")
        stamp2 = await store.max_updated_at("root")
        assert stamp2 is not None
        assert stamp2 >= stamp1


class TestPathContextCacheLookup:
    @pytest.mark.asyncio
    async def test_longest_prefix_wins(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "top-level src")
        await store.upsert("root", "src/nexus/bricks/search", "search brick")
        cache = PathContextCache(store=store)
        desc = await cache.lookup("root", "src/nexus/bricks/search/fusion.py")
        assert desc == "search brick"

    @pytest.mark.asyncio
    async def test_empty_prefix_matches_any(self, store: PathContextStore) -> None:
        await store.upsert("root", "", "zone root fallback")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "anything/x.py") == "zone root fallback"

    @pytest.mark.asyncio
    async def test_slash_boundary_enforced(self, store: PathContextStore) -> None:
        """'src' must NOT match 'srcfoo/x.py' — only slash-bounded match."""
        await store.upsert("root", "src", "src only")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "srcfoo/x.py") is None
        assert await cache.lookup("root", "src/x.py") == "src only"
        assert await cache.lookup("root", "src") == "src only"

    @pytest.mark.asyncio
    async def test_no_match_returns_none(self, store: PathContextStore) -> None:
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "anything/x.py") is None

    @pytest.mark.asyncio
    async def test_zone_none_coerces_to_root(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root src")
        cache = PathContextCache(store=store)
        assert await cache.lookup(None, "src/x.py") == "root src"

    @pytest.mark.asyncio
    async def test_zones_isolated_in_cache(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "root src")
        await store.upsert("other", "src", "other src")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") == "root src"
        assert await cache.lookup("other", "src/x.py") == "other src"

    @pytest.mark.asyncio
    async def test_refresh_after_write(self, store: PathContextStore) -> None:
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") is None
        await store.upsert("root", "src", "first desc")
        assert await cache.lookup("root", "src/x.py") == "first desc"
        await store.upsert("root", "src", "second desc")
        assert await cache.lookup("root", "src/x.py") == "second desc"

    @pytest.mark.asyncio
    async def test_refresh_after_delete(self, store: PathContextStore) -> None:
        await store.upsert("root", "src", "first")
        cache = PathContextCache(store=store)
        assert await cache.lookup("root", "src/x.py") == "first"
        await store.delete("root", "src")
        assert await cache.lookup("root", "src/x.py") is None


class TestPathContextCacheBatchLookup:
    @pytest.mark.asyncio
    async def test_lookup_cached_does_not_hit_db(
        self, store: PathContextStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lookup_cached is pure in-memory; it must NOT call store methods."""
        await store.upsert("root", "src", "src desc")
        cache = PathContextCache(store=store)
        await cache.refresh_if_stale("root")

        # Replace the store's max_updated_at with a trap — any call fails.
        async def trap(zone_id: str) -> None:
            raise AssertionError("lookup_cached must not invoke max_updated_at")

        monkeypatch.setattr(store, "max_updated_at", trap)

        assert cache.lookup_cached("root", "src/x.py") == "src desc"
        assert cache.lookup_cached("root", "no/match") is None
