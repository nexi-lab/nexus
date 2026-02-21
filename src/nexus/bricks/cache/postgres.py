"""PostgreSQL cache backend implementations.

This module provides PostgreSQL-backed cache implementations that use
the existing rebac_check_cache, tiger_cache, and tiger_resource_map tables.

These serve as the cache backend when Dragonfly is not configured.
PostgreSQL-only (no SQLite branching) — SQLite users fall back to NullCacheStore.

Engine modes (Issue #1524, Decision #5A):
    - AsyncEngine (preferred): fully non-blocking I/O via asyncpg
    - Engine (legacy): sync calls wrapped in asyncio.to_thread()

Extracted from:
    - rebac_manager.py (PermissionCache)
    - tiger_cache.py (TigerCache, ResourceMapCache)
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import column, delete, text
from sqlalchemy import table as sa_table

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL Queries — Permission Cache (rebac_check_cache table)
# ---------------------------------------------------------------------------

_PERM_GET = text("""
    SELECT result, expires_at
    FROM rebac_check_cache
    WHERE zone_id = :zone_id
      AND subject_type = :subject_type AND subject_id = :subject_id
      AND permission = :permission
      AND object_type = :object_type AND object_id = :object_id
      AND expires_at > :now
""")

_PERM_DELETE_EXACT = text("""
    DELETE FROM rebac_check_cache
    WHERE zone_id = :zone_id
      AND subject_type = :subject_type AND subject_id = :subject_id
      AND permission = :permission
      AND object_type = :object_type AND object_id = :object_id
""")

_PERM_INSERT = text("""
    INSERT INTO rebac_check_cache (
        cache_id, zone_id, subject_type, subject_id, permission,
        object_type, object_id, result, computed_at, expires_at
    )
    VALUES (
        :cache_id, :zone_id, :subject_type, :subject_id, :permission,
        :object_type, :object_id, :result, :computed_at, :expires_at
    )
""")

_PERM_DELETE_SUBJECT = text("""
    DELETE FROM rebac_check_cache
    WHERE zone_id = :zone_id
      AND subject_type = :subject_type AND subject_id = :subject_id
""")

_PERM_DELETE_OBJECT = text("""
    DELETE FROM rebac_check_cache
    WHERE zone_id = :zone_id
      AND object_type = :object_type AND object_id = :object_id
""")

_PERM_DELETE_SUBJECT_OBJECT = text("""
    DELETE FROM rebac_check_cache
    WHERE zone_id = :zone_id
      AND subject_type = :subject_type AND subject_id = :subject_id
      AND object_type = :object_type AND object_id = :object_id
""")

_PERM_DELETE_ZONE = text("""
    DELETE FROM rebac_check_cache
    WHERE zone_id = :zone_id
""")

_PERM_DELETE_ALL = text("""
    DELETE FROM rebac_check_cache
""")

_PERM_COUNT_VALID = text("""
    SELECT COUNT(*) as count
    FROM rebac_check_cache
    WHERE expires_at > :now
""")

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
# PostgresPermissionCache
# ===========================================================================


class PostgresPermissionCache:
    """PostgreSQL-backed permission cache using rebac_check_cache table.

    Implements PermissionCacheProtocol via structural subtyping.

    Accepts either ``AsyncEngine`` (preferred, non-blocking) or sync ``Engine``
    (legacy, wrapped in ``asyncio.to_thread()``).

    Extracted from rebac_manager.py:3841-4575 and rebac_manager_zone_aware.py:909-992.
    All queries include zone_id for multi-zone isolation (P0 security).
    """

    def __init__(
        self,
        engine: Any,
        ttl: int = 300,
        denial_ttl: int = 60,
    ):
        self._engine = engine
        self._is_async = _is_async_engine(engine)
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
        """Get cached permission result. Returns True/False/None."""
        now = datetime.now(UTC)
        params = {
            "zone_id": zone_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "object_type": object_type,
            "object_id": object_id,
            "now": now,
        }
        if self._is_async:
            async with self._engine.connect() as conn:
                result = await conn.execute(_PERM_GET, params)
                row = result.fetchone()
                if row:
                    return bool(row.result)
                return None
        else:
            return await asyncio.to_thread(self._get_sync, params)

    def _get_sync(self, params: dict[str, Any]) -> bool | None:
        with self._engine.connect() as conn:
            result = conn.execute(_PERM_GET, params)
            row = result.fetchone()
            if row:
                return bool(row.result)
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
        """Cache permission result with appropriate TTL."""
        now = datetime.now(UTC)
        ttl = self._ttl if result else self._denial_ttl
        expires_at = now + timedelta(seconds=ttl)
        cache_id = str(uuid.uuid4())

        params: dict[str, Any] = {
            "zone_id": zone_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "object_type": object_type,
            "object_id": object_id,
        }
        insert_params = {
            **params,
            "cache_id": cache_id,
            "result": int(result),
            "computed_at": now,
            "expires_at": expires_at,
        }

        if self._is_async:
            async with self._engine.begin() as conn:
                await conn.execute(_PERM_DELETE_EXACT, params)
                await conn.execute(_PERM_INSERT, insert_params)
        else:
            await asyncio.to_thread(self._set_sync, params, insert_params)

    def _set_sync(self, params: dict[str, Any], insert_params: dict[str, Any]) -> None:
        with self._engine.begin() as conn:
            conn.execute(_PERM_DELETE_EXACT, params)
            conn.execute(_PERM_INSERT, insert_params)

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for a subject."""
        params = {
            "zone_id": zone_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
        }
        if self._is_async:
            async with self._engine.begin() as conn:
                result = await conn.execute(_PERM_DELETE_SUBJECT, params)
                return int(result.rowcount)
        return await asyncio.to_thread(self._invalidate_sync, _PERM_DELETE_SUBJECT, params)

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for an object."""
        params = {
            "zone_id": zone_id,
            "object_type": object_type,
            "object_id": object_id,
        }
        if self._is_async:
            async with self._engine.begin() as conn:
                result = await conn.execute(_PERM_DELETE_OBJECT, params)
                return int(result.rowcount)
        return await asyncio.to_thread(self._invalidate_sync, _PERM_DELETE_OBJECT, params)

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate cached permissions for a specific subject-object pair."""
        params = {
            "zone_id": zone_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "object_type": object_type,
            "object_id": object_id,
        }
        if self._is_async:
            async with self._engine.begin() as conn:
                result = await conn.execute(_PERM_DELETE_SUBJECT_OBJECT, params)
                return int(result.rowcount)
        return await asyncio.to_thread(self._invalidate_sync, _PERM_DELETE_SUBJECT_OBJECT, params)

    def _invalidate_sync(self, stmt: Any, params: dict[str, Any]) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(stmt, params)
            return int(result.rowcount)

    async def clear(self, zone_id: str | None = None) -> int:
        """Clear cached permissions. If zone_id given, only that zone."""
        if self._is_async:
            async with self._engine.begin() as conn:
                if zone_id is not None:
                    result = await conn.execute(_PERM_DELETE_ZONE, {"zone_id": zone_id})
                else:
                    result = await conn.execute(_PERM_DELETE_ALL)
                return int(result.rowcount)
        return await asyncio.to_thread(self._clear_sync, zone_id)

    def _clear_sync(self, zone_id: str | None) -> int:
        with self._engine.begin() as conn:
            if zone_id is not None:
                result = conn.execute(_PERM_DELETE_ZONE, {"zone_id": zone_id})
            else:
                result = conn.execute(_PERM_DELETE_ALL)
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

    async def get_stats(self) -> dict:
        """Get cache statistics including count of valid entries."""
        now = datetime.now(UTC)
        count = 0
        try:
            if self._is_async:
                async with self._engine.connect() as conn:
                    result = await conn.execute(_PERM_COUNT_VALID, {"now": now})
                    row = result.fetchone()
                    count = int(row[0]) if row else 0
            else:
                count = await asyncio.to_thread(self._get_stats_sync, now)
        except Exception as e:
            logger.warning("Failed to get permission cache stats: %s", e)

        return {
            "backend": "postgres",
            "ttl_grants": self._ttl,
            "ttl_denials": self._denial_ttl,
            "valid_entries": count,
        }

    def _get_stats_sync(self, now: datetime) -> int:
        with self._engine.connect() as conn:
            result = conn.execute(_PERM_COUNT_VALID, {"now": now})
            row = result.fetchone()
            return int(row[0]) if row else 0


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
