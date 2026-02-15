"""Tiger Cache - Pre-materialized permission bitmaps.

Stores which resources a subject can access with a given permission
using Roaring Bitmaps for O(1) permission filtering.

Architecture (with Dragonfly integration):
    L1: In-memory cache (self._cache) - fastest, per-instance
    L2: Dragonfly cache (optional) - shared across instances
    L3: PostgreSQL (tiger_cache table) - source of truth

Read path: L1 -> L2 (if available) -> L3
Write path: L3 first (source of truth) -> L2 (if available) -> L1

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

    from nexus.cache.dragonfly import DragonflyTigerCache
    from nexus.services.permissions.cache.tiger.resource_map import TigerResourceMap
    from nexus.services.permissions.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


@dataclass
class CacheKey:
    """Key for Tiger Cache lookup.

    Note: zone_id is intentionally excluded from the cache key.
    Zone isolation is enforced during permission computation, not caching.
    This allows shared resources (e.g., /skills in 'default' zone) to be
    accessible across zones without cache misses.

    See: Issue #979 - Tiger Cache persistence and cross-zone optimization
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


class TigerCache:
    """Pre-materialized permission cache using Roaring Bitmaps.

    Stores which resources a subject can access with a given permission.
    Enables O(1) permission filtering for list operations.

    Architecture (with Dragonfly integration):
        L1: In-memory cache (self._cache) - fastest, per-instance
        L2: Dragonfly cache (optional) - shared across instances
        L3: PostgreSQL (tiger_cache table) - source of truth

    Read path: L1 -> L2 (if available) -> L3
    Write path: L3 first (source of truth) -> L2 (if available) -> L1
    """

    def __init__(
        self,
        engine: Engine,
        resource_map: TigerResourceMap | None = None,
        rebac_manager: EnhancedReBACManager | None = None,
        dragonfly_cache: DragonflyTigerCache | None = None,
    ):
        """Initialize Tiger Cache.

        Args:
            engine: SQLAlchemy database engine
            resource_map: Resource mapping service (created if not provided)
            rebac_manager: ReBAC manager for permission computation
            dragonfly_cache: Optional Dragonfly cache for L2 distributed caching
        """
        from nexus.services.permissions.cache.tiger.resource_map import TigerResourceMap as _TRM

        self._engine = engine
        self._resource_map = resource_map or _TRM(engine)
        self._rebac_manager = rebac_manager
        self._is_postgresql = "postgresql" in str(engine.url)

        # L2: Dragonfly distributed cache (optional)
        self._dragonfly: DragonflyTigerCache | None = dragonfly_cache
        self._dragonfly_url: str | None = None  # Cached URL for sync Redis client

        # L1: In-memory cache for hot entries
        self._cache: dict[
            CacheKey, tuple[Any, int, float]
        ] = {}  # key -> (bitmap, revision, cached_at)
        self._cache_ttl = 300  # 5 minutes (same for L1 and L2 for consistency)
        self._cache_max_size = 100_000  # Increased from 10k per Issue #979
        self._lock = threading.RLock()

        # Persistent thread pool for L2 operations (avoid per-operation creation)
        self._l2_executor: Any | None = None

    def set_dragonfly_cache(self, dragonfly_cache: DragonflyTigerCache | None) -> None:
        """Set or update the Dragonfly cache backend.

        This allows late binding of the Dragonfly cache after initialization,
        useful when the cache factory initializes after TigerCache.

        Args:
            dragonfly_cache: DragonflyTigerCache instance or None to disable
        """
        self._dragonfly = dragonfly_cache
        if dragonfly_cache:
            # Cache URL for sync Redis operations
            self._dragonfly_url = getattr(dragonfly_cache._client, "_url", None)
            # Create persistent thread pool (max 4 workers for L2 ops)
            import concurrent.futures

            if self._l2_executor is None:
                self._l2_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="tiger-l2"
                )
            logger.info("[TIGER] Dragonfly L2 cache enabled")
        else:
            self._dragonfly_url = None
            # Shutdown executor if exists
            if self._l2_executor:
                self._l2_executor.shutdown(wait=False)
                self._l2_executor = None
            logger.info("[TIGER] Dragonfly L2 cache disabled")

    def _run_dragonfly_op(
        self,
        operation: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        bitmap_data: bytes | None = None,
        revision: int = 0,
    ) -> Any:
        """Run a Dragonfly operation using sync Redis client.

        Uses a persistent thread pool and sync Redis client to avoid
        event loop conflicts with FastAPI's async context.

        Key format: tiger:{subject_type}:{subject_id}:{permission}:{resource_type}
        Note: zone_id excluded per Issue #979 for cross-zone resource sharing.

        Args:
            operation: One of "get", "set", "invalidate"
            subject_type: Subject type for cache key
            subject_id: Subject ID for cache key
            permission: Permission for cache key
            resource_type: Resource type for cache key
            bitmap_data: Bitmap data for set operations
            revision: Revision for set operations

        Returns:
            Result from the operation, or None on error
        """
        dragonfly_url = self._dragonfly_url
        if not self._dragonfly or not dragonfly_url or not self._l2_executor:
            return None

        import concurrent.futures

        # Use L1 TTL for L2 consistency (Issue #1106: TTL sync)
        ttl = self._cache_ttl

        def run_sync_redis() -> Any:
            """Execute Dragonfly operation using sync Redis client."""
            import redis

            # Thread-local connection with connection pooling
            client = redis.from_url(
                dragonfly_url,
                decode_responses=False,
                socket_timeout=3.0,
                socket_connect_timeout=2.0,
            )
            try:
                # Key format: exclude zone_id per Issue #979
                key = f"tiger:{subject_type}:{subject_id}:{permission}:{resource_type}"

                if operation == "get":
                    result = client.hgetall(key)
                    if not result:
                        return None
                    data = result.get(b"data")
                    rev = result.get(b"revision")
                    if data is None or rev is None:
                        return None
                    return (data, int(rev))

                elif operation == "set":
                    assert bitmap_data is not None, "bitmap_data required for set"
                    pipe = client.pipeline()
                    pipe.hset(key, mapping={"data": bitmap_data, "revision": str(revision)})
                    pipe.expire(key, ttl)
                    pipe.execute()
                    return True

                elif operation == "invalidate":
                    # Pattern-based invalidation with proper wildcards
                    # Build pattern: use * for empty/None fields
                    parts = [
                        "tiger",
                        subject_type if subject_type else "*",
                        subject_id if subject_id else "*",
                        permission if permission else "*",
                        resource_type if resource_type else "*",
                    ]
                    pattern = ":".join(parts)
                    count = 0
                    # Use SCAN for safe iteration
                    cursor = 0
                    while True:
                        cursor, keys = client.scan(cursor=cursor, match=pattern, count=100)
                        if keys:
                            client.delete(*keys)
                            count += len(keys)
                        if cursor == 0:
                            break
                    return count

                return None
            finally:
                client.close()

        try:
            future = self._l2_executor.submit(run_sync_redis)
            return future.result(timeout=5.0)
        except concurrent.futures.TimeoutError:
            logger.warning("[TIGER] L2 Dragonfly operation timed out")
            return None
        except Exception as e:
            logger.warning(f"[TIGER] L2 Dragonfly error: {e}")
            return None

    def set_rebac_manager(self, manager: EnhancedReBACManager) -> None:
        """Set the ReBAC manager for permission computation."""
        self._rebac_manager = manager

    def get_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
        conn: Connection | None = None,
    ) -> set[int]:
        """Get all resource integer IDs that subject can access.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            zone_id: Zone ID (kept for API compatibility)
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
        _zone_id: str = "",  # Deprecated: kept for API compatibility, ignored
        conn: Connection | None = None,
    ) -> bool | None:
        """Check if subject has permission on resource using cached bitmap.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            resource_id: String ID of resource
            _zone_id: Zone ID (used for resource lookup, not cache key)
            conn: Optional database connection

        Returns:
            True if allowed, False if denied, None if not in cache (fallback to rebac_check)
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Get resource int ID (no zone - paths are globally unique)
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
        zone_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
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
            zone_id: Zone ID (kept for API compatibility)
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
        zone_id: str = "",  # noqa: ARG002 - Kept for API compatibility, not used in cache key
    ) -> float | None:
        """Get cache age in seconds for a specific entry (Issue #921).

        Used by HotspotDetector to determine if hot entries need prefetching
        before TTL expiry.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            zone_id: Deprecated, kept for API compatibility

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

    def get_accessible_int_ids(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
    ) -> set[int] | None:
        """Get all accessible resource integer IDs from bitmap (Polars-style predicate pushdown).

        This method enables predicate pushdown optimization by returning all resource IDs
        that a subject can access, allowing the database query to filter at the SQL level
        rather than post-filtering in Python.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")

        Returns:
            Set of integer IDs the subject can access, or None if no bitmap cached.
            Returns empty set if bitmap exists but has no entries.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    logger.debug(f"[TIGER-PUSHDOWN] Memory hit for {key}, {len(bitmap)} entries")
                    return set(bitmap)  # RoaringBitmap is iterable

        # Load from database
        bitmap = self._load_from_db(key)
        if bitmap is not None:
            logger.debug(f"[TIGER-PUSHDOWN] DB hit for {key}, {len(bitmap)} entries")
            return set(bitmap)

        logger.debug(f"[TIGER-PUSHDOWN] No bitmap found for {key}")
        return None

    def get_accessible_paths(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
    ) -> set[str] | None:
        """Get all accessible resource paths from bitmap (for SQL WHERE clause).

        Converts integer IDs from the bitmap back to string paths using the resource map.
        This is used for predicate pushdown to filter at the database level.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")

        Returns:
            Set of paths the subject can access, or None if no bitmap cached.
        """
        int_ids = self.get_accessible_int_ids(subject_type, subject_id, permission, resource_type)
        if int_ids is None:
            return None

        # Convert int IDs back to paths using resource map
        paths: set[str] = set()
        with self._resource_map._lock:
            for int_id in int_ids:
                key = self._resource_map._int_to_uuid.get(int_id)
                if key and key[0] == resource_type:
                    paths.add(key[1])  # key is (type, path)

        logger.debug(f"[TIGER-PUSHDOWN] Converted {len(int_ids)} int IDs to {len(paths)} paths")
        return paths

    def _load_from_db(
        self, key: CacheKey, conn: Connection | None = None, skip_l2: bool = False
    ) -> Any:
        """Load bitmap from L2 (Dragonfly) or L3 (PostgreSQL).

        Read path: L2 (Dragonfly) -> L3 (PostgreSQL)
        On L3 hit, also populates L2 for future requests.

        Args:
            key: Cache key
            conn: Optional database connection
            skip_l2: If True, skip L2 cache and read directly from L3 (database).
                     Used by write-through operations to ensure reading latest committed state.

        Returns:
            Bitmap if found, None otherwise
        """
        # L2: Try Dragonfly first (if available and not skipped)
        if self._dragonfly and not skip_l2:
            result = self._run_dragonfly_op(
                operation="get",
                subject_type=key.subject_type,
                subject_id=key.subject_id,
                permission=key.permission,
                resource_type=key.resource_type,
            )
            if result:
                bitmap_data, revision = result
                bitmap = RoaringBitmap.deserialize(bitmap_data)

                # Cache in L1 memory
                with self._lock:
                    self._evict_if_needed()
                    self._cache[key] = (bitmap, revision, time.time())

                logger.debug(f"[TIGER] L2 Dragonfly hit for {key}, {len(bitmap)} resources")
                return bitmap
            else:
                logger.debug(f"[TIGER] L2 Dragonfly miss for {key}")
            # Dragonfly miss or unavailable, fall through to L3

        # L3: Fall back to PostgreSQL
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
                revision = int(row.revision)

                # Cache in L1 memory
                with self._lock:
                    self._evict_if_needed()
                    self._cache[key] = (bitmap, revision, time.time())

                # Populate L2 Dragonfly (fire-and-forget)
                if self._dragonfly:
                    self._run_dragonfly_op(
                        operation="set",
                        subject_type=key.subject_type,
                        subject_id=key.subject_id,
                        permission=key.permission,
                        resource_type=key.resource_type,
                        bitmap_data=bytes(row.bitmap_data),
                        revision=revision,
                    )
                    logger.debug(f"[TIGER] Populated L2 Dragonfly from L3 for {key}")

                logger.debug(f"[TIGER] L3 PostgreSQL hit for {key}, {len(bitmap)} resources")
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

        # Bulk fetch from database (zone_id removed from cache key per Issue #979)
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
        # Each tuple: (subject_type, subject_id, permission, resource_type, resource_id, zone_id)
    ) -> dict[tuple[str, str, str, str, str, str], bool | None]:
        """Bulk check permissions using Tiger Cache with only 2 DB queries.

        This is the optimal bulk check method that:
        1. Collects all unique resources and cache keys
        2. Bulk loads all resource int IDs in one query
        3. Bulk loads all bitmaps in one query
        4. Checks each item against in-memory bitmaps

        Args:
            checks: List of (subject_type, subject_id, permission, resource_type, resource_id, zone_id)

        Returns:
            Dict mapping each check tuple to True (allowed), False (denied), or None (not in cache)
        """
        if not checks:
            return {}

        results: dict[tuple[str, str, str, str, str, str], bool | None] = {}

        # Step 1: Collect unique resources and cache keys
        # Note: resource key excludes zone - paths are globally unique
        unique_resources: set[tuple[str, str]] = set()  # (res_type, res_id)
        unique_keys: set[CacheKey] = set()

        for subj_type, subj_id, perm, res_type, res_id, _zone in checks:
            unique_resources.add((res_type, res_id))
            unique_keys.add(CacheKey(subj_type, subj_id, perm, res_type))

        with self._engine.connect() as conn:
            # Step 2: Bulk load resource int IDs (1 query)
            resource_ids = self._resource_map.bulk_get_int_ids(list(unique_resources), conn)

            # Step 3: Bulk load bitmaps (1 query)
            bitmaps = self._bulk_load_from_db(list(unique_keys), conn)

        # Step 4: Check each item against in-memory data
        for check in checks:
            subj_type, subj_id, perm, res_type, res_id, zone = check
            key = CacheKey(subj_type, subj_id, perm, res_type)
            resource_key = (res_type, res_id)  # No zone

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
        zone_id: str,
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
            zone_id: Zone ID
            resource_int_ids: Set of integer resource IDs the subject can access
            revision: Current revision for staleness detection
            conn: Optional database connection
        """
        from sqlalchemy import text

        logger.info(
            f"Tiger Cache UPDATE: {subject_type}:{subject_id} -> {permission} -> {resource_type} "
            f"(zone={zone_id}, {len(resource_int_ids)} resources, rev={revision}, "
            f"db={self._engine.url.database}, dialect={self._engine.dialect.name})"
        )

        # Create bitmap
        bitmap = RoaringBitmap(resource_int_ids)

        bitmap_data = bitmap.serialize()
        key = CacheKey(subject_type, subject_id, permission, resource_type)

        # Upsert to database (zone_id removed from unique constraint per Issue #979)
        # Note: zone_id still included in INSERT for backward compatibility (NOT NULL column)
        query: Any  # TextClause or tuple[TextClause, TextClause]
        if self._is_postgresql:
            query = text("""
                INSERT INTO tiger_cache
                    (subject_type, subject_id, permission, resource_type, zone_id, bitmap_data, revision, created_at, updated_at)
                VALUES
                    (:subject_type, :subject_id, :permission, :resource_type, :zone_id, :bitmap_data, :revision, NOW(), NOW())
                ON CONFLICT (subject_type, subject_id, permission, resource_type, zone_id)
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
                    (subject_type, subject_id, permission, resource_type, zone_id, bitmap_data, revision, created_at, updated_at)
                VALUES
                    (:subject_type, :subject_id, :permission, :resource_type, :zone_id, :bitmap_data, :revision, datetime('now'), datetime('now'))
            """)
            query = (update_query, insert_query)  # Tuple of queries for SQLite

        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "zone_id": zone_id,  # Keep for backward compatibility
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
                logger.info(f"[TIGER] L3 PostgreSQL write (via conn) for {key}")
            else:
                with self._engine.begin() as new_conn:
                    # Set short timeout for Tiger Cache ops - fail fast instead of blocking
                    if not self._is_postgresql:
                        new_conn.execute(text("PRAGMA busy_timeout=100"))
                    execute(new_conn)
                # Transaction committed after exiting 'with' block
                logger.info(f"[TIGER] L3 PostgreSQL write COMMITTED for {key}")
        except Exception as e:
            logger.error(f"[TIGER] L3 PostgreSQL write FAILED for {key}: {e}")
            raise

        # L2: Populate Dragonfly cache (write-through pattern)
        if self._dragonfly:
            self._run_dragonfly_op(
                operation="set",
                subject_type=subject_type,
                subject_id=subject_id,
                permission=permission,
                resource_type=resource_type,
                bitmap_data=bytes(bitmap_data),
                revision=revision,
            )
            logger.debug(f"[TIGER] L2 Dragonfly write for {key}")

        # L1: Update in-memory cache
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
        zone_id: str | None = None,
        conn: Connection | None = None,
    ) -> int:
        """Invalidate cache entries matching the criteria.

        Args:
            subject_type: Filter by subject type (None = all)
            subject_id: Filter by subject ID (None = all)
            permission: Filter by permission (None = all)
            resource_type: Filter by resource type (None = all)
            zone_id: Filter by zone (None = all)
            conn: Optional database connection

        Returns:
            Number of entries invalidated
        """
        from sqlalchemy import text

        logger.info(
            f"Tiger Cache INVALIDATE: subject={subject_type}:{subject_id}, "
            f"permission={permission}, resource_type={resource_type}, zone={zone_id}"
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
        if zone_id:
            conditions.append("zone_id = :zone_id")
            params["zone_id"] = zone_id

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

        # L2: Invalidate from Dragonfly cache (zone_id excluded per Issue #979)
        dragonfly_count = 0
        if self._dragonfly:
            dragonfly_count = (
                self._run_dragonfly_op(
                    operation="invalidate",
                    subject_type=subject_type or "",
                    subject_id=subject_id or "",
                    permission=permission or "",
                    resource_type=resource_type or "",
                )
                or 0
            )
            logger.debug(f"[TIGER] L2 Dragonfly invalidated {dragonfly_count} entries")

        # L1: Clear in-memory cache entries
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
                # Note: zone_id removed from CacheKey per Issue #979
                # Zone isolation is enforced during permission computation
                if match:
                    keys_to_remove.append(key)

            for key in keys_to_remove:
                del self._cache[key]

        logger.debug(
            f"[TIGER] Invalidated {count} L3 + {dragonfly_count} L2 + {len(keys_to_remove)} L1 entries"
        )
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
        zone_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
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
            zone_id: Zone ID (kept for API compatibility)
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
        zone_id: str,
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
            zone_id: Zone ID (used for resource lookup, not cache key)

        Returns:
            True if persisted successfully, False on error
        """
        from sqlalchemy import text

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        try:
            # Step 1: Get or create resource int ID (separate transaction to avoid commit conflicts)
            resource_int_id = self._resource_map.get_or_create_int_id(
                resource_type, resource_id, zone_id
            )

            with self._engine.begin() as conn:
                # Step 2: Load existing bitmap from DB (if exists)
                # IMPORTANT: skip_l2=True to read from database directly, avoiding stale L2 cache
                # This prevents race conditions when multiple concurrent grants happen
                existing_bitmap = self._load_from_db(key, conn, skip_l2=True)

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

                # Step 3: Persist to database (zone_id removed from key per Issue #979)
                # Note: zone_id still included in INSERT for backward compatibility
                bitmap_data = bitmap.serialize()

                if self._is_postgresql:
                    upsert_query = text("""
                        INSERT INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, zone_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
                             :bitmap_data, :revision, NOW(), NOW())
                        ON CONFLICT (subject_type, subject_id, permission, resource_type, zone_id)
                        DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                      revision = EXCLUDED.revision,
                                      updated_at = NOW()
                    """)
                else:
                    # SQLite: Use INSERT OR REPLACE
                    upsert_query = text("""
                        INSERT OR REPLACE INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, zone_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
                             :bitmap_data, :revision, datetime('now'), datetime('now'))
                    """)

                conn.execute(
                    upsert_query,
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
                # Commit happens automatically when exiting 'with' block

            # Step 4: Update L2 cache (Dragonfly) for cross-instance consistency
            if self._dragonfly:
                self._run_dragonfly_op(
                    operation="set",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    bitmap_data=bitmap_data,
                    revision=revision,
                )

            # Step 5: Update L1 in-memory cache
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
        zone_id: str,
    ) -> bool:
        """Write-through: Remove a single resource grant and persist to database.

        Critical for security - permission revocations must propagate immediately.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            resource_id: String ID of the resource being revoked
            zone_id: Zone ID (used for resource lookup, not cache key)

        Returns:
            True if persisted successfully, False on error
        """
        from sqlalchemy import text

        key = CacheKey(subject_type, subject_id, permission, resource_type)

        try:
            with self._engine.begin() as conn:
                # Step 1: Get resource int ID (don't create if doesn't exist)
                # Note: resource key excludes zone - paths are globally unique
                resource_key = (resource_type, resource_id)
                with self._lock:
                    resource_int_id = self._resource_map._uuid_to_int.get(resource_key)

                if resource_int_id is None:
                    # Try to get from DB (no zone filter)
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
                # IMPORTANT: skip_l2=True to read from database directly for atomic operation
                existing_bitmap = self._load_from_db(key, conn, skip_l2=True)

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

                # Step 4: Persist to database (zone_id removed from key per Issue #979)
                # Note: zone_id still included in INSERT for backward compatibility
                bitmap_data = bitmap.serialize()

                if self._is_postgresql:
                    upsert_query = text("""
                        INSERT INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, zone_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
                             :bitmap_data, :revision, NOW(), NOW())
                        ON CONFLICT (subject_type, subject_id, permission, resource_type, zone_id)
                        DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                      revision = EXCLUDED.revision,
                                      updated_at = NOW()
                    """)
                else:
                    upsert_query = text("""
                        INSERT OR REPLACE INTO tiger_cache
                            (subject_type, subject_id, permission, resource_type, zone_id,
                             bitmap_data, revision, created_at, updated_at)
                        VALUES
                            (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
                             :bitmap_data, :revision, datetime('now'), datetime('now'))
                    """)

                conn.execute(
                    upsert_query,
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

            # Step 5: Update L2 cache (Dragonfly) for cross-instance consistency
            if self._dragonfly:
                self._run_dragonfly_op(
                    operation="set",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    bitmap_data=bitmap_data,
                    revision=revision,
                )

            # Step 6: Update L1 in-memory cache
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
        zone_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
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
            zone_id: Zone ID (kept for API compatibility)
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
        zone_id: str,  # noqa: ARG002 - Kept for API compatibility, not used in cache key (Issue #979)
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
            zone_id: Zone ID (kept for API compatibility)
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
        zone_id: str = "default",
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
            zone_id: Zone ID (for backward compatibility, not used in cache key)

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

            # Note: zone_id still included in INSERT for backward compatibility
            if self._is_postgresql:
                upsert_query = text("""
                    INSERT INTO tiger_cache
                        (subject_type, subject_id, permission, resource_type, zone_id,
                         bitmap_data, revision, created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
                         :bitmap_data, :revision, NOW(), NOW())
                    ON CONFLICT (subject_type, subject_id, permission, resource_type, zone_id)
                    DO UPDATE SET bitmap_data = EXCLUDED.bitmap_data,
                                  revision = EXCLUDED.revision,
                                  updated_at = NOW()
                """)
            else:
                upsert_query = text("""
                    INSERT OR REPLACE INTO tiger_cache
                        (subject_type, subject_id, permission, resource_type, zone_id,
                         bitmap_data, revision, created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :resource_type, :zone_id,
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
                        "zone_id": zone_id,
                        "bitmap_data": bitmap_data,
                        "revision": revision,
                    },
                )

            # L2: Also populate Dragonfly cache (write-through)
            if self._dragonfly:
                self._run_dragonfly_op(
                    operation="set",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    bitmap_data=bytes(bitmap_data),
                    revision=revision,
                )
                logger.debug(f"[TIGER] L2 Dragonfly write (persist_bulk) for {key}")

            logger.debug(f"[TIGER] Persisted bulk bitmap for {key} ({len(bitmap)} resources)")
            return True

        except Exception as e:
            logger.error(f"[TIGER] persist_bitmap_bulk failed for {key}: {e}")
            return False

    # =========================================================================
    # Directory Grant Pre-materialization (Leopard-style)
    # =========================================================================

    def record_directory_grant(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        directory_path: str,
        zone_id: str,
        grant_revision: int,
        include_future_files: bool = True,
    ) -> int | None:
        """Record a directory-level grant for pre-materialization tracking.

        When a permission is granted on a directory, this records the grant
        so that:
        1. New files created under the directory can inherit the permission
        2. Grant revocation can clean up all expanded permissions
        3. File moves can update permissions based on ancestor grants

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission type (e.g., "read", "write")
            directory_path: Path of the directory granted (e.g., "/workspace/")
            zone_id: Zone ID
            grant_revision: Revision at time of grant (for consistency)
            include_future_files: Whether new files should inherit this grant

        Returns:
            Grant ID if created, None if already exists
        """
        from sqlalchemy import text

        # Normalize directory path (ensure trailing slash)
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        try:
            if self._is_postgresql:
                query = text("""
                    INSERT INTO tiger_directory_grants
                        (subject_type, subject_id, permission, directory_path, zone_id,
                         grant_revision, include_future_files, expansion_status, expanded_count,
                         created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :directory_path, :zone_id,
                         :grant_revision, :include_future_files, 'pending', 0,
                         NOW(), NOW())
                    ON CONFLICT (zone_id, directory_path, permission, subject_type, subject_id)
                    DO UPDATE SET
                        grant_revision = EXCLUDED.grant_revision,
                        include_future_files = EXCLUDED.include_future_files,
                        updated_at = NOW()
                    RETURNING grant_id
                """)
            else:
                query = text("""
                    INSERT OR REPLACE INTO tiger_directory_grants
                        (subject_type, subject_id, permission, directory_path, zone_id,
                         grant_revision, include_future_files, expansion_status, expanded_count,
                         created_at, updated_at)
                    VALUES
                        (:subject_type, :subject_id, :permission, :directory_path, :zone_id,
                         :grant_revision, :include_future_files, 'pending', 0,
                         datetime('now'), datetime('now'))
                """)

            with self._engine.begin() as conn:
                result = conn.execute(
                    query,
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "permission": permission,
                        "directory_path": directory_path,
                        "zone_id": zone_id,
                        "grant_revision": grant_revision,
                        "include_future_files": include_future_files,
                    },
                )
                if self._is_postgresql:
                    row = result.fetchone()
                    return int(row.grant_id) if row else None
                return None

        except Exception as e:
            logger.error(f"[TIGER] record_directory_grant failed: {e}")
            return None

    def get_directory_grants_for_path(
        self,
        path: str,
        zone_id: str,
    ) -> list[dict]:
        """Get all directory grants that would apply to a given path.

        Used when a new file is created to find all ancestor directory grants
        that should be inherited.

        Args:
            path: File path to check (e.g., "/workspace/project/file.txt")
            zone_id: Zone ID

        Returns:
            List of grant dictionaries with subject, permission, directory info
        """
        from sqlalchemy import text

        # Generate all ancestor paths
        ancestors = self._get_ancestor_paths(path)
        if not ancestors:
            return []

        try:
            if self._is_postgresql:
                query = text("""
                    SELECT grant_id, subject_type, subject_id, permission, directory_path,
                           grant_revision, include_future_files
                    FROM tiger_directory_grants
                    WHERE zone_id = :zone_id
                      AND directory_path = ANY(:ancestors)
                      AND expansion_status = 'completed'
                """)
                params = {"zone_id": zone_id, "ancestors": ancestors}
            else:
                # SQLite: Use IN clause
                placeholders = ", ".join([f":a{i}" for i in range(len(ancestors))])
                query = text(f"""
                    SELECT grant_id, subject_type, subject_id, permission, directory_path,
                           grant_revision, include_future_files
                    FROM tiger_directory_grants
                    WHERE zone_id = :zone_id
                      AND directory_path IN ({placeholders})
                      AND expansion_status = 'completed'
                """)
                params = {"zone_id": zone_id}
                for i, a in enumerate(ancestors):
                    params[f"a{i}"] = a

            grants = []
            with self._engine.connect() as conn:
                result = conn.execute(query, params)
                for row in result:
                    grants.append(
                        {
                            "grant_id": row.grant_id,
                            "subject_type": row.subject_type,
                            "subject_id": row.subject_id,
                            "permission": row.permission,
                            "directory_path": row.directory_path,
                            "grant_revision": row.grant_revision,
                            "include_future_files": row.include_future_files,
                        }
                    )

            return grants

        except Exception as e:
            logger.error(f"[TIGER] get_directory_grants_for_path failed: {e}")
            return []

    def _get_ancestor_paths(self, path: str) -> list[str]:
        """Get all ancestor directory paths for a given path.

        Args:
            path: File or directory path

        Returns:
            List of ancestor paths, from immediate parent to root
            Example: "/a/b/c/file.txt" -> ["/a/b/c/", "/a/b/", "/a/", "/"]
        """
        ancestors = []
        # Remove trailing slash and filename
        current = path.rstrip("/")

        while current and current != "/":
            # Get parent directory
            last_slash = current.rfind("/")
            if last_slash <= 0:
                ancestors.append("/")
                break
            current = current[:last_slash]
            ancestors.append(current + "/")

        return ancestors

    def expand_directory_grant(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        directory_path: str,
        zone_id: str,
        grant_revision: int,  # noqa: ARG002 - Reserved for future consistency checks
        descendants: list[str],
        batch_size: int = 1000,
    ) -> tuple[int, bool]:
        """Expand a directory grant to all descendants (pre-materialization).

        This is the core of Leopard-style permission expansion. When a permission
        is granted on a directory, this adds ALL descendant files to the user's
        bitmap for O(1) permission checks.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            directory_path: Directory path granted
            zone_id: Zone ID
            grant_revision: Revision for consistency
            descendants: List of descendant file paths
            batch_size: Number of files to process per batch

        Returns:
            Tuple of (files_expanded, completed)
        """

        if not descendants:
            self._update_grant_status(
                subject_type,
                subject_id,
                permission,
                directory_path,
                zone_id,
                status="completed",
                expanded_count=0,
                total_count=0,
            )
            return (0, True)

        # Update total count
        self._update_grant_status(
            subject_type,
            subject_id,
            permission,
            directory_path,
            zone_id,
            status="in_progress",
            total_count=len(descendants),
        )

        total_expanded = 0
        try:
            # Process in batches to avoid memory issues
            for i in range(0, len(descendants), batch_size):
                batch = descendants[i : i + batch_size]

                # Get/create int IDs for all files in batch
                resources = [("file", path) for path in batch]
                with self._engine.connect() as conn:
                    int_ids = self._resource_map.bulk_get_int_ids(resources, conn)

                # Create IDs for resources that don't exist yet
                for resource, int_id in int_ids.items():
                    if int_id is None:
                        new_id = self._resource_map.get_or_create_int_id(resource[0], resource[1])
                        if new_id > 0:
                            int_ids[resource] = new_id

                # Collect all valid int IDs
                valid_int_ids = {v for v in int_ids.values() if v is not None and v > 0}

                if valid_int_ids:
                    # Add to in-memory bitmap
                    self.add_to_bitmap_bulk(
                        subject_type, subject_id, permission, "file", zone_id, valid_int_ids
                    )

                total_expanded += len(valid_int_ids)

                # Update progress
                self._update_grant_status(
                    subject_type,
                    subject_id,
                    permission,
                    directory_path,
                    zone_id,
                    status="in_progress",
                    expanded_count=total_expanded,
                )

                logger.debug(
                    f"[TIGER] Expanded batch {i // batch_size + 1}: "
                    f"{len(valid_int_ids)} files, total {total_expanded}/{len(descendants)}"
                )

            # Persist the complete bitmap to database
            key = CacheKey(subject_type, subject_id, permission, "file")
            with self._lock:
                if key in self._cache:
                    bitmap, revision, _ = self._cache[key]
                    all_int_ids = set(bitmap.to_array())
                    self.persist_bitmap_bulk(
                        subject_type, subject_id, permission, "file", all_int_ids, zone_id
                    )

            # Mark as completed
            self._update_grant_status(
                subject_type,
                subject_id,
                permission,
                directory_path,
                zone_id,
                status="completed",
                expanded_count=total_expanded,
            )

            logger.info(
                f"[TIGER] Directory grant expansion completed: "
                f"{directory_path} -> {total_expanded} files for {subject_type}:{subject_id}"
            )

            return (total_expanded, True)

        except Exception as e:
            logger.error(f"[TIGER] expand_directory_grant failed: {e}")
            self._update_grant_status(
                subject_type,
                subject_id,
                permission,
                directory_path,
                zone_id,
                status="failed",
                error_message=str(e),
            )
            return (total_expanded, False)

    def _update_grant_status(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        directory_path: str,
        zone_id: str,
        status: str | None = None,
        expanded_count: int | None = None,
        total_count: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Update the status of a directory grant expansion."""
        from sqlalchemy import text

        updates: list[str] = []
        params: dict[str, str | int] = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "directory_path": directory_path,
            "zone_id": zone_id,
        }

        if status is not None:
            updates.append("expansion_status = :status")
            params["status"] = status
            if status == "completed":
                if self._is_postgresql:
                    updates.append("completed_at = NOW()")
                else:
                    updates.append("completed_at = datetime('now')")

        if expanded_count is not None:
            updates.append("expanded_count = :expanded_count")
            params["expanded_count"] = expanded_count

        if total_count is not None:
            updates.append("total_count = :total_count")
            params["total_count"] = total_count

        if error_message is not None:
            updates.append("error_message = :error_message")
            params["error_message"] = error_message

        if not updates:
            return

        if self._is_postgresql:
            updates.append("updated_at = NOW()")
        else:
            updates.append("updated_at = datetime('now')")

        query = text(f"""
            UPDATE tiger_directory_grants
            SET {", ".join(updates)}
            WHERE subject_type = :subject_type
              AND subject_id = :subject_id
              AND permission = :permission
              AND directory_path = :directory_path
              AND zone_id = :zone_id
        """)

        try:
            with self._engine.begin() as conn:
                conn.execute(query, params)
        except Exception as e:
            logger.error(f"[TIGER] _update_grant_status failed: {e}")

    def remove_directory_grant(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        directory_path: str,
        zone_id: str,
    ) -> bool:
        """Remove a directory grant and optionally clean up expanded permissions.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission type
            directory_path: Directory path to remove grant from
            zone_id: Zone ID

        Returns:
            True if removed successfully
        """
        from sqlalchemy import text

        # Normalize directory path
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        try:
            query = text("""
                DELETE FROM tiger_directory_grants
                WHERE subject_type = :subject_type
                  AND subject_id = :subject_id
                  AND permission = :permission
                  AND directory_path = :directory_path
                  AND zone_id = :zone_id
            """)

            with self._engine.begin() as conn:
                conn.execute(
                    query,
                    {
                        "subject_type": subject_type,
                        "subject_id": subject_id,
                        "permission": permission,
                        "directory_path": directory_path,
                        "zone_id": zone_id,
                    },
                )

            logger.info(
                f"[TIGER] Removed directory grant: {directory_path} "
                f"for {subject_type}:{subject_id} ({permission})"
            )
            return True

        except Exception as e:
            logger.error(f"[TIGER] remove_directory_grant failed: {e}")
            return False

    def add_file_to_ancestor_grants(
        self,
        file_path: str,
        zone_id: str,
    ) -> int:
        """Add a newly created file to all applicable ancestor directory grants.

        When a new file is created, this checks for any ancestor directory grants
        and adds the file to those users' bitmaps.

        Also ensures the file is registered in tiger_resource_map for predicate
        pushdown to work correctly (Issue #1030).

        Args:
            file_path: Path of the newly created file
            zone_id: Zone ID

        Returns:
            Number of grants the file was added to
        """
        # Always register file in tiger_resource_map for predicate pushdown (Issue #1030)
        # This must happen BEFORE the early return check for ancestor grants
        int_id = self._resource_map.get_or_create_int_id("file", file_path)
        if int_id <= 0:
            logger.error(f"[TIGER] Failed to get int_id for new file: {file_path}")
            return 0

        grants = self.get_directory_grants_for_path(file_path, zone_id)
        if not grants:
            return 0

        added_count = 0
        for grant in grants:
            # Check if grant includes future files
            if not grant.get("include_future_files", True):
                continue

            try:
                # Add to user's bitmap
                self.add_to_bitmap(
                    grant["subject_type"],
                    grant["subject_id"],
                    grant["permission"],
                    "file",
                    zone_id,
                    int_id,
                )

                # Persist immediately (write-through)
                self.persist_single_grant(
                    grant["subject_type"],
                    grant["subject_id"],
                    grant["permission"],
                    "file",
                    file_path,
                    zone_id,
                )

                added_count += 1
                logger.debug(
                    f"[TIGER] Added new file {file_path} to grant "
                    f"{grant['subject_type']}:{grant['subject_id']} ({grant['permission']})"
                )

            except Exception as e:
                logger.error(f"[TIGER] Failed to add file to grant: {e}")

        if added_count > 0:
            logger.info(f"[TIGER] New file {file_path} added to {added_count} ancestor grants")

            # Issue #1147: Increment zone revision when bitmap changes
            # This enables revision-based consistency checks in list() to detect
            # concurrent writes and avoid returning stale results
            try:
                from sqlalchemy import text

                if self._is_postgresql:
                    # PostgreSQL: Atomic upsert with increment
                    query = text("""
                        INSERT INTO rebac_version_sequences (zone_id, current_version, updated_at)
                        VALUES (:zone_id, 1, NOW())
                        ON CONFLICT (zone_id)
                        DO UPDATE SET current_version = rebac_version_sequences.current_version + 1,
                                      updated_at = NOW()
                    """)
                else:
                    # SQLite: Use INSERT OR REPLACE
                    query = text("""
                        INSERT OR REPLACE INTO rebac_version_sequences (zone_id, current_version, updated_at)
                        VALUES (
                            :zone_id,
                            COALESCE((SELECT current_version FROM rebac_version_sequences WHERE zone_id = :zone_id), 0) + 1,
                            CURRENT_TIMESTAMP
                        )
                    """)

                with self._engine.begin() as conn:
                    conn.execute(query, {"zone_id": zone_id})
                    logger.debug(
                        f"[TIGER] Incremented zone revision for {zone_id} after adding file to grants"
                    )
            except Exception as e:
                logger.warning(f"[TIGER] Failed to increment zone revision: {e}")

        return added_count

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
