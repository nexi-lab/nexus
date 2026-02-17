"""PostgreSQL cache backend implementations.

This module provides PostgreSQL-backed cache implementations that use
the existing rebac_check_cache, tiger_cache, and tiger_resource_map tables.

These serve as the cache backend when Dragonfly is not configured.
PostgreSQL-only (no SQLite branching) — SQLite users fall back to NullCacheStore.

Extracted from:
    - rebac_manager.py (PermissionCache)
    - tiger_cache.py (TigerCache, ResourceMapCache)

Async-safety: All sync engine I/O is wrapped with asyncio.to_thread()
to avoid blocking the event loop (#1524).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import column, delete, text
from sqlalchemy import table as sa_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

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

_PERM_UPSERT = text("""
    INSERT INTO rebac_check_cache (
        cache_id, zone_id, subject_type, subject_id, permission,
        object_type, object_id, result, computed_at, expires_at
    )
    VALUES (
        :cache_id, :zone_id, :subject_type, :subject_id, :permission,
        :object_type, :object_id, :result, :computed_at, :expires_at
    )
    ON CONFLICT (zone_id, subject_type, subject_id, permission, object_type, object_id)
    DO UPDATE SET
        cache_id = EXCLUDED.cache_id,
        result = EXCLUDED.result,
        computed_at = EXCLUDED.computed_at,
        expires_at = EXCLUDED.expires_at
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

# ===========================================================================
# PostgresPermissionCache
# ===========================================================================

class PostgresPermissionCache:
    """PostgreSQL-backed permission cache using rebac_check_cache table.

    Implements PermissionCacheProtocol via structural subtyping.

    Extracted from rebac_manager.py:3841-4575 and rebac_manager_zone_aware.py:909-992.
    All queries include zone_id for multi-zone isolation (P0 security).
    """

    def __init__(
        self,
        engine: "Engine",
        ttl: int = 300,
        denial_ttl: int = 60,
    ):
        self._engine = engine
        self._ttl = ttl
        self._denial_ttl = denial_ttl

    # --- Sync DB helpers (run via asyncio.to_thread) ---

    def _get_sync(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool | None:
        now = datetime.now(UTC)
        with self._engine.connect() as conn:
            result = conn.execute(
                _PERM_GET,
                {
                    "zone_id": zone_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "permission": permission,
                    "object_type": object_type,
                    "object_id": object_id,
                    "now": now,
                },
            )
            row = result.fetchone()
            if row:
                return bool(row.result)
            return None

    def _set_sync(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        zone_id: str,
    ) -> None:
        now = datetime.now(UTC)
        ttl = self._ttl if result else self._denial_ttl
        expires_at = now + timedelta(seconds=ttl)
        cache_id = str(uuid.uuid4())

        with self._engine.begin() as conn:
            conn.execute(
                _PERM_UPSERT,
                {
                    "cache_id": cache_id,
                    "zone_id": zone_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "permission": permission,
                    "object_type": object_type,
                    "object_id": object_id,
                    "result": int(result),
                    "computed_at": now,
                    "expires_at": expires_at,
                },
            )

    def _invalidate_subject_sync(
        self, subject_type: str, subject_id: str, zone_id: str
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                _PERM_DELETE_SUBJECT,
                {
                    "zone_id": zone_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                },
            )
            return result.rowcount

    def _invalidate_object_sync(
        self, object_type: str, object_id: str, zone_id: str
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                _PERM_DELETE_OBJECT,
                {
                    "zone_id": zone_id,
                    "object_type": object_type,
                    "object_id": object_id,
                },
            )
            return result.rowcount

    def _invalidate_subject_object_sync(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        with self._engine.begin() as conn:
            result = conn.execute(
                _PERM_DELETE_SUBJECT_OBJECT,
                {
                    "zone_id": zone_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "object_type": object_type,
                    "object_id": object_id,
                },
            )
            return result.rowcount

    def _clear_sync(self, zone_id: str | None = None) -> int:
        with self._engine.begin() as conn:
            if zone_id is not None:
                result = conn.execute(_PERM_DELETE_ZONE, {"zone_id": zone_id})
            else:
                result = conn.execute(_PERM_DELETE_ALL)
            return result.rowcount

    def _health_check_sync(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    def _get_stats_sync(self) -> dict:
        now = datetime.now(UTC)
        count = 0
        try:
            with self._engine.connect() as conn:
                result = conn.execute(_PERM_COUNT_VALID, {"now": now})
                row = result.fetchone()
                count = int(row[0]) if row else 0
        except Exception as e:
            logger.warning("Failed to get permission cache stats: %s", e)

        return {
            "backend": "postgres",
            "ttl_grants": self._ttl,
            "ttl_denials": self._denial_ttl,
            "valid_entries": count,
        }

    # --- Async public API ---

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
        return await asyncio.to_thread(
            self._get_sync,
            subject_type, subject_id, permission, object_type, object_id, zone_id,
        )

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
        await asyncio.to_thread(
            self._set_sync,
            subject_type, subject_id, permission, object_type, object_id, result, zone_id,
        )

    async def invalidate_subject(
        self,
        subject_type: str,
        subject_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for a subject."""
        return await asyncio.to_thread(
            self._invalidate_subject_sync, subject_type, subject_id, zone_id,
        )

    async def invalidate_object(
        self,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate all cached permissions for an object."""
        return await asyncio.to_thread(
            self._invalidate_object_sync, object_type, object_id, zone_id,
        )

    async def invalidate_subject_object(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> int:
        """Invalidate cached permissions for a specific subject-object pair."""
        return await asyncio.to_thread(
            self._invalidate_subject_object_sync,
            subject_type, subject_id, object_type, object_id, zone_id,
        )

    async def clear(self, zone_id: str | None = None) -> int:
        """Clear cached permissions. If zone_id given, only that zone."""
        return await asyncio.to_thread(self._clear_sync, zone_id)

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        return await asyncio.to_thread(self._health_check_sync)

    async def get_stats(self) -> dict:
        """Get cache statistics including count of valid entries."""
        return await asyncio.to_thread(self._get_stats_sync)

# ===========================================================================
# PostgresTigerCache
# ===========================================================================

class PostgresTigerCache:
    """PostgreSQL-backed Tiger cache using tiger_cache table.

    Implements TigerCacheProtocol via structural subtyping.

    Extracted from tiger_cache.py:724-1370.
    Stores pre-materialized Roaring Bitmaps for O(1) permission filtering.
    """

    def __init__(self, engine: "Engine"):
        self._engine = engine

    # --- Sync DB helpers ---

    def _get_bitmap_sync(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        with self._engine.connect() as conn:
            result = conn.execute(
                _TIGER_GET,
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "permission": permission,
                    "resource_type": resource_type,
                    "zone_id": zone_id,
                },
            )
            row = result.fetchone()
            if row:
                return (bytes(row.bitmap_data), int(row.revision))
            return None

    def _set_bitmap_sync(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        bitmap_data: bytes,
        revision: int,
    ) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _TIGER_UPSERT,
                {
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "permission": permission,
                    "resource_type": resource_type,
                    "zone_id": zone_id,
                    "bitmap_data": bitmap_data,
                    "revision": revision,
                },
            )

    def _invalidate_sync(
        self,
        subject_type: str | None,
        subject_id: str | None,
        permission: str | None,
        resource_type: str | None,
        zone_id: str | None,
    ) -> int:
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

        with self._engine.begin() as conn:
            result = conn.execute(stmt)
            return result.rowcount

    def _health_check_sync(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    # --- Async public API ---

    async def get_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
    ) -> tuple[bytes, int] | None:
        """Get Tiger bitmap for a subject. Returns (bitmap_data, revision) or None."""
        return await asyncio.to_thread(
            self._get_bitmap_sync,
            subject_type, subject_id, permission, resource_type, zone_id,
        )

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
        await asyncio.to_thread(
            self._set_bitmap_sync,
            subject_type, subject_id, permission, resource_type, zone_id,
            bitmap_data, revision,
        )

    async def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate Tiger cache entries matching criteria."""
        return await asyncio.to_thread(
            self._invalidate_sync,
            subject_type, subject_id, permission, resource_type, zone_id,
        )

    async def health_check(self) -> bool:
        """Check if cache backend is healthy."""
        return await asyncio.to_thread(self._health_check_sync)

# ===========================================================================
# PostgresResourceMapCache
# ===========================================================================

class PostgresResourceMapCache:
    """PostgreSQL-backed resource map cache using tiger_resource_map table.

    Implements ResourceMapCacheProtocol via structural subtyping.

    Extracted from tiger_cache.py:91-167.
    Maps resource UUIDs to integer IDs for Roaring Bitmap compatibility.

    Note: tiger_resource_map intentionally has no zone_id column —
    resource paths are globally unique. The zone_id parameter in the
    Protocol interface is accepted but not used in queries.
    """

    def __init__(self, engine: "Engine"):
        self._engine = engine

    # --- Sync DB helpers ---

    def _get_int_id_sync(self, resource_type: str, resource_id: str) -> int | None:
        with self._engine.connect() as conn:
            result = conn.execute(
                _RESMAP_GET,
                {"resource_type": resource_type, "resource_id": resource_id},
            )
            row = result.fetchone()
            if row:
                return int(row.resource_int_id)
            return None

    def _get_int_ids_bulk_sync(
        self, resources: list[tuple[str, str, str]]
    ) -> dict[tuple[str, str, str], int | None]:
        if not resources:
            return {}

        types = [r[0] for r in resources]
        ids = [r[1] for r in resources]

        with self._engine.connect() as conn:
            result = conn.execute(
                _RESMAP_BULK_GET,
                {"types": types, "ids": ids},
            )
            db_map: dict[tuple[str, str], int] = {}
            for row in result:
                db_map[(row.resource_type, row.resource_id)] = int(row.resource_int_id)

        return {(rt, rid, zid): db_map.get((rt, rid)) for rt, rid, zid in resources}

    def _set_int_id_sync(self, resource_type: str, resource_id: str) -> None:
        with self._engine.begin() as conn:
            conn.execute(
                _RESMAP_INSERT,
                {"resource_type": resource_type, "resource_id": resource_id},
            )

    def _set_int_ids_bulk_sync(
        self, mappings: dict[tuple[str, str, str], int]
    ) -> None:
        if not mappings:
            return
        with self._engine.begin() as conn:
            for key in mappings:
                conn.execute(
                    _RESMAP_INSERT,
                    {"resource_type": key[0], "resource_id": key[1]},
                )

    # --- Async public API ---

    async def get_int_id(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str,
    ) -> int | None:
        """Get integer ID for a resource."""
        return await asyncio.to_thread(
            self._get_int_id_sync, resource_type, resource_id,
        )

    async def get_int_ids_bulk(
        self,
        resources: list[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], int | None]:
        """Bulk get integer IDs using PostgreSQL UNNEST for efficiency."""
        return await asyncio.to_thread(self._get_int_ids_bulk_sync, resources)

    async def set_int_id(
        self,
        resource_type: str,
        resource_id: str,
        _zone_id: str,
        _int_id: int,
    ) -> None:
        """Insert a resource mapping (auto-increment assigns the int_id)."""
        await asyncio.to_thread(self._set_int_id_sync, resource_type, resource_id)

    async def set_int_ids_bulk(
        self,
        mappings: dict[tuple[str, str, str], int],
    ) -> None:
        """Bulk insert resource mappings."""
        await asyncio.to_thread(self._set_int_ids_bulk_sync, mappings)
