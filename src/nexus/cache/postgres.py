"""PostgreSQL cache backend implementations.

This module provides PostgreSQL-backed cache implementations that use
the existing tiger_cache and tiger_resource_map tables.

These serve as the cache backend when Dragonfly is not configured.
PostgreSQL-only (no SQLite branching) — SQLite users fall back to NullCacheStore.

Engine modes (Issue #1524, Decision #5A):
    - AsyncEngine (preferred): fully non-blocking I/O via asyncpg
    - Engine (legacy): sync calls wrapped in asyncio.to_thread()

Note: The rebac_check_cache table (L2 permission cache) has been removed.
PostgresPermissionCache is now a no-op stub that always returns cache miss.
The L2 SQL cache is replaced by CacheStoreABC (task #234).

Extracted from:
    - rebac_manager.py (PermissionCache) — now a no-op
    - tiger_cache.py (TigerCache, ResourceMapCache)
"""

import asyncio
import logging
from typing import Any

from sqlalchemy import column, delete, text
from sqlalchemy import table as sa_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL Queries — Tiger Cache (tiger_cache table)
# ---------------------------------------------------------------------------

_TIGER_GET = text("""
    SELECT bitmap_data, revision FROM tiger_cache
    WHERE subject_type = :subject_type
      AND subject_id = :subject_id
      AND permission = :permission
      AND resource_type = :resource_type
      AND zone_id = :zone_id
""")

_TIGER_UPSERT = text("""
    INSERT INTO tiger_cache
        (subject_type, subject_id, permission, resource_type, zone_id,
         bitmap_data, revision, created_at, updated_at)
    VALUES
        (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
         :bitmap_data, :revision, NOW(), NOW())
    ON CONFLICT (subject_type, subject_id, permission, resource_type, zone_id)
    DO UPDATE SET
        bitmap_data = EXCLUDED.bitmap_data,
        revision = EXCLUDED.revision,
        updated_at = NOW()
""")

# ---------------------------------------------------------------------------
# SQL Queries — Resource Map (tiger_resource_map table)
# ---------------------------------------------------------------------------

_RESMAP_GET = text("""
    SELECT resource_int_id FROM tiger_resource_map
    WHERE resource_type = :resource_type
      AND resource_id = :resource_id
""")

_RESMAP_INSERT = text("""
    INSERT INTO tiger_resource_map (resource_type, resource_id, created_at)
    VALUES (:resource_type, :resource_id, NOW())
    ON CONFLICT (resource_type, resource_id) DO NOTHING
    RETURNING resource_int_id
""")

_RESMAP_BULK_GET = text("""
    SELECT resource_type, resource_id, resource_int_id
    FROM tiger_resource_map
    WHERE (resource_type, resource_id) IN (
        SELECT UNNEST(CAST(:types AS text[])), UNNEST(CAST(:ids AS text[]))
    )
""")

# ---------------------------------------------------------------------------
# Engine detection helper
# ---------------------------------------------------------------------------


def _is_async_engine(engine: Any) -> bool:
    """Check if engine is an AsyncEngine (without importing at module level)."""
    try:
        from sqlalchemy.ext.asyncio import AsyncEngine

        return isinstance(engine, AsyncEngine)
    except ImportError:
        return False


# ===========================================================================
# PostgresPermissionCache (no-op stub)
# ===========================================================================


class PostgresPermissionCache:
    """No-op permission cache stub.

    The rebac_check_cache table (L2 SQL permission cache) has been removed
    as part of the CacheStoreABC migration (Issue #186, task #234).
    The ORM model ReBACCheckCacheModel no longer exists, so the table is not
    created. This stub preserves the PermissionCacheProtocol interface so
    existing callers (CacheFactory, health_check) continue to work without
    code changes. All operations are safe no-ops (cache miss / 0 rows).
    """

    def __init__(
        self,
        engine: Any,
        ttl: int = 300,
        denial_ttl: int = 60,
    ):
        self._engine = engine
        self._ttl = ttl
        self._denial_ttl = denial_ttl

    async def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool | None:
        """Always returns None (cache miss) — L2 SQL table removed."""
        return None

    async def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        zone_id: str,
    ) -> None:
        """No-op — L2 SQL table removed."""

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> int:
        """No-op — returns 0 rows affected."""
        return 0

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """No-op — returns 0 rows affected."""
        return 0

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """No-op — returns 0 rows affected."""
        return 0

    async def clear(self, zone_id: str | None = None) -> int:
        """No-op — returns 0 rows affected."""
        return 0

    async def health_check(self) -> bool:
        """Check if the underlying engine is healthy (SELECT 1)."""
        try:
            if _is_async_engine(self._engine):
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return True
            return await asyncio.to_thread(self._health_check_sync)
        except Exception:
            return False

    def _health_check_sync(self) -> bool:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    async def get_stats(self) -> dict:
        """Return empty stats — L2 SQL table removed."""
        return {
            "backend": "postgres (no-op, L2 table removed)",
            "ttl_grants": self._ttl,
            "ttl_denials": self._denial_ttl,
            "valid_entries": 0,
        }


# ===========================================================================
# PostgresTigerCache
# ===========================================================================


class PostgresTigerCache:
    """PostgreSQL-backed Tiger cache using tiger_cache table.

    Implements TigerCacheProtocol via structural subtyping.

    Accepts either ``AsyncEngine`` (preferred) or sync ``Engine`` (legacy).

    Extracted from tiger_cache.py:724-1370.
    Stores pre-materialized Roaring Bitmaps for O(1) permission filtering.
    """

    def __init__(self, engine: Any):
        self._engine = engine
        self._is_async = _is_async_engine(engine)

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        """Get Tiger bitmap for a subject. Returns (bitmap_data, revision) or None."""
        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "zone_id": zone_id,
        }
        if self._is_async:
            async with self._engine.connect() as conn:
                result = await conn.execute(_TIGER_GET, params)
                row = result.fetchone()
                if row:
                    return (bytes(row.bitmap_data), int(row.revision))
                return None
        return await asyncio.to_thread(self._get_bitmap_sync, params)

    def _get_bitmap_sync(self, params: dict[str, Any]) -> tuple[bytes, int] | None:
        with self._engine.connect() as conn:
            result = conn.execute(_TIGER_GET, params)
            row = result.fetchone()
            if row:
                return (bytes(row.bitmap_data), int(row.revision))
            return None

    async def set_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        bitmap_data: bytes,
        revision: int,
    ) -> None:
        """Store Tiger bitmap using PostgreSQL UPSERT."""
        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "zone_id": zone_id,
            "bitmap_data": bitmap_data,
            "revision": revision,
        }
        if self._is_async:
            async with self._engine.begin() as conn:
                await conn.execute(_TIGER_UPSERT, params)
        else:
            await asyncio.to_thread(self._set_bitmap_sync, params)

    def _set_bitmap_sync(self, params: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(_TIGER_UPSERT, params)

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger cache entries matching criteria.

        Builds a dynamic WHERE clause from non-None parameters.
        If all are None, deletes everything.
        """
        tbl = sa_table("tiger_cache")
        stmt = delete(tbl)

        if subject_type is not None:
            stmt = stmt.where(column("subject_type") == subject_type)
        if subject_id is not None:
            stmt = stmt.where(column("subject_id") == subject_id)
        if permission is not None:
            stmt = stmt.where(column("permission") == permission)
        if resource_type is not None:
            stmt = stmt.where(column("resource_type") == resource_type)
        if zone_id is not None:
            stmt = stmt.where(column("zone_id") == zone_id)

        if self._is_async:
            async with self._engine.begin() as conn:
                result = await conn.execute(stmt)
                return int(result.rowcount)
        return await asyncio.to_thread(self._invalidate_sync, stmt)

    def _invalidate_sync(self, stmt: Any) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(stmt)
            return int(result.rowcount)

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        try:
            if self._is_async:
                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                return True
            return await asyncio.to_thread(self._health_check_sync)
        except Exception:
            return False

    def _health_check_sync(self) -> bool:
        with self._engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True


# ===========================================================================
# PostgresResourceMapCache
# ===========================================================================


class PostgresResourceMapCache:
    """PostgreSQL-backed resource map cache using tiger_resource_map table.

    Implements ResourceMapCacheProtocol via structural subtyping.

    Accepts either ``AsyncEngine`` (preferred) or sync ``Engine`` (legacy).

    Extracted from tiger_cache.py:91-167.
    Maps resource UUIDs to integer IDs for Roaring Bitmap compatibility.

    Note: tiger_resource_map intentionally has no zone_id column —
    resource paths are globally unique. The zone_id parameter in the
    Protocol interface is accepted but not used in queries.
    """

    def __init__(self, engine: Any):
        self._engine = engine
        self._is_async = _is_async_engine(engine)

    async def get_int_id(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str,
    ) -> int | None:
        """Get integer ID for a resource."""
        params = {
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        if self._is_async:
            async with self._engine.connect() as conn:
                result = await conn.execute(_RESMAP_GET, params)
                row = result.fetchone()
                if row:
                    return int(row.resource_int_id)
                return None
        return await asyncio.to_thread(self._get_int_id_sync, params)

    def _get_int_id_sync(self, params: dict[str, Any]) -> int | None:
        with self._engine.connect() as conn:
            result = conn.execute(_RESMAP_GET, params)
            row = result.fetchone()
            if row:
                return int(row.resource_int_id)
            return None

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], int | None]:
        """Bulk get integer IDs using PostgreSQL UNNEST for efficiency."""
        if not resources:
            return {}

        types = [r[0] for r in resources]
        ids = [r[1] for r in resources]
        params = {"types": types, "ids": ids}

        if self._is_async:
            async with self._engine.connect() as conn:
                result = await conn.execute(_RESMAP_BULK_GET, params)
                db_map: dict[tuple[str, str], int] = {}
                for row in result:
                    db_map[(row.resource_type, row.resource_id)] = int(row.resource_int_id)
        else:
            db_map = await asyncio.to_thread(self._get_int_ids_bulk_sync, params)

        return {(rt, rid, zid): db_map.get((rt, rid)) for rt, rid, zid in resources}

    def _get_int_ids_bulk_sync(self, params: dict[str, Any]) -> dict[tuple[str, str], int]:
        with self._engine.connect() as conn:
            result = conn.execute(_RESMAP_BULK_GET, params)
            db_map: dict[tuple[str, str], int] = {}
            for row in result:
                db_map[(row.resource_type, row.resource_id)] = int(row.resource_int_id)
            return db_map

    async def set_int_id(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str,
        _int_id: int,
    ) -> None:
        """Insert a resource mapping (auto-increment assigns the int_id).

        Uses INSERT ... ON CONFLICT DO NOTHING so existing mappings are preserved.
        """
        params = {
            "resource_type": resource_type,
            "resource_id": resource_id,
        }
        if self._is_async:
            async with self._engine.begin() as conn:
                await conn.execute(_RESMAP_INSERT, params)
        else:
            await asyncio.to_thread(self._set_int_id_sync, params)

    def _set_int_id_sync(self, params: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(_RESMAP_INSERT, params)

    async def set_int_ids_bulk(
        self,
        mappings: dict[tuple[str, str, str], int],
    ) -> None:
        """Bulk insert resource mappings."""
        if not mappings:
            return

        if self._is_async:
            async with self._engine.begin() as conn:
                for key in mappings:
                    await conn.execute(
                        _RESMAP_INSERT,
                        {"resource_type": key[0], "resource_id": key[1]},
                    )
        else:
            await asyncio.to_thread(self._set_int_ids_bulk_sync, mappings)

    def _set_int_ids_bulk_sync(self, mappings: dict[tuple[str, str, str], int]) -> None:
        with self._engine.begin() as conn:
            for key in mappings:
                conn.execute(
                    _RESMAP_INSERT,
                    {"resource_type": key[0], "resource_id": key[1]},
                )
