"""Read Set-Aware Cache — Precise invalidation via dependency tracking (Issue #1169).

This module wraps MetadataCache with read set awareness, enabling targeted
cache invalidation. Instead of coarse path-based invalidation (which clears
all entries matching a prefix), the wrapper uses ReadSetRegistry's reverse
index to invalidate only entries whose read sets overlap with a write.

Architecture (4-tier placement: Storage layer):
    MetadataCache <- ReadSetAwareCache <- NexusFS
                      |
                 ReadSetRegistry (kernel-level reverse index)

Design decisions:
    - Wrapper (not embedded) to avoid bloating MetadataCache (Issue 1 / Option A)
    - Single invalidation gateway: invalidate_for_write() is SSOT (Issue 5 / Option A)
    - Eviction callback: AdaptiveTTLCache on_evict cleans up read sets (Issue 3 / Option A)
    - Zookie pattern: rejects stale inserts at cache-put time (Issue 8 / Option A)
    - Uses ReadSetRegistry reverse index for O(1)+O(d) lookups (Issue 14 / Option A)

See also:
    - nexus.core.read_set: ReadSet, ReadSetRegistry infrastructure
    - nexus.storage.cache: MetadataCache (wrapped by this module)
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md: 4-tier architecture
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from nexus.core.read_set import ReadSet, ReadSetRegistry
from nexus.storage.cache import _CACHE_MISS, MetadataCache

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadata

logger = logging.getLogger(__name__)


class ReadSetAwareCache:
    """Cache wrapper that uses read sets for precise invalidation.

    Wraps MetadataCache and intercepts invalidation calls.
    Uses ReadSetRegistry reverse index for O(1) + O(d) lookups.

    Thread-safe: all mutable state is protected by _lock.

    Attributes:
        _cache: Underlying MetadataCache
        _registry: ReadSetRegistry for reverse-index lookups
        _cache_key_to_query: Maps cache_key -> query_id
        _query_to_cache_keys: Maps query_id -> set of cache_keys
    """

    def __init__(
        self,
        base_cache: MetadataCache,
        registry: ReadSetRegistry,
    ) -> None:
        self._cache = base_cache
        self._registry = registry
        self._lock = threading.RLock()  # Reentrant: eviction callback may re-enter during invalidate

        # Bidirectional mapping: cache_key <-> query_id
        self._cache_key_to_query: dict[str, str] = {}
        self._query_to_cache_keys: dict[str, set[str]] = {}

        # Metrics
        self._stats = {
            "precise_invalidations": 0,
            "skipped_invalidations": 0,
            "fallback_invalidations": 0,
            "stale_insert_rejections": 0,
        }

        # Wire eviction callback into the path cache
        # AdaptiveTTLCache supports on_evict callback (Issue #1169)
        self._wire_eviction_callbacks()

    def _wire_eviction_callbacks(self) -> None:
        """Connect AdaptiveTTLCache eviction to read set cleanup."""
        from nexus.storage.cache import AdaptiveTTLCache

        path_cache = self._cache._path_cache
        if isinstance(path_cache, AdaptiveTTLCache):
            path_cache._on_evict = self._on_cache_evict

    def _on_cache_evict(self, cache_key: str) -> None:
        """Callback when a cache entry is evicted (LRU or TTL).

        Cleans up the associated read set mapping to prevent orphans.
        """
        with self._lock:
            self._cleanup_mapping(cache_key)

    def _cleanup_mapping(self, cache_key: str) -> None:
        """Remove cache_key from internal bidirectional maps.

        Must be called under self._lock.
        """
        query_id = self._cache_key_to_query.pop(cache_key, None)
        if query_id is not None:
            cache_keys = self._query_to_cache_keys.get(query_id)
            if cache_keys is not None:
                cache_keys.discard(cache_key)
                if not cache_keys:
                    del self._query_to_cache_keys[query_id]
                    # Don't unregister from registry — other caches may share it

    # =========================================================================
    # Cache Put with Read Set
    # =========================================================================

    def put_path(
        self,
        path: str,
        metadata: FileMetadata | None,
        read_set: ReadSet | None = None,
        zone_revision: int = 0,
    ) -> None:
        """Store metadata in cache, optionally with read set for precise invalidation.

        Zookie pattern (Issue 8): If zone_revision > 0 and the read set
        contains entries with revision < zone_revision, the data is already
        stale at insert time. We skip caching to prevent serving stale data.

        Args:
            path: Virtual path (cache key)
            metadata: File metadata to cache (None = cache negative result)
            read_set: Optional read set tracking what this entry depends on
            zone_revision: Current zone revision for staleness check
        """
        # Zookie check: reject stale inserts
        if read_set and zone_revision > 0:
            for entry in read_set:
                if entry.is_stale(zone_revision):
                    self._stats["stale_insert_rejections"] += 1
                    logger.debug(
                        f"[ReadSetCache] Rejected stale insert for {path}: "
                        f"entry revision {entry.revision} < zone revision {zone_revision}"
                    )
                    return

        # Delegate to base cache
        self._cache.set_path(path, metadata)

        # Register read set mapping
        if read_set is not None:
            with self._lock:
                query_id = read_set.query_id
                self._cache_key_to_query[path] = query_id
                self._query_to_cache_keys.setdefault(query_id, set()).add(path)

            # Register in the global registry (for reverse-index lookups)
            self._registry.register(read_set)

    # =========================================================================
    # Precise Invalidation Gateway (SSOT — Issue 5)
    # =========================================================================

    def invalidate_for_write(
        self,
        path: str,
        revision: int,
        zone_id: str | None = None,
    ) -> int:
        """Invalidate entries whose read sets overlap with this write.

        This is the SSOT for all cache invalidation. Both the write path
        and the event-based invalidation path call this method.

        Uses ReadSetRegistry reverse index for O(1) + O(d) lookup.
        Falls back to path-based for entries without read sets.

        Args:
            path: Path that was written/deleted
            revision: Zone revision of the write operation
            zone_id: Optional zone filter

        Returns:
            Number of entries invalidated
        """
        invalidated = 0

        # Step 1: Find affected queries via registry reverse index (O(1) + O(d))
        affected_queries = self._registry.get_affected_queries(
            path, revision, zone_id=zone_id
        )

        if affected_queries:
            # Step 2: Map queries -> cache keys -> invalidate
            with self._lock:
                for query_id in affected_queries:
                    cache_keys = self._query_to_cache_keys.get(query_id, set())
                    for cache_key in list(cache_keys):
                        self._cache.invalidate_path(cache_key)
                        self._cleanup_mapping(cache_key)
                        invalidated += 1
                        self._stats["precise_invalidations"] += 1

            logger.debug(
                f"[ReadSetCache] Precise invalidation: write to {path}@{revision} "
                f"invalidated {invalidated} entries via {len(affected_queries)} queries"
            )
        else:
            # Step 3: No queries affected. Check if path is cached at all.
            # If cached WITHOUT a read set, fall back to path-based invalidation.
            # If cached WITH a read set (not affected), skip.
            # If not cached at all, skip.
            path_is_cached = self._cache.get_path(path) is not _CACHE_MISS
            with self._lock:
                has_readset = path in self._cache_key_to_query

            if path_is_cached and not has_readset:
                # Cached entry without read set — use path-based fallback
                self._cache.invalidate_path(path)
                self._stats["fallback_invalidations"] += 1
                invalidated += 1
                logger.debug(
                    f"[ReadSetCache] Fallback invalidation for {path} (no read set)"
                )
            else:
                # Either not cached, or cached with read set that wasn't affected
                self._stats["skipped_invalidations"] += 1
                logger.debug(
                    f"[ReadSetCache] Skipped invalidation: write to {path}@{revision} "
                    f"doesn't affect any cached entries"
                )

        return invalidated

    # =========================================================================
    # Delegated Operations
    # =========================================================================

    def invalidate_path(self, path: str) -> None:
        """Path-based invalidation — delegates to base cache.

        This is called by the existing event-based invalidation path.
        We intercept it to also clean up read set mappings.
        """
        with self._lock:
            self._cleanup_mapping(path)
        self._cache.invalidate_path(path)

    def clear(self) -> None:
        """Clear all cache entries and read set mappings."""
        with self._lock:
            self._cache_key_to_query.clear()
            self._query_to_cache_keys.clear()
        self._cache.clear()

    # =========================================================================
    # Metrics
    # =========================================================================

    def get_stats(self) -> dict[str, Any]:
        """Get cache and invalidation precision metrics."""
        base_stats = self._cache.get_stats()

        precise = self._stats["precise_invalidations"]
        fallback = self._stats["fallback_invalidations"]
        total_invalidations = precise + fallback
        precision_ratio = precise / total_invalidations if total_invalidations > 0 else 0.0

        with self._lock:
            read_set_count = len(self._cache_key_to_query)

        return {
            **base_stats,
            "precise_invalidations": precise,
            "skipped_invalidations": self._stats["skipped_invalidations"],
            "fallback_invalidations": fallback,
            "stale_insert_rejections": self._stats["stale_insert_rejections"],
            "precision_ratio": round(precision_ratio, 4),
            "read_set_count": read_set_count,
        }
