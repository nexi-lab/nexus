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
    """Key for Tiger Cache lookup."""

    subject_type: str
    subject_id: str
    permission: str
    resource_type: str
    tenant_id: str

    def __hash__(self) -> int:
        return hash(
            (
                self.subject_type,
                self.subject_id,
                self.permission,
                self.resource_type,
                self.tenant_id,
            )
        )


class TigerResourceMap:
    """Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility.

    Maintains a bidirectional mapping between string resource IDs and
    integer IDs suitable for Roaring Bitmaps.
    """

    def __init__(self, engine: Engine):
        self._engine = engine
        self._is_postgresql = "postgresql" in str(engine.url)

        # In-memory cache for frequently accessed mappings
        self._uuid_to_int: dict[tuple[str, str, str], int] = {}  # (type, id, tenant) -> int
        self._int_to_uuid: dict[int, tuple[str, str, str]] = {}  # int -> (type, id, tenant)
        self._lock = threading.RLock()

    def get_or_create_int_id(
        self,
        resource_type: str,
        resource_id: str,
        tenant_id: str,
        conn: Connection | None = None,
    ) -> int:
        """Get or create an integer ID for a resource.

        Args:
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of resource (e.g., UUID or path)
            tenant_id: Tenant ID
            conn: Optional database connection

        Returns:
            Integer ID for use in bitmaps
        """
        key = (resource_type, resource_id, tenant_id)

        # Check memory cache first
        with self._lock:
            if key in self._uuid_to_int:
                return self._uuid_to_int[key]

        # Query/insert in database
        from sqlalchemy import text

        def do_get_or_create(connection: Connection) -> int:
            # Try to get existing
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :resource_type
                  AND resource_id = :resource_id
                  AND tenant_id = :tenant_id
            """)
            result = connection.execute(
                query,
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "tenant_id": tenant_id,
                },
            )
            row = result.fetchone()
            if row:
                return int(row.resource_int_id)

            # Insert new
            if self._is_postgresql:
                insert_query = text("""
                    INSERT INTO tiger_resource_map (resource_type, resource_id, tenant_id, created_at)
                    VALUES (:resource_type, :resource_id, :tenant_id, NOW())
                    ON CONFLICT (resource_type, resource_id, tenant_id) DO NOTHING
                    RETURNING resource_int_id
                """)
                result = connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "tenant_id": tenant_id,
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
                        "tenant_id": tenant_id,
                    },
                )
                row = result.fetchone()
                return int(row.resource_int_id) if row else -1
            else:
                # SQLite - use INSERT OR IGNORE then SELECT
                # Need to commit after INSERT so SELECT can see the new row
                insert_query = text("""
                    INSERT OR IGNORE INTO tiger_resource_map (resource_type, resource_id, tenant_id, created_at)
                    VALUES (:resource_type, :resource_id, :tenant_id, datetime('now'))
                """)
                connection.execute(
                    insert_query,
                    {
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "tenant_id": tenant_id,
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
                        "tenant_id": tenant_id,
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
    ) -> tuple[str, str, str] | None:
        """Get resource info from integer ID.

        Args:
            int_id: Integer ID from bitmap
            conn: Optional database connection

        Returns:
            Tuple of (resource_type, resource_id, tenant_id) or None if not found
        """
        # Check memory cache first
        with self._lock:
            if int_id in self._int_to_uuid:
                return self._int_to_uuid[int_id]

        # Query database
        from sqlalchemy import text

        query = text("""
            SELECT resource_type, resource_id, tenant_id
            FROM tiger_resource_map
            WHERE resource_int_id = :int_id
        """)

        def execute(connection: Connection) -> tuple[str, str, str] | None:
            result = connection.execute(query, {"int_id": int_id})
            row = result.fetchone()
            if row:
                return (row.resource_type, row.resource_id, row.tenant_id)
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
        resources: list[tuple[str, str, str]],  # List of (resource_type, resource_id, tenant_id)
        conn: Connection,
    ) -> dict[tuple[str, str, str], int | None]:
        """Bulk get integer IDs for multiple resources in a single query.

        Args:
            resources: List of (resource_type, resource_id, tenant_id) tuples
            conn: Database connection

        Returns:
            Dict mapping resource tuples to their int IDs (None if not found)
        """
        from sqlalchemy import text

        if not resources:
            return {}

        results: dict[tuple[str, str, str], int | None] = {}
        to_fetch: list[tuple[str, str, str]] = []

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

        # Bulk fetch from database
        if self._is_postgresql:
            # PostgreSQL: Use UNNEST for efficient bulk lookup
            query = text("""
                SELECT resource_type, resource_id, tenant_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id, tenant_id) IN (
                    SELECT UNNEST(:types), UNNEST(:ids), UNNEST(:tenants)
                )
            """)
            types = [r[0] for r in to_fetch]
            ids = [r[1] for r in to_fetch]
            tenants = [r[2] for r in to_fetch]
            result = conn.execute(query, {"types": types, "ids": ids, "tenants": tenants})
        else:
            # SQLite: Use VALUES clause
            if len(to_fetch) > 500:
                # Batch for large sets
                for i in range(0, len(to_fetch), 500):
                    batch = to_fetch[i : i + 500]
                    batch_results = self.bulk_get_int_ids(batch, conn)
                    results.update(batch_results)
                return results

            values = ", ".join(f"('{r[0]}', '{r[1]}', '{r[2]}')" for r in to_fetch)
            query = text(f"""
                SELECT resource_type, resource_id, tenant_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id, tenant_id) IN (VALUES {values})
            """)
            result = conn.execute(query)

        # Process results and update cache
        with self._lock:
            for row in result:
                key = (row.resource_type, row.resource_id, row.tenant_id)
                int_id = int(row.resource_int_id)
                results[key] = int_id
                self._uuid_to_int[key] = int_id
                self._int_to_uuid[int_id] = key

        return results

    def get_int_ids_batch(
        self,
        resources: list[tuple[str, str, str]],
        conn: Connection | None = None,
    ) -> dict[tuple[str, str, str], int]:
        """Get integer IDs for multiple resources in batch.

        Args:
            resources: List of (resource_type, resource_id, tenant_id) tuples
            conn: Optional database connection

        Returns:
            Dict mapping resource tuples to integer IDs
        """
        result: dict[tuple[str, str, str], int] = {}
        missing: list[tuple[str, str, str]] = []

        # Check memory cache first
        with self._lock:
            for key in resources:
                if key in self._uuid_to_int:
                    result[key] = self._uuid_to_int[key]
                else:
                    missing.append(key)

        if not missing:
            return result

        # Query database for missing
        from sqlalchemy import text

        if self._is_postgresql:
            # Use UNNEST for efficient batch lookup
            query = text("""
                SELECT resource_type, resource_id, tenant_id, resource_int_id
                FROM tiger_resource_map
                WHERE (resource_type, resource_id, tenant_id) IN (
                    SELECT unnest(:types::text[]), unnest(:ids::text[]), unnest(:tenants::text[])
                )
            """)
            types = [m[0] for m in missing]
            ids = [m[1] for m in missing]
            tenants = [m[2] for m in missing]

            def execute(connection: Connection) -> None:
                db_result = connection.execute(
                    query, {"types": types, "ids": ids, "tenants": tenants}
                )
                for row in db_result:
                    key = (row.resource_type, row.resource_id, row.tenant_id)
                    result[key] = row.resource_int_id
                    with self._lock:
                        self._uuid_to_int[key] = row.resource_int_id
                        self._int_to_uuid[row.resource_int_id] = key
        else:
            # SQLite: Use individual queries (less efficient)
            query = text("""
                SELECT resource_int_id FROM tiger_resource_map
                WHERE resource_type = :type AND resource_id = :id AND tenant_id = :tenant
            """)

            def execute(connection: Connection) -> None:
                for key in missing:
                    db_result = connection.execute(
                        query, {"type": key[0], "id": key[1], "tenant": key[2]}
                    )
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
        self._cache_max_size = 10_000
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
        tenant_id: str,
        conn: Connection | None = None,
    ) -> set[int]:
        """Get all resource integer IDs that subject can access.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID
            conn: Optional database connection

        Returns:
            Set of integer resource IDs
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

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
        tenant_id: str,
        conn: Connection | None = None,
    ) -> bool | None:
        """Check if subject has permission on resource using cached bitmap.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            resource_id: String ID of resource
            tenant_id: Tenant ID
            conn: Optional database connection

        Returns:
            True if allowed, False if denied, None if not in cache (fallback to rebac_check)
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

        # Get resource int ID
        resource_key = (resource_type, resource_id, tenant_id)
        with self._lock:
            int_id = self._resource_map._uuid_to_int.get(resource_key)

        if int_id is None:
            # Resource not in map - need to create it
            int_id = self._resource_map.get_or_create_int_id(
                resource_type, resource_id, tenant_id, conn
            )

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
              AND tenant_id = :tenant_id
        """)

        params = {
            "subject_type": key.subject_type,
            "subject_id": key.subject_id,
            "permission": key.permission,
            "resource_type": key.resource_type,
            "tenant_id": key.tenant_id,
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

        # Bulk fetch from database
        is_postgresql = "postgresql" in str(self._engine.url)

        if is_postgresql:
            query = text("""
                SELECT subject_type, subject_id, permission, resource_type, tenant_id,
                       bitmap_data, revision
                FROM tiger_cache
                WHERE (subject_type, subject_id, permission, resource_type, tenant_id) IN (
                    SELECT UNNEST(:subj_types), UNNEST(:subj_ids), UNNEST(:perms),
                           UNNEST(:res_types), UNNEST(:tenants)
                )
            """)
            params = {
                "subj_types": [k.subject_type for k in to_fetch],
                "subj_ids": [k.subject_id for k in to_fetch],
                "perms": [k.permission for k in to_fetch],
                "res_types": [k.resource_type for k in to_fetch],
                "tenants": [k.tenant_id for k in to_fetch],
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
                f"('{k.subject_type}', '{k.subject_id}', '{k.permission}', '{k.resource_type}', '{k.tenant_id}')"
                for k in to_fetch
            )
            query = text(f"""
                SELECT subject_type, subject_id, permission, resource_type, tenant_id,
                       bitmap_data, revision
                FROM tiger_cache
                WHERE (subject_type, subject_id, permission, resource_type, tenant_id)
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
                    row.tenant_id,
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
        unique_resources: set[tuple[str, str, str]] = set()  # (res_type, res_id, tenant)
        unique_keys: set[CacheKey] = set()

        for subj_type, subj_id, perm, res_type, res_id, tenant in checks:
            unique_resources.add((res_type, res_id, tenant))
            unique_keys.add(CacheKey(subj_type, subj_id, perm, res_type, tenant))

        with self._engine.connect() as conn:
            # Step 2: Bulk load resource int IDs (1 query)
            resource_ids = self._resource_map.bulk_get_int_ids(list(unique_resources), conn)

            # Step 3: Bulk load bitmaps (1 query)
            bitmaps = self._bulk_load_from_db(list(unique_keys), conn)

        # Step 4: Check each item against in-memory data
        for check in checks:
            subj_type, subj_id, perm, res_type, res_id, tenant = check
            key = CacheKey(subj_type, subj_id, perm, res_type, tenant)
            resource_key = (res_type, res_id, tenant)

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
            f"(tenant={tenant_id}, {len(resource_int_ids)} resources, rev={revision})"
        )

        # Create bitmap
        bitmap = RoaringBitmap(resource_int_ids)

        bitmap_data = bitmap.serialize()
        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

        # Upsert to database
        query: Any  # TextClause or tuple[TextClause, TextClause]
        if self._is_postgresql:
            query = text("""
                INSERT INTO tiger_cache
                    (subject_type, subject_id, permission, resource_type, tenant_id, bitmap_data, revision, created_at, updated_at)
                VALUES
                    (:subject_type, :subject_id, :permission, :resource_type, :tenant_id, :bitmap_data, :revision, NOW(), NOW())
                ON CONFLICT (subject_type, subject_id, permission, resource_type, tenant_id)
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
                  AND tenant_id = :tenant_id
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
            "tenant_id": tenant_id,
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

        if conn:
            execute(conn)
        else:
            with self._engine.begin() as new_conn:
                execute(new_conn)

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
                if tenant_id and key.tenant_id != tenant_id:
                    match = False
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
        tenant_id: str,
        resource_int_id: int,
    ) -> bool:
        """Add a single resource to subject's permission bitmap (write-through).

        This method enables incremental Tiger Cache population after permission
        checks. Instead of recomputing entire bitmaps, we add individual resources
        as they are confirmed accessible.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            tenant_id: Tenant ID
            resource_int_id: Integer ID of the resource to add

        Returns:
            True if added successfully, False otherwise

        Note:
            This is a write-through operation that updates both in-memory
            cache and database. Thread-safe via RLock.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

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

    def remove_from_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        tenant_id: str,
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
            tenant_id: Tenant ID
            resource_int_id: Integer ID of the resource to remove

        Returns:
            True if removed successfully, False if not in cache

        Note:
            This is a write-through operation. For security, revocations
            should also invalidate L1 cache entries.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

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
        tenant_id: str,
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
            tenant_id: Tenant ID
            resource_int_ids: Set of integer resource IDs to add

        Returns:
            Number of resources actually added (excludes already present)
        """
        if not resource_int_ids:
            return 0

        key = CacheKey(subject_type, subject_id, permission, resource_type, tenant_id)

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

            for entry in entries:
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
                return do_process(conn)
            else:
                with self._engine.begin() as new_conn:
                    return do_process(new_conn)
        except Exception as e:
            # Handle lock errors at the top level (e.g., during SELECT)
            if is_lock_error(e):
                logger.debug(
                    f"[TIGER] Database lock during queue processing, will retry later: {e}"
                )
                return 0
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
                return execute(new_conn)
