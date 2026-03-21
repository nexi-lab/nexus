"""Directory Visibility Cache - O(1) directory visibility lookups.

This cache pre-computes and caches directory visibility to eliminate
the O(n) _has_descendant_access() calls that enumerate all descendants.

Inspired by SeaweedFS MetaCache (weed/mount/meta_cache/meta_cache.go)
which caches at directory granularity with subscription-based invalidation.

Performance:
    - Directory visibility check: O(n) -> O(1) cache / O(bitmap) compute
    - /workspace with 10K files: ~2000ms -> ~5ms
    - Database queries per list(): n+1 -> 1-2

Related: Issue #919
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

# Issue #3192: Rust-backed TTLCache for lock-free cache internals
try:
    from cachebox import TTLCache
except ImportError:
    from cachetools import TTLCache  # type: ignore[assignment]

try:
    from fastbloom_rs import BloomFilter
except ImportError:
    BloomFilter = None  # noqa: N816 — optional Rust dependency

if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.tiger.bitmap_cache import TigerCache

logger = logging.getLogger(__name__)


@dataclass
class VisibilityEntry:
    """Cache entry for directory visibility."""

    visible: bool
    computed_at: float
    # Reason for visibility (for debugging)
    reason: str = ""


class DirectoryVisibilityCache:
    """Cache which directories are visible to each user.

    Key: (zone_id, subject_type, subject_id, dir_path)
    Value: (visible: bool, computed_at: float)

    The cache uses Tiger Cache bitmaps to compute visibility without
    enumerating descendants - a key performance optimization.
    """

    def __init__(
        self,
        tiger_cache: "TigerCache | None" = None,
        ttl: int = 300,  # 5 minutes default
        max_entries: int = 10000,
    ):
        """Initialize the directory visibility cache.

        Args:
            tiger_cache: Optional Tiger Cache for bitmap-based computation
            ttl: Time-to-live in seconds for cache entries
            max_entries: Maximum number of entries before eviction
        """
        self._tiger_cache = tiger_cache
        self._ttl = ttl
        self._max_entries = max_entries

        # Issue #3192: Rust-backed TTLCache handles TTL + eviction internally
        self._cache: TTLCache = TTLCache(maxsize=max_entries, ttl=ttl)
        self._lock = threading.RLock()

        # BloomFilter pre-gate for fast negative visibility checks (Issue #3192)
        self._bloom: BloomFilter | None = None
        self._bloom_capacity = max_entries
        self._bloom_fpp = 0.01
        self._bloom_rejects = 0
        if BloomFilter is not None:
            self._bloom = BloomFilter(self._bloom_capacity, self._bloom_fpp)

        # Metrics
        self._hits = 0
        self._misses = 0
        self._bitmap_computes = 0

    def is_visible(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_path: str,
    ) -> bool | None:
        """O(1) cache lookup for directory visibility.

        Args:
            zone_id: Zone ID
            subject_type: Subject type (e.g., "user", "agent")
            subject_id: Subject ID
            dir_path: Directory path to check

        Returns:
            True if visible, False if not visible, None on cache miss
        """
        key = (zone_id, subject_type, subject_id, dir_path)

        with self._lock:
            # BloomFilter pre-gate
            if self._bloom is not None:
                bloom_key = f"{zone_id}:{subject_type}:{subject_id}:{dir_path}"
                if bloom_key not in self._bloom:
                    self._bloom_rejects += 1
                    self._misses += 1
                    return None

            entry = self._cache.get(key)
            if entry is not None:
                # TTLCache handles expiry automatically — if we get a result, it's valid
                self._hits += 1
                logger.debug(
                    f"[DirVisCache] HIT: {subject_type}:{subject_id} -> {dir_path} = {entry.visible} ({entry.reason})"
                )
                return bool(entry.visible)

            self._misses += 1
            return None

    def set_visible(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_path: str,
        visible: bool,
        reason: str = "",
    ) -> None:
        """Set visibility for a directory.

        Args:
            zone_id: Zone ID
            subject_type: Subject type
            subject_id: Subject ID
            dir_path: Directory path
            visible: Whether the directory is visible
            reason: Optional reason for visibility (debugging)
        """
        key = (zone_id, subject_type, subject_id, dir_path)

        with self._lock:
            if self._bloom is not None:
                bloom_key = f"{zone_id}:{subject_type}:{subject_id}:{dir_path}"
                self._bloom.add(bloom_key)

            self._cache[key] = VisibilityEntry(
                visible=visible,
                computed_at=time.time(),
                reason=reason,
            )
            logger.debug(
                f"[DirVisCache] SET: {subject_type}:{subject_id} -> {dir_path} = {visible} ({reason})"
            )

    def compute_from_tiger_bitmap(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_path: str,
        permission: str = "read",
    ) -> bool | None:
        """Compute directory visibility from Tiger Cache bitmap.

        This is the key optimization: instead of enumerating N descendants
        from the metadata store and checking each one, we scan the Tiger
        bitmap of accessible resources and check if any path starts with
        the directory prefix.

        Complexity: O(bitmap_size) vs O(n_descendants * permission_check)

        Args:
            zone_id: Zone ID
            subject_type: Subject type (e.g., "user", "agent")
            subject_id: Subject ID
            dir_path: Directory path to check
            permission: Permission to check (default: "read")

        Returns:
            True if directory is visible (has accessible descendants),
            False if not visible,
            None if Tiger Cache is unavailable
        """
        if not self._tiger_cache:
            return None

        self._bitmap_computes += 1

        # Get all accessible resource IDs from Tiger Cache
        accessible_ids = self._tiger_cache.get_accessible_resources(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type="file",
            zone_id=zone_id,
        )

        if not accessible_ids:
            # No accessible resources at all
            self.set_visible(
                zone_id, subject_type, subject_id, dir_path, False, "no_accessible_resources"
            )
            return False

        # Normalize directory prefix for matching
        prefix = dir_path.rstrip("/") + "/"
        if dir_path == "/":
            prefix = "/"

        # Scan bitmap and check if any accessible resource is under this directory
        # This is O(bitmap_size) but avoids N metadata queries
        resource_map = self._tiger_cache._resource_map

        for int_id in accessible_ids:
            res_info = resource_map.get_resource_id(int_id)
            if res_info:
                res_type, res_path = res_info

                # Check if resource is under the directory
                if res_path == dir_path or res_path.startswith(prefix):
                    self.set_visible(
                        zone_id,
                        subject_type,
                        subject_id,
                        dir_path,
                        True,
                        f"descendant:{res_path}",
                    )
                    logger.debug(f"[DirVisCache] BITMAP_COMPUTE: {dir_path} visible via {res_path}")
                    return True

        # No descendants found
        self.set_visible(
            zone_id, subject_type, subject_id, dir_path, False, "no_descendants_in_bitmap"
        )
        logger.debug(f"[DirVisCache] BITMAP_COMPUTE: {dir_path} not visible")
        return False

    def compute_batch_visibility(
        self,
        zone_id: str,
        subject_type: str,
        subject_id: str,
        dir_paths: list[str],
        permission: str = "read",
    ) -> dict[str, bool]:
        """Batch compute visibility for multiple directories in one bitmap scan.

        Fetches accessible resource IDs once via Tiger Cache, then checks all
        directories against the same bitmap in a single scan.

        Performance: N directories checked in 1 bitmap fetch instead of N.

        Args:
            zone_id: Zone ID
            subject_type: Subject type
            subject_id: Subject ID
            dir_paths: List of directory paths to check
            permission: Permission to check (default: "read")

        Returns:
            Dict mapping dir_path -> visible (True/False)
        """
        if not self._tiger_cache:
            return {}

        # Build cache keys for batch fetch

        # Use single get_accessible_resources (already optimized with bloom + L1)
        accessible_ids = self._tiger_cache.get_accessible_resources(
            subject_type=subject_type,
            subject_id=subject_id,
            permission=permission,
            resource_type="file",
            zone_id=zone_id,
        )

        results: dict[str, bool] = {}
        if not accessible_ids:
            for dp in dir_paths:
                self.set_visible(
                    zone_id, subject_type, subject_id, dp, False, "no_accessible_resources"
                )
                results[dp] = False
            return results

        # Build set of accessible paths for fast prefix matching
        resource_map = self._tiger_cache._resource_map
        accessible_paths: list[str] = []
        for int_id in accessible_ids:
            res_info = resource_map.get_resource_id(int_id)
            if res_info:
                accessible_paths.append(res_info[1])

        # Check each directory against accessible paths
        for dp in dir_paths:
            prefix = dp.rstrip("/") + "/" if dp != "/" else "/"
            visible = any(p == dp or p.startswith(prefix) for p in accessible_paths)
            reason = "batch_bitmap" if visible else "no_descendants_in_bitmap"
            self.set_visible(zone_id, subject_type, subject_id, dp, visible, reason)
            results[dp] = visible

        return results

    def invalidate(
        self,
        zone_id: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        dir_path: str | None = None,
    ) -> int:
        """Invalidate cache entries matching the given criteria.

        All parameters are optional - omitting a parameter matches all values
        for that field.

        Args:
            zone_id: Optional zone ID to match
            subject_type: Optional subject type to match
            subject_id: Optional subject ID to match
            dir_path: Optional directory path (invalidates this path AND ancestors)

        Returns:
            Number of entries invalidated
        """
        invalidated = 0

        with self._lock:
            keys_to_remove = []

            for key in self._cache:
                k_zone, k_subject_type, k_subject_id, k_path = key

                # Match criteria
                if zone_id is not None and k_zone != zone_id:
                    continue
                if subject_type is not None and k_subject_type != subject_type:
                    continue
                if subject_id is not None and k_subject_id != subject_id:
                    continue

                # For path matching, invalidate the path AND all ancestor paths
                # (because a change to /a/b/c affects visibility of /a/b, /a, /)
                if dir_path is not None:
                    # Check if cached path is an ancestor of changed path
                    # or if cached path equals changed path
                    normalized_dir = dir_path.rstrip("/")
                    normalized_k = k_path.rstrip("/")

                    # Invalidate if:
                    # 1. Cached path is exact match
                    # 2. Changed path starts with cached path (cached is ancestor)
                    is_ancestor = (
                        normalized_dir.startswith(normalized_k + "/") or normalized_k == ""
                    )
                    is_exact = normalized_k == normalized_dir

                    if not (is_exact or is_ancestor):
                        continue

                keys_to_remove.append(key)

            for key in keys_to_remove:
                del self._cache[key]
                invalidated += 1

            if invalidated > 0:
                logger.debug(
                    f"[DirVisCache] INVALIDATE: {invalidated} entries "
                    f"(zone={zone_id}, subject={subject_type}:{subject_id}, path={dir_path})"
                )

        return invalidated

    def invalidate_for_resource(
        self,
        resource_path: str,
        zone_id: str,
    ) -> int:
        """Invalidate cache entries affected by a resource change.

        When a resource at path /a/b/c changes, we need to invalidate
        visibility cache for /a/b, /a, and / (all ancestors).

        Args:
            resource_path: Path of the changed resource
            zone_id: Zone ID

        Returns:
            Number of entries invalidated
        """
        # Get all ancestor paths
        ancestors = self._get_ancestor_paths(resource_path)

        total_invalidated = 0
        for ancestor in ancestors:
            total_invalidated += self.invalidate(zone_id=zone_id, dir_path=ancestor)

        return total_invalidated

    def _get_ancestor_paths(self, path: str) -> list[str]:
        """Get all ancestor paths of a given path.

        Args:
            path: Path to get ancestors for

        Returns:
            List of ancestor paths, from immediate parent to root
        """
        ancestors = []
        parts = path.rstrip("/").split("/")

        # Build ancestor paths
        for i in range(len(parts) - 1, 0, -1):
            ancestor = "/".join(parts[:i])
            if ancestor:
                ancestors.append(ancestor)

        # Always include root
        ancestors.append("/")

        return ancestors

    def _evict_oldest(self) -> None:
        """Evict oldest entries when cache is at capacity.

        Note: cachebox TTLCache handles eviction automatically.
        This method is retained for manual eviction edge cases.
        """
        # Sort by computed_at and remove oldest 10%
        if not self._cache:
            return

        entries = sorted(self._cache.items(), key=lambda x: x[1].computed_at)
        to_remove = max(1, len(entries) // 10)

        for key, _ in entries[:to_remove]:
            del self._cache[key]

        logger.debug(f"[DirVisCache] EVICT: removed {to_remove} oldest entries")

    def get_metrics(self) -> dict:
        """Get cache metrics.

        Returns:
            Dict with hit rate, cache size, and other metrics
        """
        with self._lock:
            total = self._hits + self._misses
            hit_rate = self._hits / total if total > 0 else 0.0

            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": hit_rate,
                "bitmap_computes": self._bitmap_computes,
                "cache_size": len(self._cache),
                "max_entries": self._max_entries,
                "ttl": self._ttl,
            }

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            if self._bloom is not None and BloomFilter is not None:
                self._bloom = BloomFilter(self._bloom_capacity, self._bloom_fpp)
            logger.debug("[DirVisCache] CLEAR: all entries removed")

    def __len__(self) -> int:
        """Return number of cached entries."""
        with self._lock:
            return len(self._cache)
