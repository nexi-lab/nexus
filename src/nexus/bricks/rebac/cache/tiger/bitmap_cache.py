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

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pyroaring import BitMap as RoaringBitmap
from sqlalchemy import delete, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from nexus.bricks.rebac.consistency.metastore_version_store import MetastoreVersionStore

try:
    from fastbloom_rs import BloomFilter
except ImportError:
    BloomFilter = None  # noqa: N816 — optional Rust dependency

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models.permissions import TigerCacheModel as TC
from nexus.storage.models.permissions import TigerDirectoryGrantsModel as TDG
from nexus.storage.models.permissions import TigerResourceMapModel as TRM

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

    from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.core.protocols.caching import TigerCacheProtocol

logger = logging.getLogger(__name__)


@dataclass
class CacheKey:
    """Key for Tiger Cache lookup.

    zone_id is included for multi-zone namespace isolation — each zone
    gets its own cache partition to prevent cross-zone cache pollution.
    """

    subject_type: str
    subject_id: str
    permission: str
    resource_type: str
    zone_id: str = ""

    def __hash__(self) -> int:
        return hash(
            (
                self.subject_type,
                self.subject_id,
                self.permission,
                self.resource_type,
                self.zone_id,
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
        engine: "Engine",
        resource_map: "TigerResourceMap | None" = None,
        rebac_manager: "ReBACManager | None" = None,
        dragonfly_cache: "TigerCacheProtocol | None" = None,
        l2_max_workers: int = 4,
        *,
        is_postgresql: bool = False,
        version_store: MetastoreVersionStore | None = None,
    ):
        """Initialize Tiger Cache.

        Args:
            engine: SQLAlchemy database engine
            resource_map: Resource mapping service (created if not provided)
            rebac_manager: ReBAC manager for permission computation
            dragonfly_cache: Optional Dragonfly cache for L2 distributed caching
            l2_max_workers: Thread pool size for L2 dragonfly operations.
                Sourced from ProfileTuning.cache.tiger_max_workers.
            is_postgresql: Whether the database is PostgreSQL (config-time flag).
        """
        from nexus.bricks.rebac.cache.tiger.resource_map import TigerResourceMap as _TRM

        self._engine = engine
        self._resource_map = resource_map or _TRM(engine, is_postgresql=is_postgresql)
        self._rebac_manager = rebac_manager
        self._is_postgresql = is_postgresql
        self._version_store = version_store

        # L2: Dragonfly distributed cache (optional)
        self._dragonfly: TigerCacheProtocol | None = dragonfly_cache
        self._dragonfly_url: str | None = None  # Cached URL for sync Redis client

        # L1: In-memory cache for hot entries
        self._cache: dict[
            CacheKey, tuple[Any, int, float]
        ] = {}  # key -> (bitmap, revision, cached_at)
        self._cache_ttl = 300  # 5 minutes (same for L1 and L2 for consistency)
        self._cache_max_size = 100_000  # Increased from 10k per Issue #979
        self._lock = threading.RLock()

        # BloomFilter pre-gate for L2 Dragonfly lookups (Issue #3192)
        # Rejects definite negatives before network round-trip to Dragonfly
        self._l2_bloom_capacity = 100_000
        self._l2_bloom_fpp = 0.01  # 1% false positive rate
        self._bloom_rejects = 0
        self._bloom_passes = 0
        if BloomFilter is not None:
            self._l2_bloom = BloomFilter(self._l2_bloom_capacity, self._l2_bloom_fpp)
        else:
            self._l2_bloom = None

        # Stats counters for observability
        self._stats_hits = 0
        self._stats_misses = 0
        self._stats_sets = 0
        self._stats_invalidations = 0

        # Persistent thread pool for L2 operations (avoid per-operation creation)
        self._l2_executor: Any | None = None
        self._l2_max_workers = l2_max_workers

    @property
    def resource_map(self) -> "TigerResourceMap":
        """Public accessor for the resource map."""
        return self._resource_map

    def set_dragonfly_cache(self, dragonfly_cache: "TigerCacheProtocol | None") -> None:
        """Set or update the Dragonfly cache backend.

        This allows late binding of the Dragonfly cache after initialization,
        useful when the cache factory initializes after TigerCache.

        Args:
            dragonfly_cache: TigerCacheProtocol instance or None to disable
        """
        self._dragonfly = dragonfly_cache
        if dragonfly_cache:
            # Cache URL for sync Redis operations
            _client = getattr(dragonfly_cache, "_client", None)
            self._dragonfly_url = getattr(_client, "_url", None) if _client else None
            # Create persistent thread pool (max 4 workers for L2 ops)
            import concurrent.futures

            if self._l2_executor is None:
                self._l2_executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=self._l2_max_workers, thread_name_prefix="tiger-l2"
                )
            logger.info("[TIGER] Dragonfly L2 cache enabled")
        else:
            self._dragonfly_url = None
            # Shutdown executor if exists
            if self._l2_executor:
                self._l2_executor.shutdown(wait=False)
                self._l2_executor = None
            logger.info("[TIGER] Dragonfly L2 cache disabled")

    @staticmethod
    def _bloom_key(key: CacheKey) -> str:
        """Canonical bloom filter key — same format as Dragonfly redis key."""
        base = f"tiger:{key.subject_type}:{key.subject_id}:{key.permission}:{key.resource_type}"
        return f"{base}:{key.zone_id}" if key.zone_id else base

    def _bloom_add(self, key: CacheKey) -> None:
        """Add a key to the bloom filter (call on every L1/L2 cache set)."""
        if self._l2_bloom is not None:
            self._l2_bloom.add(self._bloom_key(key))

    def _rebuild_l2_bloom(self) -> None:
        """Rebuild BloomFilter from current L1 cache keys."""
        if BloomFilter is None:
            return
        bloom = BloomFilter(self._l2_bloom_capacity, self._l2_bloom_fpp)
        with self._lock:
            for key in self._cache:
                bloom.add(self._bloom_key(key))
        self._l2_bloom = bloom

    def _bloom_might_contain(self, key: CacheKey) -> bool:
        """Check BloomFilter for possible L2 presence.

        Returns True if key MIGHT be in L2 (possible false positive).
        Returns True if bloom filter not initialized (fail-open).
        """
        if self._l2_bloom is None:
            return True  # fail-open: no bloom = always check L2
        result = self._bloom_key(key) in self._l2_bloom
        if result:
            self._bloom_passes += 1
        else:
            self._bloom_rejects += 1
        return result

    def _run_dragonfly_op(
        self,
        operation: str,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str = "",
        bitmap_data: bytes | None = None,
        revision: int = 0,
        timeout: float = 5.0,
    ) -> Any:
        """Run a Dragonfly operation using sync Redis client.

        Uses a persistent thread pool and sync Redis client to avoid
        event loop conflicts with FastAPI's async context.

        Key format: tiger:{subject_type}:{subject_id}:{permission}:{resource_type}[:{zone_id}]

        Args:
            operation: One of "get", "set", "invalidate"
            subject_type: Subject type for cache key
            subject_id: Subject ID for cache key
            permission: Permission for cache key
            resource_type: Resource type for cache key
            zone_id: Zone ID for cache partitioning
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
                key = (
                    f"tiger:{subject_type}:{subject_id}:{permission}:{resource_type}:{zone_id}"
                    if zone_id
                    else f"tiger:{subject_type}:{subject_id}:{permission}:{resource_type}"
                )

                if operation == "get":
                    result = client.hgetall(key)
                    if not isinstance(result, dict) or not result:
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
                    # Update bloom filter using canonical key (Issue #3192)
                    self._bloom_add(
                        CacheKey(subject_type, subject_id, permission, resource_type, zone_id)
                    )
                    return True

                elif operation == "delete_exact":
                    # Exact key deletion — O(1) single DEL (Issue #3395)
                    return client.delete(key)

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
                    if zone_id:
                        parts.append(zone_id)
                    pattern = ":".join(parts)
                    count = 0
                    # Use SCAN for safe iteration
                    cursor = 0
                    while True:
                        _scan_result = client.scan(cursor=cursor, match=pattern, count=100)
                        if not isinstance(_scan_result, tuple):
                            break
                        cursor, keys = _scan_result
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
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            logger.warning("[TIGER] L2 Dragonfly operation timed out")
            return None
        except Exception as e:
            logger.warning("[TIGER] L2 Dragonfly error: %s", e)
            return None

    def evict_cached(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str = "",
    ) -> int:
        """Evict Tiger L1 (in-memory) and L2 (Dragonfly) cached entries (Issue #3395).

        Clears both the in-process bitmap cache and the Dragonfly distributed
        cache without touching L3 (PostgreSQL).  Called by CacheCoordinator
        via callback for coordinated invalidation on permission writes.

        Deletes both the zone-scoped L2 key (``tiger:...:zone_id``) and the
        zone-agnostic key (``tiger:...``) to prevent stale reads via either
        the ``get_accessible_resources`` or ``check_access`` code paths.

        Returns:
            Number of entries evicted (L1 + L2).
        """
        evicted = 0

        # L1: Evict matching in-memory entries (all zone variants)
        with self._lock:
            keys_to_remove = [
                k
                for k in self._cache
                if k.subject_type == subject_type
                and k.subject_id == subject_id
                and k.permission == permission
                and k.resource_type == resource_type
            ]
            for k in keys_to_remove:
                del self._cache[k]
            evicted += len(keys_to_remove)

        # L2: Delete from Dragonfly (exact key, 1s timeout, fail-open)
        if self._dragonfly:
            # Zone-scoped key (tiger:...:zone_id)
            if zone_id:
                evicted += (
                    self._run_dragonfly_op(
                        operation="delete_exact",
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type=resource_type,
                        zone_id=zone_id,
                        timeout=1.0,
                    )
                    or 0
                )
            # Zone-agnostic key (tiger:...) — always delete
            evicted += (
                self._run_dragonfly_op(
                    operation="delete_exact",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    zone_id="",
                    timeout=1.0,
                )
                or 0
            )

        return evicted

    def set_rebac_manager(self, manager: "ReBACManager") -> None:
        """Set the ReBAC manager for permission computation."""
        self._rebac_manager = manager

    def get_accessible_resources(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        conn: "Connection | None" = None,
    ) -> set[int]:
        """Get all resource integer IDs that subject can access.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            zone_id: Zone ID for cache partitioning
            conn: Optional database connection

        Returns:
            Set of integer resource IDs
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[TIGER] Memory cache hit for %s", key)
                    return set(bitmap)

        # Load from database
        bitmap = self._load_from_db(key, conn)
        if bitmap is not None:
            return set(bitmap)

        # Cache miss - return empty set (cache will be populated by background worker)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[TIGER] Cache miss for %s", key)
        return set()

    def check_access(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_id: str,
        conn: "Connection | None" = None,
    ) -> bool | None:
        """Check if subject has permission on resource using cached bitmap.

        Args:
            subject_type: Type of subject
            subject_id: ID of subject
            permission: Permission to check
            resource_type: Type of resource
            resource_id: String ID of resource
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
                    self._stats_hits += 1
                    logger.debug(
                        "Tiger Cache MEMORY HIT: %s:%s -> %s -> %s:%s = %s",
                        subject_type,
                        subject_id,
                        permission,
                        resource_type,
                        resource_id,
                        result,
                    )
                    return result
                else:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("Tiger Cache MEMORY EXPIRED: %s", key)

        # Load from database
        bitmap = self._load_from_db(key, conn)
        if bitmap is not None:
            result = int_id in bitmap
            self._stats_hits += 1
            logger.debug(
                "Tiger Cache DB HIT: %s:%s -> %s -> %s:%s = %s",
                subject_type,
                subject_id,
                permission,
                resource_type,
                resource_id,
                result,
            )
            return result

        # Not in cache
        self._stats_misses += 1
        logger.debug(
            "Tiger Cache MISS: %s:%s -> %s -> %s:%s",
            subject_type,
            subject_id,
            permission,
            resource_type,
            resource_id,
        )
        return None

    def get_bitmap_bytes(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
        conn: "Connection | None" = None,
    ) -> bytes | None:
        """Get serialized bitmap bytes for Rust interop (Issue #896).

        Returns the raw serialized Roaring Bitmap bytes that can be passed
        to Rust's filter_paths_with_tiger_cache() for O(1) permission filtering.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission to check (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            zone_id: Zone ID for cache partitioning
            conn: Optional database connection

        Returns:
            Serialized bitmap bytes if found, None if not in cache
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[TIGER] get_bitmap_bytes memory hit for %s", key)
                    return bytes(bitmap.serialize())

        # Load from database and return raw bytes
        stmt = select(TC.bitmap_data, TC.revision).where(
            TC.subject_type == key.subject_type,
            TC.subject_id == key.subject_id,
            TC.permission == key.permission,
            TC.resource_type == key.resource_type,
        )

        def execute(connection: "Connection") -> bytes | None:
            result = connection.execute(stmt)
            row = result.fetchone()
            if row:
                # Cache the deserialized bitmap in memory for future use
                bitmap = RoaringBitmap.deserialize(row.bitmap_data)
                with self._lock:
                    self._evict_if_needed()
                    self._cache[key] = (bitmap, int(row.revision), time.time())
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[TIGER] get_bitmap_bytes DB hit for %s", key)
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
        zone_id: str = "",
    ) -> float | None:
        """Get cache age in seconds for a specific entry (Issue #921).

        Used by HotspotDetector to determine if hot entries need prefetching
        before TTL expiry.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: ID of subject
            permission: Permission (e.g., "read", "write")
            resource_type: Type of resource (e.g., "file")
            zone_id: Zone ID for cache partitioning

        Returns:
            Age in seconds if entry is in memory cache, None if not cached
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

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
        zone_id: str = "",
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
            zone_id: Zone ID for cache partitioning. Zone-scoped predicate pushdown
                must only read the matching zone-scoped bitmap.

        Returns:
            Set of integer IDs the subject can access, or None if no bitmap cached.
            Returns empty set if bitmap exists but has no entries.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        # Check in-memory cache first
        with self._lock:
            if key in self._cache:
                bitmap, revision, cached_at = self._cache[key]
                if time.time() - cached_at < self._cache_ttl:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "[TIGER-PUSHDOWN] Memory hit for %s, %d entries",
                            key,
                            len(bitmap),
                        )
                    return set(bitmap)  # RoaringBitmap is iterable

        # Load from database
        bitmap = self._load_from_db(key)
        if bitmap is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TIGER-PUSHDOWN] DB hit for %s, %d entries", key, len(bitmap))
            return set(bitmap)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[TIGER-PUSHDOWN] No bitmap found for %s", key)
        return None

    def get_accessible_paths(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str = "",
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
        int_ids = self.get_accessible_int_ids(
            subject_type,
            subject_id,
            permission,
            resource_type,
            zone_id=zone_id,
        )
        if int_ids is None:
            return None

        # Convert int IDs back to paths using resource map
        paths: set[str] = set()
        with self._resource_map._lock:
            for int_id in int_ids:
                key = self._resource_map._int_to_uuid.get(int_id)
                if key and key[0] == resource_type:
                    paths.add(key[1])  # key is (type, path)

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[TIGER-PUSHDOWN] Converted %d int IDs to %d paths", len(int_ids), len(paths)
            )
        return paths

    def _load_from_db(
        self, key: CacheKey, conn: "Connection | None" = None, skip_l2: bool = False
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
            # BloomFilter pre-gate: skip L2 round-trip for definite negatives (Issue #3192)
            if not self._bloom_might_contain(key):
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[TIGER] BloomFilter rejected L2 lookup for %s", key)
                # Fall through to L3 (database)
            else:
                result = self._run_dragonfly_op(
                    operation="get",
                    subject_type=key.subject_type,
                    subject_id=key.subject_id,
                    permission=key.permission,
                    resource_type=key.resource_type,
                    zone_id=key.zone_id,
                )
                if result:
                    bitmap_data, revision = result
                    bitmap = RoaringBitmap.deserialize(bitmap_data)

                    # Cache in L1 memory
                    with self._lock:
                        self._evict_if_needed()
                        self._cache[key] = (bitmap, revision, time.time())

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "[TIGER] L2 Dragonfly hit for %s, %d resources", key, len(bitmap)
                        )
                    return bitmap
                else:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[TIGER] L2 Dragonfly miss for %s", key)
            # Dragonfly miss, bloom rejection, or unavailable — fall through to L3

        # L3: Fall back to PostgreSQL
        stmt = select(TC.bitmap_data, TC.revision).where(
            TC.subject_type == key.subject_type,
            TC.subject_id == key.subject_id,
            TC.permission == key.permission,
            TC.resource_type == key.resource_type,
        )
        if key.zone_id:
            stmt = stmt.where(TC.zone_id == key.zone_id)

        def execute(connection: "Connection") -> Any:  # Returns Bitmap or None
            result = connection.execute(stmt)
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
                        zone_id=key.zone_id,
                        bitmap_data=bytes(row.bitmap_data),
                        revision=revision,
                    )
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[TIGER] Populated L2 Dragonfly from L3 for %s", key)

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[TIGER] L3 PostgreSQL hit for %s, %d resources", key, len(bitmap))
                return bitmap
            return None

        if conn:
            return execute(conn)
        else:
            with self._engine.connect() as new_conn:
                return execute(new_conn)

    def _bulk_load_from_db(self, keys: list[CacheKey], conn: "Connection") -> dict[CacheKey, Any]:
        """Bulk load bitmaps from database in a single query.

        Args:
            keys: List of cache keys to load
            conn: Database connection

        Returns:
            Dict mapping cache keys to their bitmaps (missing keys not included)
        """
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

        # Bulk fetch from database using OR conditions (dialect-agnostic)
        batch_size = 100
        for i in range(0, len(to_fetch), batch_size):
            batch = to_fetch[i : i + batch_size]
            conditions = [
                (TC.subject_type == k.subject_type)
                & (TC.subject_id == k.subject_id)
                & (TC.permission == k.permission)
                & (TC.resource_type == k.resource_type)
                for k in batch
            ]
            stmt = select(
                TC.subject_type,
                TC.subject_id,
                TC.permission,
                TC.resource_type,
                TC.bitmap_data,
                TC.revision,
            ).where(or_(*conditions))

            db_result = conn.execute(stmt)

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
        conn: "Connection | None" = None,
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
        logger.info(
            "Tiger Cache UPDATE: %s:%s -> %s -> %s "
            "(zone=%s, %d resources, rev=%s, "
            "db=%s, dialect=%s)",
            subject_type,
            subject_id,
            permission,
            resource_type,
            zone_id,
            len(resource_int_ids),
            revision,
            self._engine.url.database,
            self._engine.dialect.name,
        )

        # Create bitmap
        bitmap = RoaringBitmap(resource_int_ids)

        bitmap_data = bitmap.serialize()
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)
        now = datetime.now(UTC)

        # Upsert to database (zone_id removed from unique constraint per Issue #979)
        # Note: zone_id still included in INSERT for backward compatibility (NOT NULL column)
        params = {
            "subject_type": subject_type,
            "subject_id": subject_id,
            "permission": permission,
            "resource_type": resource_type,
            "zone_id": zone_id,
            "bitmap_data": bitmap_data,
            "revision": revision,
        }

        def execute(connection: "Connection") -> None:
            if self._is_postgresql:
                pg_stmt = pg_insert(TC).values(**params, created_at=now, updated_at=now)
                pg_stmt = pg_stmt.on_conflict_do_update(
                    constraint="uq_tiger_cache",
                    set_={
                        "bitmap_data": pg_stmt.excluded.bitmap_data,
                        "revision": pg_stmt.excluded.revision,
                        "updated_at": now,
                    },
                )
                connection.execute(pg_stmt)
            else:
                # SQLite: Try UPDATE first, then INSERT if no rows affected
                result = connection.execute(
                    update(TC)
                    .where(
                        TC.subject_type == subject_type,
                        TC.subject_id == subject_id,
                        TC.permission == permission,
                        TC.resource_type == resource_type,
                    )
                    .values(bitmap_data=bitmap_data, revision=revision, updated_at=now)
                )
                if result.rowcount == 0:
                    connection.execute(insert(TC).values(**params, created_at=now, updated_at=now))

        try:
            if conn:
                execute(conn)
                logger.info("[TIGER] L3 PostgreSQL write (via conn) for %s", key)
            else:
                with self._engine.begin() as new_conn:
                    execute(new_conn)
                # Transaction committed after exiting 'with' block
                logger.info("[TIGER] L3 PostgreSQL write COMMITTED for %s", key)
        except Exception as e:
            logger.error("[TIGER] L3 PostgreSQL write FAILED for %s: %s", key, e)
            raise

        # L2: Populate Dragonfly cache (write-through pattern)
        if self._dragonfly:
            self._run_dragonfly_op(
                operation="set",
                subject_type=subject_type,
                subject_id=subject_id,
                permission=permission,
                resource_type=resource_type,
                zone_id=zone_id,
                bitmap_data=bytes(bitmap_data),
                revision=revision,
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TIGER] L2 Dragonfly write for %s", key)

        # L1: Update in-memory cache
        with self._lock:
            self._evict_if_needed()
            self._cache[key] = (bitmap, revision, time.time())
            self._stats_sets += 1

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("[TIGER] Updated cache for %s, %d resources", key, len(resource_int_ids))

    def invalidate(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        permission: str | None = None,
        resource_type: str | None = None,
        zone_id: str | None = None,
        conn: "Connection | None" = None,
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
        logger.info(
            "Tiger Cache INVALIDATE: subject=%s:%s, permission=%s, resource_type=%s, zone=%s",
            subject_type,
            subject_id,
            permission,
            resource_type,
            zone_id,
        )

        # Build WHERE conditions dynamically using ORM
        conditions = []
        if subject_type:
            conditions.append(TC.subject_type == subject_type)
        if subject_id:
            conditions.append(TC.subject_id == subject_id)
        if permission:
            conditions.append(TC.permission == permission)
        if resource_type:
            conditions.append(TC.resource_type == resource_type)
        if zone_id:
            conditions.append(TC.zone_id == zone_id)

        # Delete from database
        stmt = delete(TC)
        if conditions:
            stmt = stmt.where(*conditions)

        def execute(connection: "Connection") -> int:
            result = connection.execute(stmt)
            return result.rowcount

        if conn:
            count = execute(conn)
        else:
            with self._engine.begin() as new_conn:
                count = execute(new_conn)

        # L2: Invalidate from Dragonfly cache
        dragonfly_count = 0
        if self._dragonfly:
            dragonfly_count = (
                self._run_dragonfly_op(
                    operation="invalidate",
                    subject_type=subject_type or "",
                    subject_id=subject_id or "",
                    permission=permission or "",
                    resource_type=resource_type or "",
                    zone_id=zone_id or "",
                )
                or 0
            )
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("[TIGER] L2 Dragonfly invalidated %d entries", dragonfly_count)

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

        self._stats_invalidations += count + dragonfly_count + len(keys_to_remove)
        logger.debug(
            "[TIGER] Invalidated %d L3 + %d L2 + %d L1 entries",
            count,
            dragonfly_count,
            len(keys_to_remove),
        )
        return count

    def _evict_if_needed(self) -> None:
        """Evict old entries if cache is too large (must hold lock)."""
        if len(self._cache) >= self._cache_max_size:
            # Evict 10% oldest entries
            entries = sorted(self._cache.items(), key=lambda x: x[1][2])
            num_to_evict = max(1, len(entries) // 10)
            logger.info(
                "Tiger Cache EVICT: cache full (%d/%d), evicting %d oldest entries",
                len(self._cache),
                self._cache_max_size,
                num_to_evict,
            )
            for key, _ in entries[:num_to_evict]:
                del self._cache[key]

    def clear_memory_cache(self) -> None:
        """Clear in-memory cache."""
        with self._lock:
            self._cache.clear()

    def get_stats(self) -> dict[str, Any]:
        """Return cache statistics for observability.

        Returns a dict with hit/miss/set/invalidation counts and L1 cache size.
        Exposed via the /cache/stats endpoint.
        """
        with self._lock:
            l1_size = len(self._cache)
        return {
            "hits": self._stats_hits,
            "misses": self._stats_misses,
            "sets": self._stats_sets,
            "invalidations": self._stats_invalidations,
            "l1_size": l1_size,
            "l1_max_size": self._cache_max_size,
            "l1_ttl_seconds": self._cache_ttl,
            "l2_enabled": self._dragonfly is not None,
            "bloom_passes": self._bloom_passes,
            "bloom_rejects": self._bloom_rejects,
            "bloom_initialized": self._l2_bloom is not None,
        }

    def batch_get_bitmaps(
        self,
        keys: list[CacheKey],
        conn: "Connection | None" = None,
    ) -> dict[CacheKey, set[int]]:
        """Batch get bitmaps for multiple keys using Redis pipeline.

        Reduces N round-trips to 1 for cold cache scenarios (e.g., readdir).

        Args:
            keys: List of CacheKey objects to fetch
            conn: Optional database connection

        Returns:
            Dict mapping keys to sets of accessible resource integer IDs
        """
        results: dict[CacheKey, set[int]] = {}
        l2_keys: list[CacheKey] = []
        l3_keys: list[CacheKey] = []

        # 1. Check L1 cache first
        with self._lock:
            for key in keys:
                if key in self._cache:
                    bitmap, revision, cached_at = self._cache[key]
                    if time.time() - cached_at < self._cache_ttl:
                        results[key] = set(bitmap)
                        self._stats_hits += 1
                        continue
                # L1 miss — check bloom filter for L2
                if self._bloom_might_contain(key):
                    l2_keys.append(key)
                else:
                    l3_keys.append(key)  # Bloom says not in L2, skip to L3
                    self._stats_misses += 1

        # 2. Batch L2 fetch via Redis pipeline
        if l2_keys and self._dragonfly and self._dragonfly_url and self._l2_executor:
            l2_results = self._batch_dragonfly_get(l2_keys)
            for key, bitmap_data in l2_results.items():
                if bitmap_data is not None:
                    data, rev = bitmap_data
                    bitmap = RoaringBitmap.deserialize(data)
                    results[key] = set(bitmap)
                    # Update L1
                    with self._lock:
                        self._evict_if_needed()
                        self._cache[key] = (bitmap, rev, time.time())
                        self._stats_hits += 1
                else:
                    l3_keys.append(key)  # L2 miss, fall to L3
                    self._stats_misses += 1
        else:
            l3_keys.extend(l2_keys)

        # 3. L3 fetch from database (individual queries)
        for key in l3_keys:
            bitmap = self._load_from_db(key, conn)
            if bitmap is not None:
                results[key] = set(bitmap)

        return results

    def _batch_dragonfly_get(
        self,
        keys: list[CacheKey],
    ) -> dict[CacheKey, tuple[bytes, int] | None]:
        """Batch fetch from Dragonfly using Redis pipeline.

        Args:
            keys: Cache keys to fetch

        Returns:
            Dict mapping keys to (bitmap_data, revision) or None
        """
        if not self._dragonfly_url or not self._l2_executor:
            return {}

        import concurrent.futures

        def run_batch_get() -> dict[CacheKey, tuple[bytes, int] | None]:
            import redis

            url = self._dragonfly_url
            assert url is not None
            client = redis.from_url(
                url,
                decode_responses=False,
                socket_timeout=3.0,
                socket_connect_timeout=2.0,
            )
            try:
                pipe = client.pipeline()
                redis_keys = []
                for key in keys:
                    redis_key = f"tiger:{key.subject_type}:{key.subject_id}:{key.permission}:{key.resource_type}"
                    pipe.hgetall(redis_key)
                    redis_keys.append(key)

                results_list = pipe.execute()
                result_map: dict[CacheKey, tuple[bytes, int] | None] = {}

                for cache_key, redis_result in zip(redis_keys, results_list, strict=False):
                    if redis_result and b"data" in redis_result and b"revision" in redis_result:
                        result_map[cache_key] = (
                            redis_result[b"data"],
                            int(redis_result[b"revision"]),
                        )
                    else:
                        result_map[cache_key] = None

                return result_map
            finally:
                client.close()

        try:
            future = self._l2_executor.submit(run_batch_get)
            result: dict[CacheKey, tuple[bytes, int] | None] = future.result(timeout=5.0)
            return result
        except concurrent.futures.TimeoutError:
            logger.warning("[TIGER] Batch L2 Dragonfly get timed out")
            return dict.fromkeys(keys)
        except Exception as e:
            logger.warning(f"[TIGER] Batch L2 Dragonfly error: {e}")
            return dict.fromkeys(keys)

    def add_to_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
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
            zone_id: Zone ID for cache partitioning
            resource_int_id: Integer ID of the resource to add

        Returns:
            True if added successfully, False otherwise
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

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
                    "[TIGER] Added resource %d to bitmap for %s (now %d resources)",
                    resource_int_id,
                    key,
                    len(bitmap),
                )
                return True
            else:
                # Create new bitmap with this single resource
                bitmap = RoaringBitmap([resource_int_id])
                self._evict_if_needed()
                self._cache[key] = (bitmap, 0, time.time())
                logger.debug(
                    "[TIGER] Created new bitmap for %s with resource %d",
                    key,
                    resource_int_id,
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
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        try:
            # Step 1: Get or create resource int ID (separate transaction to avoid commit conflicts)
            resource_int_id = self._resource_map.get_or_create_int_id(resource_type, resource_id)

            with self._engine.begin() as conn:
                # Step 2: Load existing bitmap from DB (if exists)
                # IMPORTANT: skip_l2=True to read from database directly, avoiding stale L2 cache
                # This prevents race conditions when multiple concurrent grants happen
                existing_bitmap = self._load_from_db(key, conn, skip_l2=True)

                if existing_bitmap is not None:
                    # Check if already in bitmap
                    if resource_int_id in existing_bitmap:
                        logger.debug(
                            "[TIGER] Resource %s already in bitmap for %s",
                            resource_id,
                            subject_id,
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
                now = datetime.now(UTC)

                if self._is_postgresql:
                    pg_stmt = pg_insert(TC).values(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type=resource_type,
                        zone_id=zone_id,
                        bitmap_data=bitmap_data,
                        revision=revision,
                        created_at=now,
                        updated_at=now,
                    )
                    pg_stmt = pg_stmt.on_conflict_do_update(
                        constraint="uq_tiger_cache",
                        set_={
                            "bitmap_data": pg_stmt.excluded.bitmap_data,
                            "revision": pg_stmt.excluded.revision,
                            "updated_at": now,
                        },
                    )
                    conn.execute(pg_stmt)
                else:
                    # SQLite: Use INSERT OR REPLACE
                    conn.execute(
                        insert(TC)
                        .prefix_with("OR REPLACE")
                        .values(
                            subject_type=subject_type,
                            subject_id=subject_id,
                            permission=permission,
                            resource_type=resource_type,
                            zone_id=zone_id,
                            bitmap_data=bitmap_data,
                            revision=revision,
                            created_at=now,
                            updated_at=now,
                        )
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
                    zone_id=zone_id,
                    bitmap_data=bitmap_data,
                    revision=revision,
                )

            # Step 5: Update L1 in-memory cache
            with self._lock:
                self._evict_if_needed()
                self._cache[key] = (bitmap, revision, time.time())
                # Also update the zone-agnostic key used by check_access()
                if zone_id:
                    compat_key = CacheKey(subject_type, subject_id, permission, resource_type)
                    if compat_key in self._cache:
                        self._cache[compat_key] = (bitmap, revision, time.time())

            logger.info(
                "[TIGER] Write-through: %s:%s granted %s "
                "on %s:%s (int_id=%d, "
                "bitmap now has %d resources)",
                subject_type,
                subject_id,
                permission,
                resource_type,
                resource_id,
                resource_int_id,
                len(bitmap),
            )
            return True

        except Exception as e:
            logger.error("[TIGER] Write-through failed for %s: %s", key, e)
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
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        try:
            with self._engine.begin() as conn:
                # Step 1: Get resource int ID (don't create if doesn't exist)
                # Note: resource key excludes zone - paths are globally unique
                resource_key = (resource_type, resource_id)
                with self._lock:
                    resource_int_id = self._resource_map._uuid_to_int.get(resource_key)

                if resource_int_id is None:
                    # Try to get from DB (no zone filter)
                    trm_stmt = select(TRM.resource_int_id).where(
                        TRM.resource_type == resource_type,
                        TRM.resource_id == resource_id,
                    )
                    row = conn.execute(trm_stmt).fetchone()
                    if row:
                        resource_int_id = int(row.resource_int_id)
                    else:
                        # Resource not in map - nothing to revoke
                        logger.debug(
                            "[TIGER] Revoke: Resource %s not in map, nothing to do",
                            resource_id,
                        )
                        return True

                # Step 2: Load existing bitmap from DB
                # IMPORTANT: skip_l2=True to read from database directly for atomic operation
                existing_bitmap = self._load_from_db(key, conn, skip_l2=True)

                if existing_bitmap is None:
                    # No bitmap exists - nothing to revoke
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("[TIGER] Revoke: No bitmap for %s, nothing to do", subject_id)
                    return True

                if resource_int_id not in existing_bitmap:
                    # Resource not in bitmap - nothing to revoke
                    logger.debug(
                        "[TIGER] Revoke: Resource %s not in bitmap for %s",
                        resource_id,
                        subject_id,
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
                now = datetime.now(UTC)

                if self._is_postgresql:
                    pg_stmt = pg_insert(TC).values(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type=resource_type,
                        zone_id=zone_id,
                        bitmap_data=bitmap_data,
                        revision=revision,
                        created_at=now,
                        updated_at=now,
                    )
                    pg_stmt = pg_stmt.on_conflict_do_update(
                        constraint="uq_tiger_cache",
                        set_={
                            "bitmap_data": pg_stmt.excluded.bitmap_data,
                            "revision": pg_stmt.excluded.revision,
                            "updated_at": now,
                        },
                    )
                    conn.execute(pg_stmt)
                else:
                    conn.execute(
                        insert(TC)
                        .prefix_with("OR REPLACE")
                        .values(
                            subject_type=subject_type,
                            subject_id=subject_id,
                            permission=permission,
                            resource_type=resource_type,
                            zone_id=zone_id,
                            bitmap_data=bitmap_data,
                            revision=revision,
                            created_at=now,
                            updated_at=now,
                        )
                    )

            # Step 5: Update L2 cache (Dragonfly) for cross-instance consistency
            if self._dragonfly:
                self._run_dragonfly_op(
                    operation="set",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    zone_id=zone_id,
                    bitmap_data=bitmap_data,
                    revision=revision,
                )

            # Step 6: Update L1 in-memory cache
            with self._lock:
                self._evict_if_needed()
                self._cache[key] = (bitmap, revision, time.time())
                # Also update the zone-agnostic key used by check_access(),
                # which creates CacheKey without zone_id (defaults to "").
                # Without this, a stale entry from _load_from_db() survives the revoke.
                if zone_id:
                    compat_key = CacheKey(subject_type, subject_id, permission, resource_type)
                    if compat_key in self._cache:
                        self._cache[compat_key] = (bitmap, revision, time.time())

            logger.info(
                "[TIGER] Write-through revoke: %s:%s revoked %s "
                "on %s:%s (bitmap now has %d resources)",
                subject_type,
                subject_id,
                permission,
                resource_type,
                resource_id,
                len(bitmap),
            )
            return True

        except Exception as e:
            logger.error("[TIGER] Write-through revoke failed for %s: %s", key, e)
            return False

    def remove_from_bitmap(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        zone_id: str,
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
            zone_id: Zone ID for cache partitioning
            resource_int_id: Integer ID of the resource to remove

        Returns:
            True if removed successfully, False if not in cache

        Note:
            This is a write-through operation. For security, revocations
            should also invalidate L1 cache entries.
        """
        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

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
                    "[TIGER] Removed resource %d from bitmap for %s (now %d resources)",
                    resource_int_id,
                    key,
                    len(bitmap),
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
        zone_id: str,
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
            zone_id: Zone ID for cache partitioning
            resource_int_ids: Set of integer resource IDs to add

        Returns:
            Number of resources actually added (excludes already present)
        """
        if not resource_int_ids:
            return 0

        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

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
                    "[TIGER] Bulk added %d resources to bitmap for %s (now %d resources)",
                    added,
                    key,
                    len(bitmap),
                )
            return added

    def persist_bitmap_bulk(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        resource_type: str,
        resource_int_ids: set[int],
        zone_id: str = ROOT_ZONE_ID,
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
            zone_id: Zone ID for cache partitioning

        Returns:
            True if persisted successfully, False on error
        """
        if not resource_int_ids:
            return True

        key = CacheKey(subject_type, subject_id, permission, resource_type, zone_id)

        try:
            # Get current bitmap from memory (may have more entries than resource_int_ids)
            with self._lock:
                if key in self._cache:
                    bitmap, revision, _ = self._cache[key]
                else:
                    bitmap = RoaringBitmap(resource_int_ids)
                    revision = 0

            bitmap_data = bitmap.serialize()
            now = datetime.now(UTC)

            # Note: zone_id still included in INSERT for backward compatibility
            if self._is_postgresql:
                pg_stmt = pg_insert(TC).values(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    zone_id=zone_id,
                    bitmap_data=bitmap_data,
                    revision=revision,
                    created_at=now,
                    updated_at=now,
                )
                pg_stmt = pg_stmt.on_conflict_do_update(
                    constraint="uq_tiger_cache",
                    set_={
                        "bitmap_data": pg_stmt.excluded.bitmap_data,
                        "revision": pg_stmt.excluded.revision,
                        "updated_at": now,
                    },
                )
                upsert_stmt: Any = pg_stmt
            else:
                upsert_stmt = (
                    insert(TC)
                    .prefix_with("OR REPLACE")
                    .values(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        resource_type=resource_type,
                        zone_id=zone_id,
                        bitmap_data=bitmap_data,
                        revision=revision,
                        created_at=now,
                        updated_at=now,
                    )
                )

            with self._engine.begin() as conn:
                conn.execute(upsert_stmt)

            # L2: Also populate Dragonfly cache (write-through)
            if self._dragonfly:
                self._run_dragonfly_op(
                    operation="set",
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    resource_type=resource_type,
                    zone_id=zone_id,
                    bitmap_data=bytes(bitmap_data),
                    revision=revision,
                )
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("[TIGER] L2 Dragonfly write (persist_bulk) for %s", key)

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "[TIGER] Persisted bulk bitmap for %s (%d resources)", key, len(bitmap)
                )
            return True

        except Exception as e:
            logger.error("[TIGER] persist_bitmap_bulk failed for %s: %s", key, e)
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
        # Normalize directory path (ensure trailing slash)
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        now = datetime.now(UTC)

        try:
            if self._is_postgresql:
                pg_stmt = pg_insert(TDG).values(
                    subject_type=subject_type,
                    subject_id=subject_id,
                    permission=permission,
                    directory_path=directory_path,
                    zone_id=zone_id,
                    grant_revision=grant_revision,
                    include_future_files=include_future_files,
                    expansion_status="pending",
                    expanded_count=0,
                    created_at=now,
                    updated_at=now,
                )
                upsert_stmt: Any = pg_stmt.on_conflict_do_update(
                    constraint="uq_tiger_directory_grants",
                    set_={
                        "grant_revision": pg_stmt.excluded.grant_revision,
                        "include_future_files": pg_stmt.excluded.include_future_files,
                        "updated_at": now,
                    },
                ).returning(TDG.grant_id)

                with self._engine.begin() as conn:
                    result = conn.execute(upsert_stmt)
                    row = result.fetchone()
                    return int(row.grant_id) if row else None
            else:
                sqlite_stmt = (
                    insert(TDG)
                    .prefix_with("OR REPLACE")
                    .values(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission=permission,
                        directory_path=directory_path,
                        zone_id=zone_id,
                        grant_revision=grant_revision,
                        include_future_files=include_future_files,
                        expansion_status="pending",
                        expanded_count=0,
                        created_at=now,
                        updated_at=now,
                    )
                )
                with self._engine.begin() as conn:
                    conn.execute(sqlite_stmt)
                return None

        except Exception as e:
            logger.error("[TIGER] record_directory_grant failed: %s", e)
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
        # Generate all ancestor paths
        ancestors = self._get_ancestor_paths(path)
        if not ancestors:
            return []

        try:
            # Dialect-agnostic: use .in_() which works for both PG and SQLite
            stmt = select(
                TDG.grant_id,
                TDG.subject_type,
                TDG.subject_id,
                TDG.permission,
                TDG.directory_path,
                TDG.grant_revision,
                TDG.include_future_files,
            ).where(
                TDG.zone_id == zone_id,
                TDG.directory_path.in_(ancestors),
                TDG.expansion_status == "completed",
            )

            grants = []
            with self._engine.connect() as conn:
                result = conn.execute(stmt)
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
            logger.error("[TIGER] get_directory_grants_for_path failed: %s", e)
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
                    "[TIGER] Expanded batch %d: %d files, total %d/%d",
                    i // batch_size + 1,
                    len(valid_int_ids),
                    total_expanded,
                    len(descendants),
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
                "[TIGER] Directory grant expansion completed: %s -> %d files for %s:%s",
                directory_path,
                total_expanded,
                subject_type,
                subject_id,
            )

            return (total_expanded, True)

        except Exception as e:
            logger.error("[TIGER] expand_directory_grant failed: %s", e)
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
        now = datetime.now(UTC)
        values_dict: dict[str, Any] = {"updated_at": now}

        if status is not None:
            values_dict["expansion_status"] = status
            if status == "completed":
                values_dict["completed_at"] = now

        if expanded_count is not None:
            values_dict["expanded_count"] = expanded_count

        if total_count is not None:
            values_dict["total_count"] = total_count

        if error_message is not None:
            values_dict["error_message"] = error_message

        stmt = (
            update(TDG)
            .where(
                TDG.subject_type == subject_type,
                TDG.subject_id == subject_id,
                TDG.permission == permission,
                TDG.directory_path == directory_path,
                TDG.zone_id == zone_id,
            )
            .values(**values_dict)
        )

        try:
            with self._engine.begin() as conn:
                conn.execute(stmt)
        except Exception as e:
            logger.error("[TIGER] _update_grant_status failed: %s", e)

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
        # Normalize directory path
        if not directory_path.endswith("/"):
            directory_path = directory_path + "/"

        try:
            stmt = delete(TDG).where(
                TDG.subject_type == subject_type,
                TDG.subject_id == subject_id,
                TDG.permission == permission,
                TDG.directory_path == directory_path,
                TDG.zone_id == zone_id,
            )

            with self._engine.begin() as conn:
                conn.execute(stmt)

            logger.info(
                "[TIGER] Removed directory grant: %s for %s:%s (%s)",
                directory_path,
                subject_type,
                subject_id,
                permission,
            )
            return True

        except Exception as e:
            logger.error("[TIGER] remove_directory_grant failed: %s", e)
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
            logger.error("[TIGER] Failed to get int_id for new file: %s", file_path)
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
                    "[TIGER] Added new file %s to grant %s:%s (%s)",
                    file_path,
                    grant["subject_type"],
                    grant["subject_id"],
                    grant["permission"],
                )

            except Exception as e:
                logger.error("[TIGER] Failed to add file to grant: %s", e)

        if added_count > 0:
            logger.info("[TIGER] New file %s added to %d ancestor grants", file_path, added_count)

            # Issue #1147: Increment zone revision when bitmap changes
            # This enables revision-based consistency checks in list() to detect
            # concurrent writes and avoid returning stale results
            try:
                if self._version_store is not None:
                    self._version_store.increment_version(zone_id)
                    logger.debug(
                        "[TIGER] Incremented zone revision for %s after adding file to grants",
                        zone_id,
                    )
            except Exception as e:
                logger.warning("[TIGER] Failed to increment zone revision: %s", e)

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
        stmt = (
            select(
                TC.subject_type,
                TC.subject_id,
                TC.permission,
                TC.resource_type,
                TC.bitmap_data,
                TC.revision,
            )
            .order_by(TC.updated_at.desc())
            .limit(limit)
        )

        loaded = 0
        try:
            with self._engine.connect() as conn:
                result = conn.execute(stmt)

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

            logger.info("[TIGER] Warmed cache with %d entries from database", loaded)
            return loaded

        except Exception as e:
            logger.error("[TIGER] warm_from_db failed: %s", e)
            return loaded
