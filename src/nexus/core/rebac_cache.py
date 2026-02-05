"""In-memory caching layer for ReBAC permission checks.

This module provides a high-performance L1 cache for permission checks,
reducing latency from ~5ms (database) to <1ms (memory).

Architecture:
- L1 Cache (in-memory): This module - <1ms lookup, 50k entries (Issue #1077)
- L2 Cache (database): rebac_check_cache table - 5-10ms lookup
- L3 Compute: Graph traversal - 50-500ms

Revision Quantization (Issue #909):
- Cache keys include a revision bucket based on write operations
- Multiple instances share cache entries within the same revision window
- Replaces broken time-bucket approach from Issue #842
- Based on SpiceDB/Google Zanzibar quantization approach
- See: https://authzed.com/blog/hotspot-caching-in-google-zanzibar-and-spicedb

Targeted Invalidation (Issue #1077):
- Secondary indexes for O(1) path-based and subject-based invalidation
- Avoids O(n) full cache scans for invalidation
- Configurable via NEXUS_CACHE_INVALIDATION_MODE

Tiered TTL (Issue #1077):
- Different TTLs based on permission stability
- Owner: 1 hour (rarely changes)
- Editor/Viewer: 10 minutes
- Inherited: 5 minutes (depends on parent changes)
- Denial: 60 seconds (security critical)
"""

import logging
import math
import random
import threading
import time
from collections.abc import Callable
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)


class ReBACPermissionCache:
    """
    Thread-safe in-memory L1 cache for ReBAC permission checks.

    Provides fast permission check caching with:
    - LRU+TTL eviction policy
    - Thread-safe operations
    - Metrics tracking (hit rate, latency)
    - Precise invalidation by subject/object
    - Write frequency tracking for adaptive TTL

    Example:
        >>> cache = ReBACPermissionCache(max_size=10000, ttl_seconds=300)
        >>> # Check cache
        >>> result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        >>> if result is None:
        >>>     # Cache miss - compute permission
        >>>     result = compute_permission(...)
        >>>     cache.set("agent", "alice", "read", "file", "/doc.txt", result)
    """

    def __init__(
        self,
        max_size: int = 50000,  # Issue #1077: increased from 10k to 50k
        ttl_seconds: int = 300,
        denial_ttl_seconds: int = 60,
        enable_metrics: bool = True,
        enable_adaptive_ttl: bool = False,
        quantization_interval: int = 0,  # DEPRECATED: Use revision_quantization_window
        revision_quantization_window: int = 10,
        enable_revision_quantization: bool = True,
        ttl_jitter_percent: float = 0.2,
        refresh_ahead_factor: float = 0.7,
        xfetch_beta: float = 1.0,
        # Issue #1077: Tiered TTL configuration
        tiered_ttl_config: dict[str, int] | None = None,
        # Issue #1077: Invalidation mode ("targeted" or "zone_wide")
        invalidation_mode: str = "targeted",
    ):
        """
        Initialize ReBAC permission cache.

        Args:
            max_size: Maximum number of entries (default: 50k, Issue #1077)
            ttl_seconds: Time-to-live for grant (True) cache entries (default: 300s)
            denial_ttl_seconds: Time-to-live for denial (False) cache entries (default: 60s)
                Shorter TTL for denials ensures revoked access is reflected quickly (Issue #877)
            enable_metrics: Track hit rates and latency (default: True)
            enable_adaptive_ttl: Adjust TTL based on write frequency (default: False)
            quantization_interval: DEPRECATED - was broken (Issue #909). Ignored.
            revision_quantization_window: Number of revisions per quantization bucket
                (default: 10). Cache keys remain stable within a revision window.
            enable_revision_quantization: Enable revision-based cache keys (default: True)
            ttl_jitter_percent: Jitter percentage for TTL (default: 0.2 = ±20%)
                Prevents thundering herd by staggering cache expiry (Issue #932)
            refresh_ahead_factor: Refresh cache at this fraction of TTL (default: 0.7)
                Triggers background refresh before expiry (Issue #932)
            xfetch_beta: XFetch algorithm aggressiveness parameter (default: 1.0)
                Controls probabilistic early expiration (Issue #718):
                - beta > 1.0: More aggressive (refresh earlier)
                - beta < 1.0: Less aggressive (refresh later)
                - beta = 1.0: Mathematically optimal (VLDB 2015 paper)
            tiered_ttl_config: TTL configuration by relation type (Issue #1077)
                Maps relation names to TTL in seconds. Example:
                {"owner": 3600, "editor": 600, "viewer": 600, "inherited": 300}
                If not provided, defaults to sensible values.
            invalidation_mode: Invalidation strategy (Issue #1077)
                - "targeted": Use secondary indexes for O(1) invalidation (default)
                - "zone_wide": Legacy O(n) full cache scan
        """
        # Deprecation warning for old parameter
        if quantization_interval > 0:
            import warnings

            warnings.warn(
                "quantization_interval is deprecated and was broken (Issue #909). "
                "Use revision_quantization_window for revision-based quantization.",
                DeprecationWarning,
                stacklevel=2,
            )

        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._denial_ttl_seconds = denial_ttl_seconds
        self._enable_metrics = enable_metrics
        self._enable_adaptive_ttl = enable_adaptive_ttl
        self._revision_quantization_window = revision_quantization_window
        self._enable_revision_quantization = enable_revision_quantization
        self._lock = threading.RLock()

        # Revision-based quantization (Issue #909)
        # Callback to fetch current revision for a zone
        self._revision_fetcher: Callable[[str], int] | None = None
        # Local cache for revisions to reduce DB queries (zone -> (revision, timestamp))
        self._revision_cache: dict[str, tuple[int, float]] = {}
        self._revision_cache_ttl = 1.0  # Refresh revision from DB every 1 second

        # Split caches for grants and denials (Issue #877)
        # Denials use shorter TTL for security - revoked access should be reflected quickly
        # Key format: "subject_type:subject_id:permission:object_type:object_id:zone_id:r{revision_bucket}"
        grant_cache_size = max_size // 2  # Split capacity between grant and denial caches
        denial_cache_size = max_size - grant_cache_size
        self._grant_cache: TTLCache[str, bool] = TTLCache(maxsize=grant_cache_size, ttl=ttl_seconds)
        self._denial_cache: TTLCache[str, bool] = TTLCache(
            maxsize=denial_cache_size, ttl=denial_ttl_seconds
        )

        # Metrics tracking
        self._hits = 0
        self._misses = 0
        self._grant_hits = 0  # Issue #877: Track grant cache hits separately
        self._denial_hits = 0  # Issue #877: Track denial cache hits separately
        self._sets = 0
        self._grant_sets = 0  # Issue #877: Track grant cache sets
        self._denial_sets = 0  # Issue #877: Track denial cache sets
        self._invalidations = 0
        self._total_lookup_time_ms = 0.0  # Total time spent on lookups
        self._lookup_count = 0  # Number of lookups

        # Write frequency tracking for adaptive TTL
        # Maps object path -> (write_count, last_reset_time)
        self._write_frequency: dict[str, tuple[int, float]] = {}
        self._write_frequency_window = 300.0  # 5-minute window

        # Stampede prevention (Issue #878)
        # Prevents thundering herd when cache entries expire
        # Maps key -> Event that signals when computation is complete
        self._computing: dict[str, threading.Event] = {}
        self._stampede_waits = 0  # Number of requests that waited
        self._stampede_timeouts = 0  # Number of waits that timed out
        self._stampede_timeout_seconds = 5.0  # Max time to wait for computation

        # TTL jitter and refresh-ahead (Issue #932)
        # Prevents thundering herd by staggering cache expiry
        self._ttl_jitter_percent = ttl_jitter_percent
        self._refresh_ahead_factor = refresh_ahead_factor
        # Track entry metadata for jitter, refresh-ahead, and XFetch
        # Maps key -> (created_at, jittered_ttl, delta, revision)
        # delta is the recomputation time in seconds (Issue #718)
        # revision is the zone revision at cache time (Issue #1081)
        self._entry_metadata: dict[str, tuple[float, float, float, int]] = {}
        # Track keys currently being refreshed in background
        self._refresh_in_progress: set[str] = set()

        # XFetch probabilistic early expiration (Issue #718)
        # Based on VLDB 2015 paper: "Optimal Probabilistic Cache Stampede Prevention"
        # https://www.vldb.org/pvldb/vol8/p886-vattani.pdf
        self._xfetch_beta = xfetch_beta
        self._xfetch_early_refreshes = 0  # Count of XFetch-triggered refreshes
        self._xfetch_computed_refreshes = 0  # Count of refreshes that would have been stampedes

        # Issue #1077: Tiered TTL configuration
        # Different TTLs based on permission stability
        self._tiered_ttl_config = tiered_ttl_config or {
            "owner": 3600,  # 1 hour - rarely changes
            "direct_owner": 3600,
            "admin": 3600,
            "editor": 600,  # 10 minutes
            "write": 600,
            "contributor": 600,
            "can_write": 600,
            "viewer": 600,  # 10 minutes
            "read": 600,
            "can_read": 600,
            "reader": 600,
            "inherited": 300,  # 5 minutes - depends on parent
            "denial": 60,  # 1 minute - security critical
        }

        # Issue #1077: Invalidation mode
        self._invalidation_mode = invalidation_mode

        # Issue #1077: Secondary indexes for O(1) targeted invalidation
        # Instead of scanning all cache keys, we maintain indexes
        # _subject_index: Maps (zone_id, subject_type, subject_id) -> set of cache keys
        # _object_index: Maps (zone_id, object_type, object_id) -> set of cache keys
        # _path_prefix_index: Maps (zone_id, object_type, path_prefix) -> set of cache keys
        self._subject_index: dict[tuple[str, str, str], set[str]] = {}
        self._object_index: dict[tuple[str, str, str], set[str]] = {}
        self._path_prefix_index: dict[tuple[str, str, str], set[str]] = {}

        # Metrics for targeted invalidation
        self._targeted_invalidations = 0
        self._index_lookups = 0

    def _get_jittered_ttl(self, base_ttl: float) -> float:
        """Add random jitter to TTL to prevent thundering herd.

        Args:
            base_ttl: Base TTL in seconds

        Returns:
            Jittered TTL with random offset of ±jitter_percent
        """
        if self._ttl_jitter_percent <= 0:
            return base_ttl
        jitter = base_ttl * self._ttl_jitter_percent
        return base_ttl + random.uniform(-jitter, jitter)

    def _get_ttl_for_relation(self, relation: str, is_denial: bool = False) -> int:
        """Get TTL for a specific relation type (Issue #1077).

        Args:
            relation: The relation type (e.g., "owner", "editor", "viewer")
            is_denial: Whether this is a denial (False permission result)

        Returns:
            TTL in seconds based on relation stability
        """
        if is_denial:
            return self._tiered_ttl_config.get("denial", self._denial_ttl_seconds)

        relation_lower = relation.lower() if relation else "viewer"
        ttl = self._tiered_ttl_config.get(relation_lower)
        if ttl is not None:
            return ttl

        # Default to base TTL
        return self._ttl_seconds

    def _add_to_indexes(
        self,
        key: str,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> None:
        """Add a cache key to secondary indexes for O(1) invalidation (Issue #1077).

        This method must be called under the lock.

        Args:
            key: The cache key to index
            subject_type: Type of subject
            subject_id: Subject identifier
            object_type: Type of object
            object_id: Object identifier (path)
            zone_id: Zone ID
        """
        if self._invalidation_mode != "targeted":
            return

        # Subject index
        subject_key = (zone_id, subject_type, subject_id)
        if subject_key not in self._subject_index:
            self._subject_index[subject_key] = set()
        self._subject_index[subject_key].add(key)

        # Object index
        object_key = (zone_id, object_type, object_id)
        if object_key not in self._object_index:
            self._object_index[object_key] = set()
        self._object_index[object_key].add(key)

        # Path prefix index - index all path prefixes for directory-based invalidation
        # e.g., /workspace/project/file.py -> index at /workspace, /workspace/project
        # Only index actual paths (starting with /) to avoid infinite loops with non-path IDs
        if object_type in ("file", "memory", "resource") and object_id.startswith("/"):
            path = object_id
            while path and path != "/":
                parent = path.rsplit("/", 1)[0] or "/"
                prefix_key = (zone_id, object_type, parent)
                if prefix_key not in self._path_prefix_index:
                    self._path_prefix_index[prefix_key] = set()
                self._path_prefix_index[prefix_key].add(key)
                if parent == "/":
                    break
                path = parent

    def _remove_from_indexes(self, key: str) -> None:
        """Remove a cache key from all secondary indexes (Issue #1077).

        This method must be called under the lock.

        Args:
            key: The cache key to remove from indexes
        """
        if self._invalidation_mode != "targeted":
            return

        # Parse the key to get components
        parsed = self._parse_key(key)
        if not parsed:
            return

        subject_type, subject_id, _, object_type, object_id, zone_id = parsed

        # Remove from subject index
        subject_key = (zone_id, subject_type, subject_id)
        if subject_key in self._subject_index:
            self._subject_index[subject_key].discard(key)
            if not self._subject_index[subject_key]:
                del self._subject_index[subject_key]

        # Remove from object index
        object_key = (zone_id, object_type, object_id)
        if object_key in self._object_index:
            self._object_index[object_key].discard(key)
            if not self._object_index[object_key]:
                del self._object_index[object_key]

        # Remove from path prefix index (only for actual paths starting with /)
        if object_type in ("file", "memory", "resource") and object_id.startswith("/"):
            path = object_id
            while path and path != "/":
                parent = path.rsplit("/", 1)[0] or "/"
                prefix_key = (zone_id, object_type, parent)
                if prefix_key in self._path_prefix_index:
                    self._path_prefix_index[prefix_key].discard(key)
                    if not self._path_prefix_index[prefix_key]:
                        del self._path_prefix_index[prefix_key]
                if parent == "/":
                    break
                path = parent

    def _get_keys_for_subject(self, zone_id: str, subject_type: str, subject_id: str) -> set[str]:
        """Get all cache keys for a subject using secondary index (Issue #1077).

        Args:
            zone_id: Zone ID
            subject_type: Type of subject
            subject_id: Subject identifier

        Returns:
            Set of cache keys (empty if no matches or using zone_wide mode)
        """
        if self._invalidation_mode != "targeted":
            return set()

        if self._enable_metrics:
            self._index_lookups += 1

        subject_key = (zone_id, subject_type, subject_id)
        return self._subject_index.get(subject_key, set()).copy()

    def _get_keys_for_object(self, zone_id: str, object_type: str, object_id: str) -> set[str]:
        """Get all cache keys for an object using secondary index (Issue #1077).

        Args:
            zone_id: Zone ID
            object_type: Type of object
            object_id: Object identifier

        Returns:
            Set of cache keys (empty if no matches or using zone_wide mode)
        """
        if self._invalidation_mode != "targeted":
            return set()

        if self._enable_metrics:
            self._index_lookups += 1

        object_key = (zone_id, object_type, object_id)
        return self._object_index.get(object_key, set()).copy()

    def _get_keys_for_path_prefix(
        self, zone_id: str, object_type: str, path_prefix: str
    ) -> set[str]:
        """Get all cache keys under a path prefix using secondary index (Issue #1077).

        This is the key optimization: instead of O(n) scan, we get O(affected) lookup.

        Args:
            zone_id: Zone ID
            object_type: Type of object
            path_prefix: Path prefix (e.g., "/workspace/")

        Returns:
            Set of cache keys under this prefix (empty if no matches or using zone_wide mode)
        """
        if self._invalidation_mode != "targeted":
            return set()

        if self._enable_metrics:
            self._index_lookups += 1

        # Normalize prefix
        normalized_prefix = path_prefix.rstrip("/") or "/"

        # Get direct matches from the prefix index
        prefix_key = (zone_id, object_type, normalized_prefix)
        keys = self._path_prefix_index.get(prefix_key, set()).copy()

        # Also include the exact object if it exists
        object_key = (zone_id, object_type, normalized_prefix)
        if object_key in self._object_index:
            keys |= self._object_index[object_key]

        return keys

    def set_revision_fetcher(self, fetcher: Callable[[str], int]) -> None:
        """Set callback to fetch current revision for a zone.

        The revision fetcher is used for revision-based cache key quantization (Issue #909).
        It should return the current write revision for the given zone.

        Args:
            fetcher: Function that takes zone_id and returns current revision number
        """
        self._revision_fetcher = fetcher

    def _get_revision_bucket(self, zone_id: str | None) -> int:
        """Get quantized revision bucket for cache key.

        Uses cached revision with short TTL to reduce DB queries.
        Falls back to 0 if revision fetcher not set (graceful degradation).

        Args:
            zone_id: Zone ID (defaults to "default")

        Returns:
            Quantized revision bucket number
        """
        if not self._enable_revision_quantization:
            return 0

        effective_zone = zone_id or "default"
        current_time = time.time()

        # Check local revision cache
        if effective_zone in self._revision_cache:
            cached_rev, cached_at = self._revision_cache[effective_zone]
            if current_time - cached_at < self._revision_cache_ttl:
                return cached_rev // self._revision_quantization_window

        # Fetch from DB via callback
        if self._revision_fetcher:
            try:
                revision = self._revision_fetcher(effective_zone)
                self._revision_cache[effective_zone] = (revision, current_time)
                return revision // self._revision_quantization_window
            except Exception as e:
                logger.warning(f"Failed to fetch revision for {effective_zone}: {e}")

        return 0  # Fallback: all entries share same bucket (still functional)

    def _get_current_revision(self, zone_id: str | None) -> int:
        """Get current revision for a zone (Issue #1081).

        Used for tracking revision at cache time for AT_LEAST_AS_FRESH consistency.

        Args:
            zone_id: Zone ID (defaults to "default")

        Returns:
            Current revision number, or 0 if unavailable
        """
        effective_zone = zone_id or "default"
        current_time = time.time()

        # Check local revision cache first
        if effective_zone in self._revision_cache:
            cached_rev, cached_at = self._revision_cache[effective_zone]
            if current_time - cached_at < self._revision_cache_ttl:
                return cached_rev

        # Fetch from DB via callback
        if self._revision_fetcher:
            try:
                revision = self._revision_fetcher(effective_zone)
                self._revision_cache[effective_zone] = (revision, current_time)
                return revision
            except Exception as e:
                logger.warning(f"Failed to fetch revision for {effective_zone}: {e}")

        return 0  # Fallback

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
    ) -> str:
        """Create cache key from permission check parameters.

        Args:
            subject_type: Type of subject (e.g., "agent", "user", "group")
            subject_id: Subject identifier
            permission: Permission to check (e.g., "read", "write")
            object_type: Type of object (e.g., "file", "memory")
            object_id: Object identifier (e.g., path)
            zone_id: Optional zone ID for multi-zone isolation

        Returns:
            Cache key string with revision bucket for distributed cache sharing
        """
        zone_part = zone_id if zone_id else "default"
        revision_bucket = self._get_revision_bucket(zone_id)
        return f"{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}:{zone_part}:r{revision_bucket}"

    def _parse_key(self, key: str) -> tuple[str, str, str, str, str, str] | None:
        """Parse a cache key into components (excluding revision bucket).

        Returns:
            Tuple of (subject_type, subject_id, permission, object_type, object_id, zone_id)
            or None if key format is invalid.
        """
        parts = key.split(":")
        if len(parts) < 7:
            return None
        subject_type = parts[0]
        subject_id = parts[1]
        permission = parts[2]
        object_type = parts[3]
        zone_id = parts[-2]
        object_id = ":".join(parts[4:-2])
        return (subject_type, subject_id, permission, object_type, object_id, zone_id)

    def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
    ) -> bool | None:
        """
        Get cached permission check result.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID

        Returns:
            True/False if cached, None if not cached or expired
        """
        start_time = time.perf_counter()
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)

        with self._lock:
            # Check grant cache first (Issue #877)
            result = self._grant_cache.get(key)
            is_grant_hit = result is not None

            # If not in grant cache, check denial cache
            if result is None:
                result = self._denial_cache.get(key)

            # Track metrics
            if self._enable_metrics:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._total_lookup_time_ms += elapsed_ms
                self._lookup_count += 1

                if result is not None:
                    self._hits += 1
                    if is_grant_hit:
                        self._grant_hits += 1
                    else:
                        self._denial_hits += 1
                else:
                    self._misses += 1

            return result

    def get_with_revision_check(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
        min_revision: int | None = None,
    ) -> tuple[bool | None, int]:
        """Get cached result with revision check for AT_LEAST_AS_FRESH consistency (Issue #1081).

        This method implements the SpiceDB/Zanzibar at_least_as_fresh consistency mode.
        It returns cached results only if they were cached at a revision >= min_revision.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID
            min_revision: Minimum acceptable revision (for AT_LEAST_AS_FRESH mode)

        Returns:
            Tuple of (result, cached_revision):
            - result: True/False if cached and fresh enough, None if cache miss or stale
            - cached_revision: The revision at which this entry was cached (0 if not found)

        Example:
            # After a write, check with read-your-writes guarantee
            result, revision = cache.get_with_revision_check(
                "user", "alice", "read", "file", "/doc.txt",
                zone_id="default",
                min_revision=write_result.revision
            )
            if result is None:
                # Cache miss or stale - need fresh computation
                ...
        """
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)

        with self._lock:
            # Get metadata to check revision
            metadata = self._entry_metadata.get(key)
            if metadata is None:
                # No metadata = no cached entry
                if self._enable_metrics:
                    self._misses += 1
                return None, 0

            _created_at, _jittered_ttl, _delta, cached_revision = metadata

            # Check if cached revision is fresh enough
            if min_revision is not None and cached_revision < min_revision:
                # Cached data is too stale for this request
                if self._enable_metrics:
                    self._misses += 1  # Count as miss since we can't use it
                return None, cached_revision

            # Revision is acceptable, get the actual cached value
            result = self._grant_cache.get(key)
            is_grant_hit = result is not None

            if result is None:
                result = self._denial_cache.get(key)

            # Track metrics
            if self._enable_metrics:
                if result is not None:
                    self._hits += 1
                    if is_grant_hit:
                        self._grant_hits += 1
                    else:
                        self._denial_hits += 1
                else:
                    self._misses += 1

            return result, cached_revision

    def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        zone_id: str | None = None,
        delta: float = 0.0,
        relation: str | None = None,
        is_inherited: bool = False,
    ) -> None:
        """
        Cache permission check result.

        Grants (True) are cached with longer TTL, denials (False) with shorter TTL.
        This ensures revoked access is reflected quickly while maximizing cache benefit
        for allowed operations (Issue #877).

        Issue #1077: Supports tiered TTL based on relation type and maintains
        secondary indexes for O(1) targeted invalidation.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            result: Permission check result (True/False)
            zone_id: Optional zone ID
            delta: Recomputation time in seconds (Issue #718: XFetch algorithm)
                Used for probabilistic early expiration - items that take longer
                to recompute are refreshed earlier.
            relation: Optional relation type for tiered TTL (Issue #1077)
                e.g., "owner", "editor", "viewer". If not provided, uses default TTL.
            is_inherited: Whether this is an inherited permission (Issue #1077)
                Inherited permissions use shorter TTL since they depend on parent.
        """
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)
        zone_part = zone_id if zone_id else "default"

        with self._lock:
            # Issue #1077: Get tiered TTL based on relation type
            # Only use tiered TTL when relation is explicitly provided
            # This preserves backward compatibility with existing code that relies on ttl_seconds
            if result:
                if relation is not None:
                    effective_relation = "inherited" if is_inherited else relation
                    base_ttl = self._get_ttl_for_relation(effective_relation, is_denial=False)
                elif is_inherited:
                    base_ttl = self._get_ttl_for_relation("inherited", is_denial=False)
                else:
                    base_ttl = self._ttl_seconds  # Use default TTL when no relation provided
            else:
                base_ttl = self._denial_ttl_seconds

            # Track entry metadata with jittered TTL for refresh-ahead (Issue #932)
            # and delta for XFetch (Issue #718), plus revision for AT_LEAST_AS_FRESH (Issue #1081)
            jittered_ttl = self._get_jittered_ttl(float(base_ttl))
            # Get current revision for consistency tracking
            current_revision = self._get_current_revision(zone_id)
            self._entry_metadata[key] = (time.time(), jittered_ttl, delta, current_revision)

            # Route to appropriate cache based on result (Issue #877)
            # Grants get longer TTL, denials get shorter TTL for security
            if result:
                self._grant_cache[key] = result
                if self._enable_metrics:
                    self._grant_sets += 1
            else:
                self._denial_cache[key] = result
                if self._enable_metrics:
                    self._denial_sets += 1

            # Issue #1077: Add to secondary indexes for O(1) invalidation
            self._add_to_indexes(key, subject_type, subject_id, object_type, object_id, zone_part)

            if self._enable_metrics:
                self._sets += 1

    # ============================================================
    # Stampede Prevention (Issue #878)
    # ============================================================

    def try_acquire_compute(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        Try to acquire the right to compute a permission check.

        Used for stampede prevention. When multiple requests need the same
        permission check simultaneously, only the first one computes while
        others wait.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID

        Returns:
            Tuple of (should_compute, cache_key):
            - (True, key) if caller should compute and then call release_compute()
            - (False, key) if another request is computing, caller should call wait_for_compute()
        """
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)

        with self._lock:
            # Check if already being computed
            if key in self._computing:
                return (False, key)

            # We're the leader - set up the event for others to wait on
            self._computing[key] = threading.Event()
            return (True, key)

    def wait_for_compute(self, key: str) -> bool | None:
        """
        Wait for another request to finish computing a permission.

        Args:
            key: Cache key returned by try_acquire_compute()

        Returns:
            Cached result after computation completes, or None if timeout/not found
        """
        with self._lock:
            event = self._computing.get(key)
            if event is None:
                # Computation already finished, check cache
                # Parse key to get components for cache lookup
                parsed = self._parse_key(key)
                if parsed:
                    return self.get(
                        parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5]
                    )
                return None

        # Wait outside the lock
        if self._enable_metrics:
            with self._lock:
                self._stampede_waits += 1

        success = event.wait(timeout=self._stampede_timeout_seconds)

        if not success:
            # Timeout - computation took too long
            if self._enable_metrics:
                with self._lock:
                    self._stampede_timeouts += 1
            logger.warning(f"Stampede wait timeout for key: {key[:50]}...")
            return None

        # Computation finished, get result from cache
        with self._lock:
            parsed = self._parse_key(key)
            if parsed:
                return self.get(parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5])
            return None

    def release_compute(
        self,
        key: str,
        result: bool,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
        delta: float = 0.0,
    ) -> None:
        """
        Release compute lock and cache the result.

        Must be called after try_acquire_compute() returns (True, key),
        even if computation failed.

        Args:
            key: Cache key returned by try_acquire_compute()
            result: Computed permission result
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID
            delta: Recomputation time in seconds for XFetch (Issue #718)
        """
        with self._lock:
            # Cache the result first with delta for XFetch
            self.set(
                subject_type,
                subject_id,
                permission,
                object_type,
                object_id,
                result,
                zone_id,
                delta=delta,
            )

            # Signal waiting requests and clean up
            event = self._computing.pop(key, None)
            if event:
                event.set()

    def cancel_compute(self, key: str) -> None:
        """
        Cancel a computation without caching a result.

        Use this if computation failed and you don't want to cache the error.

        Args:
            key: Cache key returned by try_acquire_compute()
        """
        with self._lock:
            event = self._computing.pop(key, None)
            if event:
                event.set()

    # ============================================================
    # XFetch Probabilistic Early Expiration (Issue #718)
    # Based on VLDB 2015 paper: "Optimal Probabilistic Cache Stampede Prevention"
    # https://www.vldb.org/pvldb/vol8/p886-vattani.pdf
    # ============================================================

    def _should_refresh_xfetch(self, key: str, beta: float | None = None) -> bool:
        """
        XFetch probabilistic early expiration check.

        Uses exponential distribution to probabilistically trigger refresh
        before expiration. The probability increases as expiry approaches,
        and items that take longer to recompute (higher delta) are refreshed
        earlier.

        Formula: time() - delta * beta * log(random()) >= expiry

        Args:
            key: Cache key to check
            beta: Optional override for aggressiveness parameter.
                If not provided, uses instance's xfetch_beta.
                - beta > 1.0: More aggressive (refresh earlier)
                - beta < 1.0: Less aggressive (refresh later)
                - beta = 1.0: Mathematically optimal

        Returns:
            True if we should trigger a refresh now
        """
        metadata = self._entry_metadata.get(key)
        if metadata is None:
            return False

        created_at, jittered_ttl, delta, _revision = metadata
        expiry = created_at + jittered_ttl
        current_time = time.time()

        # If already expired, definitely refresh
        if current_time >= expiry:
            return True

        # If delta is 0 or very small, fall back to refresh-ahead threshold
        # This handles cases where delta wasn't tracked
        if delta < 0.001:  # Less than 1ms
            age = current_time - created_at
            refresh_threshold = jittered_ttl * self._refresh_ahead_factor
            return age > refresh_threshold

        # XFetch algorithm: probabilistic early expiration
        # log(random()) is always negative (since 0 < random() < 1)
        # So -delta * beta * log(random()) is always positive
        # As we approach expiry, the probability of triggering increases
        effective_beta = beta if beta is not None else self._xfetch_beta
        random_factor = -delta * effective_beta * math.log(random.random())

        return current_time + random_factor >= expiry

    def should_refresh_xfetch(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
        beta: float | None = None,
    ) -> bool:
        """
        Public XFetch check for a permission cache entry.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID
            beta: Optional override for aggressiveness parameter

        Returns:
            True if we should trigger a refresh now
        """
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)
        with self._lock:
            return self._should_refresh_xfetch(key, beta)

    # ============================================================
    # Refresh-Ahead Pattern (Issue #932) - Now uses XFetch
    # ============================================================

    def get_with_refresh_check(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
    ) -> tuple[bool | None, bool, str]:
        """
        Get cached value and check if refresh is needed.

        Uses _entry_metadata to track entry age with jittered TTL.
        When an entry reaches refresh_ahead_factor of its TTL, it signals
        that a background refresh should be triggered while still returning
        the cached value.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID

        Returns:
            Tuple of (cached_value, needs_refresh, cache_key):
            - cached_value: True/False if cached, None if not cached
            - needs_refresh: True if entry should be refreshed in background
            - cache_key: The cache key for use with refresh methods
        """
        start_time = time.perf_counter()
        key = self._make_key(subject_type, subject_id, permission, object_type, object_id, zone_id)

        with self._lock:
            # Get cached value (existing logic)
            result = self._grant_cache.get(key)
            is_grant_hit = result is not None

            if result is None:
                result = self._denial_cache.get(key)

            # Track metrics
            if self._enable_metrics:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._total_lookup_time_ms += elapsed_ms
                self._lookup_count += 1

                if result is not None:
                    self._hits += 1
                    if is_grant_hit:
                        self._grant_hits += 1
                    else:
                        self._denial_hits += 1
                else:
                    self._misses += 1

            # If no cached result, no refresh needed
            if result is None:
                return None, False, key

            # Check if refresh is needed using XFetch algorithm (Issue #718)
            needs_refresh = (
                self._should_refresh_xfetch(key) and key not in self._refresh_in_progress
            )

            if needs_refresh and self._enable_metrics:
                self._xfetch_early_refreshes += 1

            return result, needs_refresh, key

    def mark_refresh_in_progress(self, key: str) -> bool:
        """
        Mark a key as being refreshed in background.

        Args:
            key: Cache key to mark

        Returns:
            True if successfully marked, False if already in progress
        """
        with self._lock:
            if key in self._refresh_in_progress:
                return False
            self._refresh_in_progress.add(key)
            return True

    def complete_refresh(self, key: str) -> None:
        """
        Mark a background refresh as complete.

        Args:
            key: Cache key that was refreshed
        """
        with self._lock:
            self._refresh_in_progress.discard(key)

    def _invalidate_from_both_caches(self, keys_to_delete: list[str]) -> int:
        """Helper to delete keys from both grant and denial caches."""
        count = 0
        for key in keys_to_delete:
            if key in self._grant_cache:
                del self._grant_cache[key]
                count += 1
            if key in self._denial_cache:
                del self._denial_cache[key]
                count += 1
        return count

    def _collect_matching_keys(
        self,
        match_fn: Callable[[str], bool],
    ) -> list[str]:
        """Collect keys from both caches matching a predicate."""
        keys = []
        for key in list(self._grant_cache.keys()):
            if match_fn(key):
                keys.append(key)
        for key in list(self._denial_cache.keys()):
            if match_fn(key) and key not in keys:
                keys.append(key)
        return keys

    def invalidate_subject(
        self, subject_type: str, subject_id: str, zone_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for a specific subject.

        Used when subject's permissions change (e.g., user added to group).

        Issue #1077: Uses secondary indexes for O(1) lookup when invalidation_mode="targeted".

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            zone_id: Optional zone ID

        Returns:
            Number of entries invalidated
        """
        zone_part = zone_id if zone_id else "default"

        with self._lock:
            # Issue #1077: Use secondary index for O(1) lookup if in targeted mode
            if self._invalidation_mode == "targeted":
                keys_to_delete = list(
                    self._get_keys_for_subject(zone_part, subject_type, subject_id)
                )
                if self._enable_metrics:
                    self._targeted_invalidations += 1
            else:
                # Legacy: O(n) scan through all keys
                def match_fn(key: str) -> bool:
                    parsed = self._parse_key(key)
                    return bool(
                        parsed
                        and parsed[0] == subject_type
                        and parsed[1] == subject_id
                        and parsed[5] == zone_part
                    )

                keys_to_delete = self._collect_matching_keys(match_fn)

            # Remove from indexes before deleting
            for key in keys_to_delete:
                self._remove_from_indexes(key)

            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for subject {subject_type}:{subject_id} "
                f"(mode={self._invalidation_mode})"
            )
            return count

    def invalidate_object(
        self, object_type: str, object_id: str, zone_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for a specific object.

        Used when object's permissions change (e.g., file access granted).

        Issue #1077: Uses secondary indexes for O(1) lookup when invalidation_mode="targeted".

        Args:
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID

        Returns:
            Number of entries invalidated
        """
        zone_part = zone_id if zone_id else "default"

        with self._lock:
            # Issue #1077: Use secondary index for O(1) lookup if in targeted mode
            if self._invalidation_mode == "targeted":
                keys_to_delete = list(self._get_keys_for_object(zone_part, object_type, object_id))
                if self._enable_metrics:
                    self._targeted_invalidations += 1
            else:
                # Legacy: O(n) scan through all keys
                def match_fn(key: str) -> bool:
                    parsed = self._parse_key(key)
                    return bool(
                        parsed
                        and parsed[3] == object_type
                        and parsed[4] == object_id
                        and parsed[5] == zone_part
                    )

                keys_to_delete = self._collect_matching_keys(match_fn)

            # Remove from indexes before deleting
            for key in keys_to_delete:
                self._remove_from_indexes(key)

            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for object {object_type}:{object_id} "
                f"(mode={self._invalidation_mode})"
            )
            return count

    def invalidate_subject_object_pair(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        zone_id: str | None = None,
    ) -> int:
        """
        Invalidate cache entries for a specific subject-object pair.

        Most precise invalidation - only affects permissions between this subject and object.

        Issue #1077: Uses intersection of secondary indexes for O(1) lookup.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            object_type: Type of object
            object_id: Object identifier
            zone_id: Optional zone ID

        Returns:
            Number of entries invalidated
        """
        zone_part = zone_id if zone_id else "default"

        with self._lock:
            # Issue #1077: Use intersection of indexes for precise O(1) lookup
            if self._invalidation_mode == "targeted":
                subject_keys = self._get_keys_for_subject(zone_part, subject_type, subject_id)
                object_keys = self._get_keys_for_object(zone_part, object_type, object_id)
                # Intersection gives us exactly the subject-object pair keys
                keys_to_delete = list(subject_keys & object_keys)
                if self._enable_metrics:
                    self._targeted_invalidations += 1
            else:
                # Legacy: O(n) scan through all keys
                def match_fn(key: str) -> bool:
                    parsed = self._parse_key(key)
                    return bool(
                        parsed
                        and parsed[0] == subject_type
                        and parsed[1] == subject_id
                        and parsed[3] == object_type
                        and parsed[4] == object_id
                        and parsed[5] == zone_part
                    )

                keys_to_delete = self._collect_matching_keys(match_fn)

            # Remove from indexes before deleting
            for key in keys_to_delete:
                self._remove_from_indexes(key)

            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for pair "
                f"{subject_type}:{subject_id} <-> {object_type}:{object_id} "
                f"(mode={self._invalidation_mode})"
            )
            return count

    def invalidate_object_prefix(
        self, object_type: str, object_id_prefix: str, zone_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for objects matching a prefix.

        Used for directory operations (e.g., invalidate all files under /workspace/).

        Issue #1077: Uses path prefix index for O(affected) lookup instead of O(n) scan.
        This is a critical optimization for large caches with deep directory hierarchies.

        Args:
            object_type: Type of object
            object_id_prefix: Object ID prefix (e.g., "/workspace/")
            zone_id: Optional zone ID

        Returns:
            Number of entries invalidated
        """
        zone_part = zone_id if zone_id else "default"

        with self._lock:
            # Issue #1077: Use path prefix index for O(affected) lookup
            if self._invalidation_mode == "targeted":
                keys_to_delete = list(
                    self._get_keys_for_path_prefix(zone_part, object_type, object_id_prefix)
                )
                if self._enable_metrics:
                    self._targeted_invalidations += 1
            else:
                # Legacy: O(n) scan through all keys
                def match_fn(key: str) -> bool:
                    parsed = self._parse_key(key)
                    return bool(
                        parsed
                        and parsed[3] == object_type
                        and parsed[4].startswith(object_id_prefix)
                        and parsed[5] == zone_part
                    )

                keys_to_delete = self._collect_matching_keys(match_fn)

            # Remove from indexes before deleting
            for key in keys_to_delete:
                self._remove_from_indexes(key)

            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for prefix {object_type}:{object_id_prefix} "
                f"(mode={self._invalidation_mode})"
            )
            return count

    def track_write(self, object_id: str) -> None:
        """
        Track a write operation for adaptive TTL calculation.

        Args:
            object_id: Object that was written to
        """
        if not self._enable_adaptive_ttl:
            return

        with self._lock:
            current_time = time.time()

            if object_id in self._write_frequency:
                count, last_reset = self._write_frequency[object_id]

                # Reset counter if outside window
                if current_time - last_reset > self._write_frequency_window:
                    self._write_frequency[object_id] = (1, current_time)
                else:
                    self._write_frequency[object_id] = (count + 1, last_reset)
            else:
                self._write_frequency[object_id] = (1, current_time)

    def _get_adaptive_ttl(self, object_id: str) -> int:
        """
        Calculate adaptive TTL based on write frequency.

        High-write objects get shorter TTL, stable objects get longer TTL.

        Args:
            object_id: Object to calculate TTL for

        Returns:
            TTL in seconds
        """
        if object_id not in self._write_frequency:
            return self._ttl_seconds

        count, last_reset = self._write_frequency[object_id]
        current_time = time.time()

        # If outside window, use default TTL
        if current_time - last_reset > self._write_frequency_window:
            return self._ttl_seconds

        # Calculate writes per minute
        elapsed_minutes = (current_time - last_reset) / 60.0
        writes_per_minute = count / max(elapsed_minutes, 1.0)

        # Adaptive TTL based on write frequency
        if writes_per_minute > 10:  # Very high write rate
            return max(10, self._ttl_seconds // 6)  # 10s minimum
        elif writes_per_minute > 5:  # High write rate
            return max(30, self._ttl_seconds // 3)  # 30s minimum
        elif writes_per_minute > 1:  # Moderate write rate
            return max(60, self._ttl_seconds // 2)  # 60s minimum
        else:  # Low write rate
            return min(300, self._ttl_seconds * 2)  # 5min maximum

    def clear(self) -> None:
        """Clear all cache entries from both grant and denial caches and indexes."""
        with self._lock:
            self._grant_cache.clear()
            self._denial_cache.clear()
            # Issue #1077: Also clear secondary indexes
            self._subject_index.clear()
            self._object_index.clear()
            self._path_prefix_index.clear()
            self._entry_metadata.clear()
            if self._enable_metrics:
                logger.info("L1 cache cleared (grant, denial caches, and indexes)")

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics including hit rate, latency,
            and grant/denial breakdown (Issue #877)
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0
            avg_lookup_time_ms = (
                (self._total_lookup_time_ms / self._lookup_count) if self._lookup_count > 0 else 0.0
            )

            # Calculate grant/denial hit rates (Issue #877)
            grant_hit_rate = (self._grant_hits / self._hits * 100) if self._hits > 0 else 0.0
            denial_hit_rate = (self._denial_hits / self._hits * 100) if self._hits > 0 else 0.0

            return {
                "max_size": self._max_size,
                "current_size": len(self._grant_cache) + len(self._denial_cache),
                "grant_cache_size": len(self._grant_cache),
                "denial_cache_size": len(self._denial_cache),
                "ttl_seconds": self._ttl_seconds,
                "denial_ttl_seconds": self._denial_ttl_seconds,
                "revision_quantization_window": self._revision_quantization_window,
                "enable_revision_quantization": self._enable_revision_quantization,
                "hits": self._hits,
                "grant_hits": self._grant_hits,
                "denial_hits": self._denial_hits,
                "grant_hit_rate_percent": round(grant_hit_rate, 2),
                "denial_hit_rate_percent": round(denial_hit_rate, 2),
                "misses": self._misses,
                "sets": self._sets,
                "grant_sets": self._grant_sets,
                "denial_sets": self._denial_sets,
                "invalidations": self._invalidations,
                "hit_rate_percent": round(hit_rate, 2),
                "total_requests": total_requests,
                "avg_lookup_time_ms": round(avg_lookup_time_ms, 3),
                "enable_metrics": self._enable_metrics,
                "enable_adaptive_ttl": self._enable_adaptive_ttl,
                # Stampede prevention metrics (Issue #878)
                "stampede_waits": self._stampede_waits,
                "stampede_timeouts": self._stampede_timeouts,
                "stampede_active_computes": len(self._computing),
                # XFetch metrics (Issue #718)
                "xfetch_beta": self._xfetch_beta,
                "xfetch_early_refreshes": self._xfetch_early_refreshes,
                # Issue #1077: Tiered TTL and targeted invalidation metrics
                "invalidation_mode": self._invalidation_mode,
                "tiered_ttl_config": self._tiered_ttl_config,
                "targeted_invalidations": self._targeted_invalidations,
                "index_lookups": self._index_lookups,
                "subject_index_size": len(self._subject_index),
                "object_index_size": len(self._object_index),
                "path_prefix_index_size": len(self._path_prefix_index),
            }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._grant_hits = 0
            self._denial_hits = 0
            self._sets = 0
            self._grant_sets = 0
            self._denial_sets = 0
            self._invalidations = 0
            self._total_lookup_time_ms = 0.0
            self._lookup_count = 0
            self._stampede_waits = 0
            self._stampede_timeouts = 0
            # XFetch metrics (Issue #718)
            self._xfetch_early_refreshes = 0
            self._xfetch_computed_refreshes = 0
            # Issue #1077: Targeted invalidation metrics
            self._targeted_invalidations = 0
            self._index_lookups = 0
            logger.info("L1 cache stats reset")
