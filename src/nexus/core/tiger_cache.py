"""Tiger Cache - Pre-materialized Permissions as Roaring Bitmaps

Implements pre-computed permission caches for O(1) list operations,
based on SpiceDB's Tiger Cache proposal.

Performance:
    - List operations: O(n) -> O(1)
    - 10-100x speedup for directory listings
    - Background updates don't block reads

Architecture:
    1. TigerResourceMap: Maps resource UUIDs to int64 IDs for bitmap storage
    2. TigerCache: Stores serialized Roaring Bitmaps per (subject, permission, resource_type)
    3. TigerCacheUpdater: Background worker for incremental updates via changelog

Related: Issue #682
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyroaring import BitMap as RoaringBitmap

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


@dataclass
class CacheKey:
    """Key for Tiger Cache lookup.

    Note: tenant_id is intentionally excluded from the cache key.
    Tenant isolation is enforced during permission computation, not caching.
    This allows shared resources (e.g., /skills in 'default' tenant) to be
    accessible across tenants without cache misses.

    See: Issue #979 - Tiger Cache persistence and cross-tenant optimization
    """

    subject_type: str
    subject_id: str
    permission: str
    resource_type: str

    def __hash__(self) -> int:
        return hash(
            (
                self.subject_type,
                self.subject_id,
                self.permission,
                self.resource_type,
            )
        )


class TigerResourceMap:
    """Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility.

    Maintains a bidirectional mapping between string resource IDs and
    integer IDs suitable for Roaring Bitmaps.

    Note: tenant_id is intentionally excluded from resource mapping.
    Resource paths are globally unique (e.g., /skills/system/docs is the same
    file regardless of who queries it). Tenant isolation is enforced at the
    bitmap/permission level, not the resource ID mapping.

    See: Issue #979 - Cross-tenant resource map optimization
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        self._is_postgresql = "postgresql" in str(engine.url)

        # In-memory cache for frequently accessed mappings
        # Key is (type, id) - tenant excluded for cross-tenant compatibility
        self._uuid_to_int: dict[tuple[str, str], int] = {}  # (type, id) -> int
        self._int_to_uuid: dict[int, tuple[str, str]] = {}  # int -> (type, id)
        self._lock = threading.RLock()

    def get_or_create_int_id(
        self,
        resource_type: str,
        resource_id: str,
        _tenant_id: str | None = None,  # Deprecated: kept for API compatibility, ignored
        conn: Connection | None = None,
    ) -> int:
        """Get or create an integer ID for a resource.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., UUID or path)
            tenant_id: DEPRECATED - ignored, kept for API compatibility
            conn: Optional database connection

        Returns:
            Integer ID for use in bitmaps
        """
        # Key excludes tenant - resource paths are globally unique
        key = (resource_type, resource_id)

        # Check memory cache first
        with self._lock:
            if key in self._uuid_to_int:
                return self._uuid_to_int[key]

        # Query/insert in database
        from sqlalchemy import text

        def do_get_or_create(connection: Connection) -> int:
            # Try to get existing (no tenant filter)
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :resource_type
                  AND resource_id = :resource_id
            """)
            result = connection.execute(
                query,
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                },
            )
            row = result.fetchone()
            if row:
                return int(row.resource_int_id)

            # Insert new
            if self._is_postgresql:
                insert_query = text("""
                    INSERT INTO tiger_resource_map (resource_type, resource_id, created_at)
                    VALUES (:resource_type, :resource_id, NOW())
                    ON CONFLICT (resource_type, resource_id) DO NOTHING
                    RETURNING resource_int_id
                """)
                result = connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
                # Commit so the data persists (Issue #934 fix)
                connection.commit()
                if row:
                    return int(row.resource_int_id)
                # Conflict occurred, fetch again
                result = connection.execute(
                    query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
                return int(row.resource_int_id) if row else -1
            else:
                # SQLite - use INSERT OR IGNORE then SELECT
                # Need to commit after INSERT so SELECT can see the new row
                insert_query = text("""
                    INSERT OR IGNORE INTO tiger_resource_map (resource_type, resource_id, created_at)
                    VALUES (:resource_type, :resource_id, datetime('now'))
                """)
                connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                # Commit so the SELECT can see the inserted row
                connection.commit()
                # Get the ID (either newly inserted or existing)
                result = connection.execute(
                    query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                    },
                )
                row = result.fetchone()
                return int(row.resource_int_id) if row else -1

        if conn:
            int_id = do_get_or_create(conn)
        else:
            with self._engine.connect() as new_conn:
                int_id = do_get_or_create(new_conn)

        # Cache the mapping
        with self._lock:
            self._uuid_to_int[key] = int_id
            self._int_to_uuid[int_id] = key

        return int_id

    def get_resource_id(
        self, int_id: int, conn: Connection | None = None
    ) -> tuple[str, str] | None:
        """Get resource info from integer ID.

        Args:
            int_id: Integer ID from bitmap
            conn: Optional database connection

        Returns:
            Tuple of (resource_type, resource_id) or None if not found
        """
        # Check memory cache first
        with self._lock:
            if int_id in self._int_to_uuid:
                return self._int_to_uuid[int_id]

        # Query database
        from sqlalchemy import text

        query = text("""
            SELECT resource_type, resource_id
            FROM tiger_resource_map
            WHERE resource_int_id = :int_id
        """)

        def execute(connection: Connection) -> tuple[str, str] | None:
            result = connection.execute(query, {"int_id": int_id})
            row = result.fetchone()
            if row:
                return (row.resource_type, row.resource_id)
            return None

        if conn:
            info = execute(conn)
        else:
            with self._engine.connect() as new_conn:
                info = execute(new_conn)

        # Cache if found
        if info:
            with self._lock:
                self._int_to_uuid[int_id] = info
                self._uuid_to_int[info] = int_id

        return info

    def bulk_get_int_ids(
        self,
        resources: list[tuple[str, str]],  # List of (resource_type, resource_id)
        conn: Connection,
    ) -> dict[tuple[str, str], int | None]:
        """Bulk get integer IDs for multiple resources in a single query.

        Args:
            resources: List of (resource_type, resource_id) tuples
            conn: Database connection

        Returns:
            Dict mapping resource tuples to their int IDs (None if not found)
        """
        from sqlalchemy import text

        if not resources:
            return {}

        results: dict[tuple[str, str], int | None] = {}
        to_fetch: list[tuple[str, str]] = []

        # Check memory cache first
        with self._lock:
            for resource in resources:
                if resource in self._uuid_to_int:
                    results[resource] = self._uuid_to_int[resource]
                else:
                    to_fetch.append(resource)
                    results[resource] = None

        if not to_fetch:
            return results

        # Bulk fetch from database (no tenant filter)
        if self._is_postgresql:
            # PostgreSQL: Use UNNEST for efficient bulk lookup
            query = text("""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (
                    SELECT UNNEST(:types), UNNEST(:ids)
                )
            """)
            types = [r[0] for r in to_fetch]
            ids = [r[1] for r in to_fetch]
            result = conn.execute(query, {"types": types, "ids": ids})
        else:
            # SQLite: Use VALUES clause
            if len(to_fetch) > 500:
                # Batch for large sets
                for i in range(0, len(to_fetch), 500):
                    batch = to_fetch[i : i + 500]
                    batch_results = self.bulk_get_int_ids(batch, conn)
                    results.update(batch_results)
                return results

            values = ", ".join(f"('{r[0]}', '{r[1]}')" for r in to_fetch)
            query = text(f"""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (VALUES {values})
            """)
            result = conn.execute(query)

        # Process results and update cache
        with self._lock:
            for row in result:
                key = (row.resource_type, row.resource_id)
                int_id = int(row.resource_int_id)
                results[key] = int_id
                self._uuid_to_int[key] = int_id
                self._int_to_uuid[int_id] = key

        return results

    def get_int_ids_batch(
        self,
        resources: list[tuple[str, str]],
        conn: Connection | None = None,
    ) -> dict[tuple[str, str], int]:
        """Get integer IDs for multiple resources in batch.

        Args:
            resources: List of (resource_type, resource_id) tuples
            conn: Optional database connection

        Returns:
            Dict mapping resource tuples to integer IDs
        """
        result: dict[tuple[str, str], int] = {}
        missing: list[tuple[str, str]] = []

        # Check memory cache first
        with self._lock:
            for key in resources:
                if key in self._uuid_to_int:
                    result[key] = self._uuid_to_int[key]
                else:
                    missing.append(key)

        if not missing:
            return result

        # Query database for missing (no tenant filter)
        from sqlalchemy import text

        if self._is_postgresql:
            # Use UNNEST for efficient batch lookup
            query = text("""
                SELECT resource_type, resource_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id) IN (
                    SELECT unnest(:types::text[]), unnest(:ids::text[])
                )
            """)
            types = [m[0] for m in missing]
            ids = [m[1] for m in missing]

            def execute(connection: Connection) -> None:
                db_result = connection.execute(query, {"types": types, "ids": ids})
                for row in db_result:
                    key = (row.resource_type, row.resource_id)
                    result[key] = row.resource_int_id
                    with self._lock:
                        self._uuid_to_int[key] = row.resource_int_id
                        self._int_to_uuid[row.resource_int_id] = key
        else:
            # SQLite: Use individual queries (less efficient)
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :type AND resource_id = :id
            """)

            def execute(connection: Connection) -> None:
                for key in missing:
                    db_result = connection.execute(query, {"type": key[0], "id": key[1]})
                    row = db_result.fetchone()
                    if row:
                        result[key] = row.resource_int_id
                        with self._lock:
                            self._uuid_to_int[key] = row.resource_int_id
                            self._int_to_uuid[row.resource_int_id] = key

        if conn:
            execute(conn)
        else:
            with self._engine.connect() as new_conn:
                execute(new_conn)

        return result

    def clear_cache(self) -> None:
        """Clear in-memory cache."""
        with self._lock:
            self._uuid_to_int.clear()
            self._int_to_uuid.clear()


class TigerCache:
    """Pre-materialized permission cache using Roaring Bitmaps.

    Stores which resources a subject can access with a given permission.
    Enables O(1) permission filtering for list operations.
    """

    def __init__(
        self,
        engine: Engine,
        resource_map: TigerResourceMap | None = None,
        rebac_manager: EnhancedReBACManager | None = None,
    ):
        """Initialize Tiger Cache.

        Args:
            engine: SQLAlchemy database engine
            resource_map: Resource mapping service (created if not provided)
            rebac_manager: ReBAC manager for permission computation
        """
        self._engine = engine
        self._resource_map = resource_map or TigerResourceMap(engine)
        self._rebac_manager = rebac_manager
        self._is_postgresql = "postgresql" in str(engine.url)

        # In-memory cache for hot entries
        self._cache: dict[
            CacheKey, tuple[Any, int, float]
        ] = {}  # key -> (bitmap, revision, cached_at)
        self._cache_ttl = 300  # 5 minutes
        self._cache_max_size = 100_000  # Increased from 10k per Issue #979
        self._lock = threading.RLock()

    def set_rebac_manager(self, manager: EnhancedReBACManager) -> None:
        """Set the ReBAC manager for permission computation."""
        self._rebac_manager = manager

    def get_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        conn: Connection | None = None,
    ) -> set[int]:
        """Get all resource integer IDs that subject can access.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID (kept for API compatibility)
            conn: Optional database connection

        Returns:
            Set of integer resource IDs
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    logger.debug(f"[TIGER] Memory cache hit for {key}")
                    return set(bitmap)

        # Load from database
        bitmap = self._load_from_db(key, conn)
        if bitmap is not None:
            return set(bitmap)

        # Cache miss - return empty set (cache will be populated by background worker)
        logger.debug(f"[TIGER] Cache miss for {key}")
        return set()

    def check_access(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_id: str,
        _tenant_id: str = "",  # Deprecated: kept for API compatibility, ignored
        conn: Connection | None = None,
    ) -> bool | None:
        """Check if subject has permission on resource using cached bitmap.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            resource_id: String ID of resource
            tenant_id: Tenant ID (used for resource lookup, not cache key)
            conn: Optional database connection

        Returns:
            True if allowed, False if denied, None if not in cache (fallback to rebac_check)
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Get resource int ID (no tenant - paths are globally unique)
        resource_key = (resource_type, resource_id)
        with self._lock:
            int_id = self._resource_map._uuid_to_int.get(resource_key)

        if int_id is None:
            # Resource not in map - need to create it
            int_id = self._resource_map.get_or_create_int_id(resource_type, resource_id, conn=conn)

        # Check in-memory cache
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    result = int_id in bitmap
                    logger.debug(
                        f"Tiger Cache MEMORY HIT: {subject_type}:{subject_id} -> {permission} -> {resource_type}:{resource_id} = {result}"
                    )
                    return result
                else:
                    logger.debug(f"Tiger Cache MEMORY EXPIRED: {key}")

        # Load from database
        bitmap = self._load_from_db(key, conn)
        if bitmap is not None:
            result = int_id in bitmap
            logger.debug(
                f"Tiger Cache DB HIT: {subject_type}:{subject_id} -> {permission} -> {resource_type}:{resource_id} = {result}"
            )
            return result

        # Not in cache
        logger.debug(
            f"Tiger Cache MISS: {subject_type}:{subject_id} -> {permission} -> {resource_type}:{resource_id}"
        )
        return None

    def get_bitmap_bytes(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        conn: Connection | None = None,
    ) -> bytes | None:
        """Get serialized bitmap bytes for Rust interop (Issue #896).

        Returns the raw serialized Roaring Bitmap bytes that can be passed
        to Rust's filter_paths_with_tiger_cache() for O(1) permission filtering.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID (kept for API compatibility)
            conn: Optional database connection

        Returns:
            Serialized bitmap bytes if found, None if not in cache
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    logger.debug(f"[TIGER] get_bitmap_bytes memory hit for {key}")
                    return bytes(bitmap.serialize())

        # Load from database and return raw bytes
        from sqlalchemy import text

        query = text("""
            SELECT bitmap_data, revision FROM tiger_cache
            WHERE subject_type = :subject_type
              AND subject_id = :subject_id
              AND permission = :permission
              AND resource_type = :resource_type
        """)

        params = {
            "subject_type": key.subject_type,
            "subject_id": key.subject_id,
            "permission": key.permission,
            "resource_type": key.resource_type,
        }

        def execute(connection: Connection) -> bytes | None:
            result = connection.execute(query, params)
            row = result.fetchone()
            if row:
                # Cache the deserialized bitmap in memory for future use
                bitmap = RoaringBitmap.deserialize(row.bitmap_data)
                with self._lock:
                    self._evict_if_needed()
                    self._cache[key] = (bitmap, int(row.revision), time.time())
                logger.debug(f"[TIGER] get_bitmap_bytes DB hit for {key}")
                # Return raw bytes (already serialized from DB)
                return bytes(row.bitmap_data)
            return None

        if conn:
            return execute(conn)
        else:
            with self._engine.connect() as new_conn:
                return execute(new_conn)

    def get_cache_age(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str = "",  # noqa: ARG002 - Kept for API compatibility, not used in cache key
    ) -> float | None:
        """Get cache age in seconds for a specific entry (Issue #921).

        Used by HotspotDetector to determine if hot entries need prefetching
        before TTL expiry.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Deprecated, kept for API compatibility

        Returns:
            Age in seconds if entry is in memory cache, None if not cached
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                age = time.time() - cached_at
                # Only return age if entry hasn't expired
                if age < self._cache_ttl:
                    return age
        return None

    def _load_from_db(self, key: CacheKey, conn: Connection | None = None) -> Any:
        """Load bitmap from database.

        Args:
            key: Cache key
            conn: Optional database connection

        Returns:
            Bitmap if found, None otherwise
        """
        from sqlalchemy import text

        query = text("""
            SELECT bitmap_data, revision FROM tiger_cache
            WHERE subject_type = :subject_type
              AND subject_id = :subject_id
              AND permission = :permission
              AND resource_type = :resource_type
        """)

        params = {
            "subject_type": key.subject_type,
            "subject_id": key.subject_id,
            "permission": key.permission,
            "resource_type": key.resource_type,
        }

        def execute(connection: Connection) -> Any:  # Returns Bitmap or None
            result = connection.execute(query, params)
            row = result.fetchone()
            if row:
                bitmap = RoaringBitmap.deserialize(row.bitmap_data)

                # Cache in memory
                with self._lock:
                    self._evict_if_needed()
                    self._cache[key] = (bitmap, int(row.revision), time.time())

                logger.debug(f"[TIGER] DB cache hit for {key}, {len(bitmap)} resources")
                return bitmap
            return None

        if conn:
            return execute(conn)
        else:
            with self._engine.connect() as new_conn:
                return execute(new_conn)

    def _bulk_load_from_db(self, keys: list[CacheKey], conn: Connection) -> dict[CacheKey, Any]:
        """Bulk load bitmaps from database in a single query.

        Args:
            keys: List of cache keys to load
            conn: Database connection

        Returns:
            Dict mapping cache keys to their bitmaps (missing keys not included)
        """
        from sqlalchemy import text

        if not keys:
            return {}

        results: dict[CacheKey, Any] = {}
        to_fetch: list[CacheKey] = []

        # Check memory cache first
        current_time = time.time()
        with self._lock:
            for key in keys:
                if key in self._cache:
                    bitmap, revision, cached_at = self._cache[key]
                    if current_time - cached_at < self._cache_ttl:
                        results[key] = bitmap
                    else:
                        to_fetch.append(key)
                else:
                    to_fetch.append(key)

        if not to_fetch:
            return results

        # Bulk fetch from database (tenant_id removed from cache key per Issue #979)
        is_postgresql = "postgresql" in str(self._engine.url)

        if is_postgresql:
            query = text("""
                SELECT subject_type, subject_id, permission, resource_type,
                       bitmap_data, revision
                FROM tiger_cache
                WHERE (subject_type, subject_id, permission, resource_type) IN (
                    SELECT UNNEST(:subj_types), UNNEST(:subj_ids), UNNEST(:perms),
                           UNNEST(:res_types)
                )
            """)
            params = {
                "subj_types": [k.subject_type for k in to_fetch],
                "subj_ids": [k.subject_id for k in to_fetch],
                "perms": [k.permission for k in to_fetch],
                "res_types": [k.resource_type for k in to_fetch],
            }
            db_result = conn.execute(query, params)
        else:
            # SQLite: Use VALUES clause
            if len(to_fetch) > 100:
                # Batch for large sets
                for i in range(0, len(to_fetch), 100):
                    batch = to_fetch[i : i + 100]
                    batch_results = self._bulk_load_from_db(batch, conn)
                    results.update(batch_results)
                return results

            values = ", ".join(
                f"('{k.subject_type}', '{k.subject_id}', '{k.permission}', '{k.resource_type}')"
                for k in to_fetch
            )
            query = text(f"""
                SELECT subject_type, subject_id, permission, resource_type,
                       bitmap_data, revision
                FROM tiger_cache
                WHERE (subject_type, subject_id, permission, resource_type)
                    IN (VALUES {values})
            """)
            db_result = conn.execute(query)

        # Process results and update cache
        with self._lock:
            for row in db_result:
                key = CacheKey(
                    row.subject_type,
                    row.subject_id,
                    row.permission,
                    row.resource_type,
                )
                bitmap = RoaringBitmap.deserialize(row.bitmap_data)
                results[key] = bitmap

                # Update memory cache
                self._evict_if_needed()
                self._cache[key] = (bitmap, int(row.revision), time.time())

        return results

    def check_access_bulk(
        self,
        checks: list[tuple[str, str, str, str, str, str]],
        # Each tuple: (subject_type, subject_id, permission, resource_type, resource_id, tenant_id)
    ) -> dict[tuple[str, str, str, str, str, str], bool | None]:
        """Bulk check permissions using Tiger Cache with only 2 DB queries.

        This is the optimal bulk check method that:
        1. Collects all unique resources and cache keys
        2. Bulk loads all resource int IDs in one query
        3. Bulk loads all bitmaps in one query
        4. Checks each item against in-memory bitmaps

        Args:
            checks: List of (subject_type, subject_id, permission, resource_type, resource_id, tenant_id)

        Returns:
            Dict mapping each check tuple to True (allowed), False (denied), or None (not in cache)
        """
        if not checks:
            return {}

        results: dict[tuple[str, str, str, str, str, str], bool | None] = {}

        # Step 1: Collect unique resources and cache keys
        # Note: resource key excludes tenant - paths are globally unique
        unique_resources: set[tuple[str, str]] = set()  # (res_type, res_id)
        unique_keys: set[CacheKey] = set()

        for subj_type, subj_id, perm, res_type, res_id, _tenant in checks:
            unique_resources.add((res_type, res_id))
            unique_keys.add(CacheKey(subj_type, subj_id, perm, res_type))

        with self._engine.connect() as conn:
            # Step 2: Bulk load resource int IDs (1 query)
            resource_ids = self._resource_map.bulk_get_int_ids(list(unique_resources), conn)

            # Step 3: Bulk load bitmaps (1 query)
            bitmaps = self._bulk_load_from_db(list(unique_keys), conn)

        # Step 4: Check each item against in-memory data
        for check in checks:
            subj_type, subj_id, perm, res_type, res_id, tenant = check
            key = CacheKey(subj_type, subj_id, perm, res_type)
            resource_key = (res_type, res_id)  # No tenant

            # Get bitmap for this subject/permission/resource_type
            bitmap = bitmaps.get(key)
            if bitmap is None:
                results[check] = None  # Cache miss
                continue

            # Get int ID for this resource
            int_id = resource_ids.get(resource_key)
            if int_id is None:
                results[check] = None  # Resource not mapped
                continue

            # Check if resource is in bitmap
            results[check] = int_id in bitmap

        return results

    def update_cache(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
        resource_int_ids: set[int],
        revision: int,
        conn: Connection | None = None,
    ) -> None:
        """Update the cache for a subject.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            resource_type: Type of resource
            tenant_id: Tenant ID
            resource_int_ids: Set of integer resource IDs the subject can access
            revision: Current revision for staleness detection
            conn: Optional database connection
        """
        from sqlalchemy import text

        logger.info(
            f"Tiger Cache UPDATE: {subject_type}:{subject_id} -> {permission} -> {resource_type} "
            f"(tenant={tenant_id}, {len(resource_int_ids)} resources, rev={revision}, "
            f"db={self._engine.url.database}, dialect={self._engine.dialect.name})"
        )

        # Create bitmap
        bitmap = RoaringBitmap(resource_int_ids)

        bitmap_data = bitmap.serialize()
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Upsert to database (tenant_id removed from unique constraint per Issue #979)
        # Note: tenant_id still included in INSERT for backward compatibility (NOT NULL column)
        query: Any  # TextClause or tuple[TextClause, TextClause]
        if self._is_postgresql:
            query = text("""
                INSERT INTO tiger_cache
                    (subject_type, subject_id, permission, resource_type, tenant_id, bitmap_data, revision, created_at, updated_at)
                VALUES
                    (:subject_type, :subject_id, :permission, :resource_type, :tenant_id, :bitmap_data, :revision, NOW(), NOW())
                ON CONFLICT (subject_type, subject_id, permission, resource_type)
                DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data, revision = EXCLUDED.revision, updated_at = NOW()
            """)
        else:
            # SQLite: Try UPDATE first, then INSERT if no rows affected
            update_query = text("""
                UPDATE tiger_cache
                SET bitmap_data = :bitmap_data, revision = :revision, updated_at = datetime('now')
                WHERE subject_type = :subject_type
                  AND subject_id = :subject_id
                  AND permission = :permission
                  AND resource_type = :resource_type
            """)
            insert_query = text("""
                INSERT INTO tiger_cache
                    (subject_type, subject_id, permission, resource_type, tenant_id, bitmap_data, revision, created_at, updated_at)
                VALUES
                    (:subject_type, :subject_id, :permission, :resource_type, :tenant_id, :bitmap_data, :revision, datetime('now'), datetime('now'))
            """)
            query = (update_query, insert_query)  # Tuple of queries for SQLite

        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "tenant_id": tenant_id,  # Keep for backward compatibility
            "bitmap_data": bitmap_data,
            "revision": revision,
        }

        def execute(connection: Connection) -> None:
            if isinstance(query, tuple):
                # SQLite: Try UPDATE first
                result = connection.execute(query[0], params)
                if result.rowcount == 0:
                    # No existing row, INSERT
                    connection.execute(query[1], params)
            else:
                connection.execute(query, params)

        try:
            if conn:
                execute(conn)
                logger.info(f"[TIGER] Database write (via conn) for {key}")
            else:
                with self._engine.begin() as new_conn:
                    # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                    if not self._is_postgresql:
                        new_conn.execute(text("PRAGMA busy_timeout=100"))
                    execute(new_conn)
                # Transaction committed after exiting 'with' block
                logger.info(f"[TIGER] Database write COMMITTED for {key}")
        except Exception as e:
            logger.error(f"[TIGER] Database write FAILED for {key}: {e}")
            raise

        # Update in-memory cache
        with self._lock:
            self._evict_if_needed()
            self._cache[key] = (bitmap, revision, time.time())

        logger.debug(f"[TIGER] Updated cache for {key}, {len(resource_int_ids)} resources")

    def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        tenant_id: str | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Invalidate cache entries matching the criteria.

        Args:
            subject_type: Filter by subject type (None = all)
            subject_id: Filter by subject ID (None = all)
            permission: Filter by permission (None = all)
            resource_type: Filter by resource type (None = all)
            tenant_id: Filter by tenant (None = all)
            conn: Optional database connection

        Returns:
            Number of entries invalidated
        """
        from sqlalchemy import text

        logger.info(
            f"Tiger Cache INVALIDATE: subject={subject_type}:{subject_id}, "
            f"permission={permission}, resource_type={resource_type}, tenant={tenant_id}"
        )

        # Build WHERE clause
        conditions = []
        params: dict[str, Any] = {}

        if subject_type:
            conditions.append("subject_type = :subject_type")
            params["subject_type"] = subject_type
        if subject_id:
            conditions.append("subject_id = :subject_id")
            params["subject_id"] = subject_id
        if permission:
            conditions.append("permission = :permission")
            params["permission"] = permission
        if resource_type:
            conditions.append("resource_type = :resource_type")
            params["resource_type"] = resource_type
        if tenant_id:
            conditions.append("tenant_id = :tenant_id")
            params["tenant_id"] = tenant_id

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        # Delete from database
        query = text(f"DELETE FROM tiger_cache WHERE {where_clause}")

        def execute(connection: Connection) -> int:
            result = connection.execute(query, params)
            return result.rowcount

        if conn:
            count = execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                count = execute(new_conn)

        # Clear in-memory cache entries
        with self._lock:
            keys_to_remove = []
            for key in self._cache:
                match = True
                if subject_type and key.subject_type != subject_type:
                    match = False
                if subject_id and key.subject_id != subject_id:
                    match = False
                if permission and key.permission != permission:
                    match = False
                if resource_type and key.resource_type != resource_type:
                    match = False
                # Note: tenant_id removed from CacheKey per Issue #979
                # Tenant isolation is enforced during permission computation
                if match:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                del self._cache[key]

        logger.debug(f"[TIGER] Invalidated {count} cache entries")
        return count

    def _evict_if_needed(self) -> None:
        """Evict old entries if cache is too large (must hold lock)."""
        if len(self._cache) >= self._cache_max_size:
            # Evict 10% oldest entries
            entries = sorted(self._cache.items(), key=lambda x: x[1][2])
            num_to_evict = max(1, len(entries) // 10)
            logger.info(
                f"Tiger Cache EVICT: cache full ({len(self._cache)}/{self._cache_max_size}), "
                f"evicting {num_to_evict} oldest entries"
            )
            for key, _ in entries[:num_to_evict]:
                del self._cache[key]

    def clear_memory_cache(self) -> None:
        """Clear in-memory cache."""
        with self._lock:
            self._cache.clear()

    def add_to_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        resource_int_id: int,
    ) -> bool:
        """Add a single resource to subject's permission bitmap (in-memory only).

        This method updates the in-memory cache only. For full write-through
        that persists to database, use persist_single_grant() instead.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID (kept for API compatibility)
            resource_int_id: Integer ID of the resource to add

        Returns:
            True if added successfully, False otherwise
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                # Check if already in bitmap (avoid unnecessary updates)
                if resource_int_id in bitmap:
                    return True
                # Add to bitmap
                bitmap.add(resource_int_id)
                self._cache[key] = (bitmap, revision, time.time())
                logger.debug(
                    f"[TIGER] Added resource {resource_int_id} to bitmap for {key} "
                    f"(now {len(bitmap)} resources)"
                )
                return True
            else:
                # Create new bitmap with this single resource
                bitmap = RoaringBitmap([resource_int_id])
                self._evict_if_needed()
                self._cache[key] = (bitmap, 0, time.time())
                logger.debug(
                    f"[TIGER] Created new bitmap for {key} with resource {resource_int_id}"
                )
                return True

    def persist_single_grant(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
    ) -> bool:
        """Write-through: Add a single resource grant and persist to database.

        This is the recommended method for permission grants. It:
        1. Gets/creates the resource's integer ID
        2. Loads existing bitmap from DB (or creates new)
        3. Adds the resource to the bitmap
        4. Persists the updated bitmap to database
        5. Updates in-memory cache

        Performance: ~1-5ms (single DB upsert)
        vs Queue processing: ~20-40 seconds (recomputes ALL resources)

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of the resource being granted
            tenant_id: Tenant ID (used for resource lookup, not cache key)

        Returns:
            True if persisted successfully, False on error
        """
        from sqlalchemy import text

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        try:
            # Step 1: Get or create resource int ID (separate transaction to avoid commit conflicts)
            resource_int_id = self._resource_map.get_or_create_int_id(
                resource_type, resource_id, tenant_id
            )

            with self._engine.begin() as conn:
                # Step 2: Load existing bitmap from DB (if exists)
                existing_bitmap = self._load_from_db(key, conn)

                if existing_bitmap is not None:
                    # Check if already in bitmap
                    if resource_int_id in existing_bitmap:
                        logger.debug(
                            f"[TIGER] Resource {resource_id} already in bitmap for {subject_id}"
                        )
                        return True
                    # Add to existing bitmap
                    existing_bitmap.add(resource_int_id)
                    bitmap = existing_bitmap
                    revision = 0  # Will be updated from cache if available
                    with self._lock:
                        if key in self._cache:
                            _, revision, _ = self._cache[key]
                else:
                    # Create new bitmap with this single resource
                    bitmap = RoaringBitmap([resource_int_id])
                    revision = 0

                # Step 3: Persist to database (tenant_id removed from key per Issue #979)
                # Note: tenant_id still included in INSERT for backward compatibility
                bitmap_data = bitmap.serialize()

                if self._is_postgresql:
                    upsert_query = text("""
                        INSERT INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, tenant_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                             :bitmap_data, :revision, NOW(), NOW())
                        ON CONFLICT (subject_type, subject_id, permission, resource_type)
                        DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                      revision = EXCLUDED.revision,
                                      updated_at = NOW()
                    """)
                else:
                    # SQLite: Use INSERT OR REPLACE
                    upsert_query = text("""
                        INSERT OR REPLACE INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, tenant_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                             :bitmap_data, :revision, datetime('now'), datetime('now'))
                    """)

                conn.execute(
                    upsert_query,
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "permission": permission,
                        "resource_type": resource_type,
                        "tenant_id": tenant_id,
                        "bitmap_data": bitmap_data,
                        "revision": revision,
                    },
                )
                # Commit happens automatically when exiting 'with' block

            # Step 4: Update in-memory cache
            with self._lock:
                self._evict_if_needed()
                self._cache[key] = (bitmap, revision, time.time())

            logger.info(
                f"[TIGER] Write-through: {subject_type}:{subject_id} granted {permission} "
                f"on {resource_type}:{resource_id} (int_id={resource_int_id}, "
                f"bitmap now has {len(bitmap)} resources)"
            )
            return True

        except Exception as e:
            logger.error(f"[TIGER] Write-through failed for {key}: {e}")
            return False

    def persist_single_revoke(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
    ) -> bool:
        """Write-through: Remove a single resource grant and persist to database.

        Critical for security - permission revocations must propagate immediately.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of the resource being revoked
            tenant_id: Tenant ID (used for resource lookup, not cache key)

        Returns:
            True if persisted successfully, False on error
        """
        from sqlalchemy import text

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        try:
            with self._engine.begin() as conn:
                # Step 1: Get resource int ID (don't create if doesn't exist)
                # Note: resource key excludes tenant - paths are globally unique
                resource_key = (resource_type, resource_id)
                with self._lock:
                    resource_int_id = self._resource_map._uuid_to_int.get(resource_key)

                if resource_int_id is None:
                    # Try to get from DB (no tenant filter)
                    query = text("""
                        SELECT resource_int_id FROM tiger_resource_map
                        WHERE resource_type = :resource_type
                          AND resource_id = :resource_id
                    """)
                    result = conn.execute(
                        query,
                        {
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                        },
                    )
                    row = result.fetchone()
                    if row:
                        resource_int_id = int(row.resource_int_id)
                    else:
                        # Resource not in map - nothing to revoke
                        logger.debug(
                            f"[TIGER] Revoke: Resource {resource_id} not in map, nothing to do"
                        )
                        return True

                # Step 2: Load existing bitmap from DB
                existing_bitmap = self._load_from_db(key, conn)

                if existing_bitmap is None:
                    # No bitmap exists - nothing to revoke
                    logger.debug(f"[TIGER] Revoke: No bitmap for {subject_id}, nothing to do")
                    return True

                if resource_int_id not in existing_bitmap:
                    # Resource not in bitmap - nothing to revoke
                    logger.debug(
                        f"[TIGER] Revoke: Resource {resource_id} not in bitmap for {subject_id}"
                    )
                    return True

                # Step 3: Remove from bitmap
                existing_bitmap.discard(resource_int_id)
                bitmap = existing_bitmap
                revision = 0
                with self._lock:
                    if key in self._cache:
                        _, revision, _ = self._cache[key]

                # Step 4: Persist to database (tenant_id removed from key per Issue #979)
                # Note: tenant_id still included in INSERT for backward compatibility
                bitmap_data = bitmap.serialize()

                if self._is_postgresql:
                    upsert_query = text("""
                        INSERT INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, tenant_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                             :bitmap_data, :revision, NOW(), NOW())
                        ON CONFLICT (subject_type, subject_id, permission, resource_type)
                        DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                      revision = EXCLUDED.revision,
                                      updated_at = NOW()
                    """)
                else:
                    upsert_query = text("""
                        INSERT OR REPLACE INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, tenant_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                             :bitmap_data, :revision, datetime('now'), datetime('now'))
                    """)

                conn.execute(
                    upsert_query,
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "permission": permission,
                        "resource_type": resource_type,
                        "tenant_id": tenant_id,
                        "bitmap_data": bitmap_data,
                        "revision": revision,
                    },
                )

            # Step 5: Update in-memory cache
            with self._lock:
                self._evict_if_needed()
                self._cache[key] = (bitmap, revision, time.time())

            logger.info(
                f"[TIGER] Write-through revoke: {subject_type}:{subject_id} revoked {permission} "
                f"on {resource_type}:{resource_id} (bitmap now has {len(bitmap)} resources)"
            )
            return True

        except Exception as e:
            logger.error(f"[TIGER] Write-through revoke failed for {key}: {e}")
            return False

    def remove_from_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        resource_int_id: int,
    ) -> bool:
        """Remove a resource from subject's permission bitmap (write-through).

        This method enables incremental Tiger Cache updates when permissions
        are revoked. Critical for security - revocations must propagate immediately.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID (kept for API compatibility)
            resource_int_id: Integer ID of the resource to remove

        Returns:
            True if removed successfully, False if not in cache

        Note:
            This is a write-through operation. For security, revocations
            should also invalidate L1 cache entries.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        with self._lock:
            if key in self._cache:
                bitmap, revision, _ = self._cache[key]
                # Check if in bitmap
                if resource_int_id not in bitmap:
                    return True  # Already not present
                # Remove from bitmap
                bitmap.discard(resource_int_id)
                self._cache[key] = (bitmap, revision, time.time())
                logger.debug(
                    f"[TIGER] Removed resource {resource_int_id} from bitmap for {key} "
                    f"(now {len(bitmap)} resources)"
                )
                return True
            else:
                # Not in cache, nothing to remove
                return False

    def add_to_bitmap_bulk(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        resource_int_ids: set[int],
    ) -> int:
        """Add multiple resources to subject's permission bitmap in bulk.

        More efficient than calling add_to_bitmap() repeatedly when
        adding multiple resources at once.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            resource_type: Type of resource
            tenant_id: Tenant ID (kept for API compatibility)
            resource_int_ids: Set of integer resource IDs to add

        Returns:
            Number of resources actually added (excludes already present)
        """
        if not resource_int_ids:
            return 0

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        with self._lock:
            if key in self._cache:
                bitmap, revision, _ = self._cache[key]
                original_size = len(bitmap)
                bitmap.update(resource_int_ids)
                added = len(bitmap) - original_size
                self._cache[key] = (bitmap, revision, time.time())
            else:
                bitmap = RoaringBitmap(resource_int_ids)
                self._evict_if_needed()
                self._cache[key] = (bitmap, 0, time.time())
                added = len(resource_int_ids)

            if added > 0:
                logger.debug(
                    f"[TIGER] Bulk added {added} resources to bitmap for {key} "
                    f"(now {len(bitmap)} resources)"
                )
            return added

    def persist_bitmap_bulk(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_int_ids: set[int],
        tenant_id: str = "default",
    ) -> bool:
        """Persist bitmap to database after bulk read operations (Issue #979).

        This is a write-behind method called after add_to_bitmap_bulk() to ensure
        computed permissions survive restarts. Should be called asynchronously
        to avoid blocking the read path.

        Usage:
            # In rebac_check_bulk after computing permissions:
            self._tiger_cache.add_to_bitmap_bulk(...)      # Memory (sync)
            asyncio.create_task(
                asyncio.to_thread(
                    self._tiger_cache.persist_bitmap_bulk, ...  # DB (async)
                )
            )

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            resource_type: Type of resource
            resource_int_ids: Set of integer resource IDs to persist
            tenant_id: Tenant ID (for backward compatibility, not used in cache key)

        Returns:
            True if persisted successfully, False on error
        """
        from sqlalchemy import text

        if not resource_int_ids:
            return True

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        try:
            # Get current bitmap from memory (may have more entries than resource_int_ids)
            with self._lock:
                if key in self._cache:
                    bitmap, revision, _ = self._cache[key]
                else:
                    bitmap = RoaringBitmap(resource_int_ids)
                    revision = 0

            bitmap_data = bitmap.serialize()

            # Note: tenant_id still included in INSERT for backward compatibility
            if self._is_postgresql:
                upsert_query = text("""
                    INSERT INTO tiger_cache
                        (subject_type, subject_id, permission, resource_type, tenant_id,
                         bitmap_data, revision, created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                         :bitmap_data, :revision, NOW(), NOW())
                    ON CONFLICT (subject_type, subject_id, permission, resource_type)
                    DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                  revision = EXCLUDED.revision,
                                  updated_at = NOW()
                """)
            else:
                upsert_query = text("""
                    INSERT OR REPLACE INTO tiger_cache
                        (subject_type, subject_id, permission, resource_type, tenant_id,
                         bitmap_data, revision, created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :resource_type, :tenant_id,
                         :bitmap_data, :revision, datetime('now'), datetime('now'))
                """)

            with self._engine.begin() as conn:
                conn.execute(
                    upsert_query,
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "permission": permission,
                        "resource_type": resource_type,
                        "tenant_id": tenant_id,
                        "bitmap_data": bitmap_data,
                        "revision": revision,
                    },
                )

            logger.debug(f"[TIGER] Persisted bulk bitmap for {key} ({len(bitmap)} resources)")
            return True

        except Exception as e:
            logger.error(f"[TIGER] persist_bitmap_bulk failed for {key}: {e}")
            return False

    def warm_from_db(self, limit: int = 1000) -> int:
        """Load recently used bitmaps from database into memory cache (Issue #979).

        Called during startup to warm the cache and avoid cold-start penalties.
        Uses non-blocking background loading - server can start immediately.

        Args:
            limit: Maximum number of entries to load (default 1000)

        Returns:
            Number of entries loaded

        Usage:
            # In NexusFS startup (non-blocking):
            asyncio.create_task(
                asyncio.to_thread(tiger_cache.warm_from_db, limit=500)
            )
        """
        from sqlalchemy import text

        query = text("""
            SELECT subject_type, subject_id, permission, resource_type,
                   bitmap_data, revision
            FROM tiger_cache
            ORDER BY updated_at DESC
            LIMIT :limit
        """)

        loaded = 0
        try:
            with self._engine.connect() as conn:
                result = conn.execute(query, {"limit": limit})

                with self._lock:
                    for row in result:
                        key = CacheKey(
                            row.subject_type,
                            row.subject_id,
                            row.permission,
                            row.resource_type,
                        )
                        bitmap = RoaringBitmap.deserialize(row.bitmap_data)
                        self._evict_if_needed()
                        self._cache[key] = (bitmap, int(row.revision), time.time())
                        loaded += 1

            logger.info(f"[TIGER] Warmed cache with {loaded} entries from database")
            return loaded

        except Exception as e:
            logger.error(f"[TIGER] warm_from_db failed: {e}")
            return loaded


class TigerCacheUpdater:
    """Background worker for updating Tiger Cache from changelog.

    Processes ReBAC changelog entries and updates affected cache entries
    incrementally.
    """

    def __init__(
        self,
        engine: Engine,
        tiger_cache: TigerCache,
        rebac_manager: EnhancedReBACManager | None = None,
    ):
        """Initialize the updater.

        Args:
            engine: SQLAlchemy database engine
            tiger_cache: Tiger Cache instance to update
            rebac_manager: ReBAC manager for permission computation
        """
        self._engine = engine
        self._tiger_cache = tiger_cache
        self._rebac_manager = rebac_manager
        self._is_postgresql = "postgresql" in str(engine.url)
        self._last_processed_revision = 0

    def set_rebac_manager(self, manager: EnhancedReBACManager) -> None:
        """Set the ReBAC manager for permission computation."""
        self._rebac_manager = manager
        self._tiger_cache.set_rebac_manager(manager)

    def queue_update(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
        priority: int = 100,
        conn: Connection | None = None,
    ) -> int:
        """Queue a cache update for background processing.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to recompute
            resource_type: Type of resource
            tenant_id: Tenant ID
            priority: Priority (lower = higher priority)
            conn: Optional database connection

        Returns:
            Queue entry ID
        """
        from sqlalchemy import text

        now_sql = "NOW()" if self._is_postgresql else "datetime('now')"
        query = text(f"""
            INSERT INTO tiger_cache_queue
                (subject_type, subject_id, permission, resource_type, tenant_id, priority, status, created_at)
            VALUES
                (:subject_type, :subject_id, :permission, :resource_type, :tenant_id, :priority, 'pending', {now_sql})
        """)

        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "tenant_id": tenant_id,
            "priority": priority,
        }

        def execute(connection: Connection) -> int:
            result = connection.execute(query, params)
            return result.lastrowid or 0

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)

    def reset_stuck_entries(
        self, stuck_timeout_minutes: int = 5, conn: Connection | None = None
    ) -> int:
        """Reset entries stuck in 'processing' state.

        If a worker crashes while processing, entries can get stuck in
        'processing' state. This method resets them to 'pending' so they
        can be retried.

        Args:
            stuck_timeout_minutes: Reset entries stuck longer than this
            conn: Optional database connection

        Returns:
            Number of entries reset
        """
        from sqlalchemy import text

        if self._is_postgresql:
            query = text("""
                UPDATE tiger_cache_queue
                SET status = 'pending'
                WHERE status = 'processing'
                  AND created_at < NOW() - INTERVAL ':minutes minutes'
            """)
        else:
            query = text("""
                UPDATE tiger_cache_queue
                SET status = 'pending'
                WHERE status = 'processing'
                  AND created_at < datetime('now', '-' || :minutes || ' minutes')
            """)

        def execute(connection: Connection) -> int:
            result = connection.execute(query, {"minutes": stuck_timeout_minutes})
            count = result.rowcount
            if count > 0:
                logger.info(f"[TIGER] Reset {count} stuck queue entries to pending")
            return count

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)

    def process_queue(self, batch_size: int = 100, conn: Connection | None = None) -> int:
        """Process pending queue entries.

        Args:
            batch_size: Maximum entries to process
            conn: Optional database connection

        Returns:
            Number of entries processed
        """
        import sqlite3

        from sqlalchemy import text
        from sqlalchemy.exc import OperationalError

        if self._rebac_manager is None:
            logger.warning("[TIGER] Cannot process queue - no ReBAC manager set")
            return 0

        # Reset any stuck entries before processing
        try:
            self.reset_stuck_entries(stuck_timeout_minutes=5)
        except Exception as e:
            logger.debug(f"[TIGER] Could not reset stuck entries: {e}")

        now_sql = "NOW()" if self._is_postgresql else "datetime('now')"

        # Helper to check if error is a database lock/deadlock error
        def is_lock_error(e: Exception) -> bool:
            err_str = str(e).lower()
            return (
                "database is locked" in err_str
                or "deadlock" in err_str
                or isinstance(e, sqlite3.OperationalError)
                or (isinstance(e, OperationalError) and "lock" in err_str)
            )

        # Get pending entries
        # Use FOR UPDATE SKIP LOCKED on PostgreSQL to avoid deadlocks
        if self._is_postgresql:
            select_query = text(f"""
                SELECT queue_id, subject_type, subject_id, permission, resource_type, tenant_id
                FROM tiger_cache_queue
                WHERE status = 'pending'
                ORDER BY priority, created_at
                LIMIT {batch_size}
                FOR UPDATE SKIP LOCKED
            """)
        else:
            select_query = text(f"""
                SELECT queue_id, subject_type, subject_id, permission, resource_type, tenant_id
                FROM tiger_cache_queue
                WHERE status = 'pending'
                ORDER BY priority, created_at
                LIMIT {batch_size}
            """)

        def do_process(connection: Connection) -> int:
            processed = 0
            result = connection.execute(select_query)
            entries = list(result)
            logger.info(f"[TIGER] do_process: fetched {len(entries)} entries from queue")

            for i, entry in enumerate(entries):
                logger.info(f"[TIGER] Processing entry {i + 1}/{len(entries)}: {entry.subject_id}")
                try:
                    # Mark as processing
                    connection.execute(
                        text(
                            "UPDATE tiger_cache_queue SET status = 'processing' WHERE queue_id = :qid"
                        ),
                        {"qid": entry.queue_id},
                    )

                    # Compute accessible resources
                    accessible = self._compute_accessible_resources(
                        entry.subject_type,
                        entry.subject_id,
                        entry.permission,
                        entry.resource_type,
                        entry.tenant_id,
                        connection,
                    )

                    # Get current revision
                    revision = self._get_current_revision(entry.tenant_id, connection)

                    # Update cache
                    self._tiger_cache.update_cache(
                        entry.subject_type,
                        entry.subject_id,
                        entry.permission,
                        entry.resource_type,
                        entry.tenant_id,
                        accessible,
                        revision,
                        connection,
                    )

                    # Mark as completed
                    connection.execute(
                        text(
                            f"UPDATE tiger_cache_queue SET status = 'completed', processed_at = {now_sql} WHERE queue_id = :qid"
                        ),
                        {"qid": entry.queue_id},
                    )
                    processed += 1

                except Exception as e:
                    # For database lock errors, don't try to update (it would also fail)
                    # Leave entry in 'processing' state - it will be cleaned up later
                    if is_lock_error(e):
                        logger.debug(
                            f"[TIGER] Database lock during queue processing for entry {entry.queue_id}, will retry later"
                        )
                    else:
                        logger.error(f"[TIGER] Failed to process queue entry {entry.queue_id}: {e}")
                        try:
                            connection.execute(
                                text(
                                    f"UPDATE tiger_cache_queue SET status = 'failed', error_message = :err, processed_at = {now_sql} WHERE queue_id = :qid"
                                ),
                                {"qid": entry.queue_id, "err": str(e)[:1000]},
                            )
                        except Exception as update_err:
                            # If we can't update the status, just log and continue
                            logger.debug(
                                f"[TIGER] Could not update queue entry status: {update_err}"
                            )

            return processed

        try:
            if conn:
                result = do_process(conn)
                logger.info(f"[TIGER] Queue processing complete (external conn): {result} entries")
                return result
            else:
                with self._engine.begin() as new_conn:
                    # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                    if not self._is_postgresql:
                        new_conn.execute(text("PRAGMA busy_timeout=100"))
                    result = do_process(new_conn)
                # Commit happens here when 'with' block exits
                logger.info(f"[TIGER] Queue processing COMMITTED: {result} entries processed")
                return result
        except Exception as e:
            # Handle lock errors at the top level (e.g., during SELECT)
            if is_lock_error(e):
                logger.debug(
                    f"[TIGER] Database lock during queue processing, will retry later: {e}"
                )
                return 0
            logger.error(f"[TIGER] Queue processing FAILED: {e}")
            raise

    def _compute_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
        conn: Connection,
    ) -> set[int]:
        """Compute all resources accessible by subject.

        This is the expensive operation that Tiger Cache amortizes.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            tenant_id: Tenant ID
            conn: Database connection

        Returns:
            Set of accessible resource integer IDs
        """
        from sqlalchemy import text

        if self._rebac_manager is None:
            return set()

        # Get all resources of this type in tenant
        # (In practice, you might want to limit this or paginate)
        resources_query = text("""
            SELECT resource_int_id, resource_id
            FROM tiger_resource_map
            WHERE resource_type = :resource_type
              AND tenant_id = :tenant_id
        """)

        result = conn.execute(
            resources_query,
            {"resource_type": resource_type, "tenant_id": tenant_id},
        )

        accessible: set[int] = set()
        for row in result:
            # Check permission
            has_access = self._rebac_manager.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=(resource_type, row.resource_id),
                tenant_id=tenant_id,
            )
            if has_access:
                accessible.add(row.resource_int_id)

        return accessible

    def _get_current_revision(self, tenant_id: str, conn: Connection) -> int:
        """Get current revision from changelog."""
        from sqlalchemy import text

        query = text("""
            SELECT COALESCE(MAX(change_id), 0) as revision
            FROM rebac_changelog
            WHERE tenant_id = :tenant_id
        """)
        result = conn.execute(query, {"tenant_id": tenant_id})
        row = result.fetchone()
        return int(row.revision) if row else 0

    def cleanup_completed(self, older_than_hours: int = 24, conn: Connection | None = None) -> int:
        """Clean up completed queue entries.

        Args:
            older_than_hours: Delete entries older than this
            conn: Optional database connection

        Returns:
            Number of entries deleted
        """
        from sqlalchemy import text

        if self._is_postgresql:
            query = text("""
                DELETE FROM tiger_cache_queue
                WHERE status IN ('completed', 'failed')
                  AND processed_at < NOW() - INTERVAL ':hours hours'
            """)
        else:
            query = text("""
                DELETE FROM tiger_cache_queue
                WHERE status IN ('completed', 'failed')
                  AND processed_at < datetime('now', '-' || :hours || ' hours')
            """)

        def execute(connection: Connection) -> int:
            result = connection.execute(query, {"hours": older_than_hours})
            return result.rowcount

        if conn:
            return execute(conn)
        else:
            with self._engine.begin() as new_conn:
                # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                if not self._is_postgresql:
                    new_conn.execute(text("PRAGMA busy_timeout=100"))
                return execute(new_conn)
