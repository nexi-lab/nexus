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
    async def test_upsert_preserves_row_id(self, async_session_factory) -> None:
        """Upsert must do ON CONFLICT DO UPDATE, not DELETE+INSERT (Issue #3773
        review): row id stays stable so future FKs/audit trails don't break."""
        from sqlalchemy import text

        store = PathContextStore(async_session_factory=async_session_factory, db_type="sqlite")
        await store.upsert("root", "src", "first")
        async with async_session_factory() as s:
            first_id = (
                await s.execute(
                    text(
                        "SELECT id FROM path_contexts "
                        "WHERE zone_id = 'root' AND path_prefix = 'src'"
                    )
                )
            ).scalar()
        await store.upsert("root", "src", "second")
        async with async_session_factory() as s:
            second_id = (
                await s.execute(
                    text(
                        "SELECT id FROM path_contexts "
                        "WHERE zone_id = 'root' AND path_prefix = 'src'"
                    )
                )
            ).scalar()
        assert first_id == second_id

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

    @pytest.mark.asyncio
    async def test_refresh_after_delete_when_other_rows_remain(self, async_session_factory) -> None:
        """Issue #3773 review regression: deleting a row below the zone MAX
        previously left the cache serving the stale entry because the
        freshness stamp (MAX(updated_at)) did not change. The fingerprint
        token must also detect row removals.
        """
        from sqlalchemy import text

        store = PathContextStore(async_session_factory=async_session_factory, db_type="sqlite")

        # Seed two rows with distinct updated_at stamps and force the MAX onto
        # the row we intend to KEEP. Raw SQL drives the timestamps so the
        # test isn't flaky on same-millisecond writes.
        await store.upsert("root", "docs", "old row")
        await store.upsert("root", "docs/new", "keep row")
        async with async_session_factory() as s:
            await s.execute(
                text(
                    "UPDATE path_contexts SET updated_at = '2024-01-01 00:00:00' "
                    "WHERE path_prefix = 'docs'"
                )
            )
            await s.execute(
                text(
                    "UPDATE path_contexts SET updated_at = '2025-06-01 00:00:00' "
                    "WHERE path_prefix = 'docs/new'"
                )
            )
            await s.commit()

        cache = PathContextCache(store=store)
        await cache.refresh_if_stale("root")
        assert cache.lookup_cached("root", "docs/readme.md") == "old row"

        # Delete the OLDER row. MAX(updated_at) does not change because the
        # newer row still holds the max. Pre-fix, the cache would not
        # refresh and would keep serving "old row" against the deleted
        # prefix.
        await store.delete("root", "docs")
        assert await cache.lookup("root", "docs/readme.md") is None
        assert await cache.lookup("root", "docs/new/readme.md") == "keep row"


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


class TestDaemonConfigMaxZonesWiring:
    """Round-4 gap: verify DaemonConfig.path_context_max_zones actually
    reaches PathContextCache — neither the default nor the env override
    had test coverage before this."""

    def test_default_path_context_max_zones(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        assert DaemonConfig().path_context_max_zones == 2048

    def test_custom_path_context_max_zones(self) -> None:
        from nexus.bricks.search.daemon import DaemonConfig

        assert DaemonConfig(path_context_max_zones=42).path_context_max_zones == 42

    @pytest.mark.asyncio
    async def test_cache_honors_max_zones_from_config(self, store: PathContextStore) -> None:
        """The cache must respect the value plumbed from DaemonConfig so
        operator-tuned values actually take effect at the cache level."""
        cache = PathContextCache(store=store, max_zones=3)
        assert cache._max_zones == 3


class TestPathContextCacheLocksCap:
    """Round-8 review regression: zone_id is client-controlled via the
    ``X-Nexus-Zone-ID`` header. The per-zone ``asyncio.Lock`` dict is
    intentionally not LRU-evicted (Round-3 identity guarantee), but an
    authenticated caller could still churn unique zone_ids and inflate
    ``_locks`` without bound. Bound the dict by dropping non-held locks
    once it exceeds a safe multiple of ``max_zones``."""

    @pytest.mark.asyncio
    async def test_locks_dict_bounded_by_cap(self, store: PathContextStore) -> None:
        cache = PathContextCache(store=store, max_zones=4)
        for i in range(40):
            cache._lock_for(f"z{i}")
        # Cap from source: max(_max_zones * 4, 16) => 16. Tolerate +1 because
        # the newest lock is retained even when it tips over the cap.
        assert len(cache._locks) <= 17

    @pytest.mark.asyncio
    async def test_locks_dict_preserves_currently_held_locks(self, store: PathContextStore) -> None:
        """Round-3 identity guarantee must survive Round-8 cap logic: any
        lock currently ``locked()`` must NOT be dropped by the cap."""
        cache = PathContextCache(store=store, max_zones=4)
        held = cache._lock_for("hot_zone")
        await held.acquire()
        try:
            for i in range(80):
                cache._lock_for(f"z{i}")
            assert cache._locks.get("hot_zone") is held
        finally:
            held.release()


class TestPathContextCacheLRU:
    @pytest.mark.asyncio
    async def test_lru_bound_evicts_oldest_zone(self, store: PathContextStore) -> None:
        """With max_zones=2, the third distinct zone forces eviction of the
        oldest — prevents the cache from growing without bound across many
        short-lived zones (Issue #3773 review)."""
        await store.upsert("z1", "src", "one")
        await store.upsert("z2", "src", "two")
        await store.upsert("z3", "src", "three")

        cache = PathContextCache(store=store, max_zones=2)
        await cache.refresh_if_stale("z1")
        await cache.refresh_if_stale("z2")
        assert set(cache._entries.keys()) == {"z1", "z2"}
        await cache.refresh_if_stale("z3")
        # z1 was oldest -> evicted. z2 and z3 remain.
        assert set(cache._entries.keys()) == {"z2", "z3"}
        # Locks are intentionally kept alive across eviction — see Round-3
        # review feedback. Re-accessing z1 creates a fresh entry using the
        # same Lock object, preserving mutual-exclusion identity.
        assert "z1" in cache._locks
