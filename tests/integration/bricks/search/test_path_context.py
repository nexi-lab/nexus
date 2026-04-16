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
