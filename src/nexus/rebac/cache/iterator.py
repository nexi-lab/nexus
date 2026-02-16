"""Iterator caching layer for ReBAC paginated list operations.

This module provides caching for paginated query results, avoiding
recomputation of full result sets on each page request.

Architecture:
- Cache stores full computed results keyed by query hash
- Subsequent page requests fetch from cache instead of recomputing
- TTL-based expiration (default: 5 minutes, matching zone graph cache)
- Thread-safe with RLock
- Automatic invalidation when permissions change

Performance:
- Page 1: Compute and cache (same as before)
- Page 2+: <1ms cache lookup (vs 50ms+ recomputation)
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)


@dataclass
class CachedResult:
    """Represents a cached result set for paginated queries."""

    cursor_id: str  # Unique identifier (UUID)
    query_hash: str  # Hash of query parameters
    results: list[Any]  # Full computed results
    total_count: int  # Total number of results
    created_at: float  # Timestamp for TTL tracking
    zone_id: str  # For zone-based invalidation


class CursorExpiredError(Exception):
    """Raised when a pagination cursor has expired or been invalidated."""

    pass


class IteratorCache:
    """
    Thread-safe cache for paginated query results.

    Provides efficient pagination by caching full result sets and serving
    subsequent page requests from cache.

    Example:
        >>> cache = IteratorCache(max_size=1000, ttl_seconds=300)
        >>> # First request - compute and cache
        >>> cursor_id, results, total = cache.get_or_create(
        ...     query_hash="incoming:user123",
        ...     zone_id="default",
        ...     compute_fn=lambda: fetch_all_shares("user123")
        ... )
        >>> # Subsequent page - fetch from cache
        >>> items, next_cursor, total = cache.get_page(cursor_id, offset=100, limit=50)
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl_seconds: int = 300,
        enable_metrics: bool = True,
    ):
        """
        Initialize iterator cache.

        Args:
            max_size: Maximum number of cached result sets (default: 1000)
            ttl_seconds: Time-to-live for cache entries (default: 300s = 5min)
            enable_metrics: Track hit rates and performance (default: True)
        """
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._enable_metrics = enable_metrics
        self._lock = threading.RLock()

        # Main cache: cursor_id -> CachedResult
        self._cache: TTLCache[str, CachedResult] = TTLCache(maxsize=max_size, ttl=ttl_seconds)

        # Query hash to cursor mapping for deduplication
        # When same query is made, return existing cursor instead of creating new
        self._query_to_cursor: dict[str, str] = {}

        # Metrics tracking
        self._hits = 0
        self._misses = 0
        self._expired_cursors = 0
        self._evictions = 0
        self._total_results_cached = 0

    def get_or_create(
        self,
        query_hash: str,
        zone_id: str,
        compute_fn: Callable[[], list[Any]],
    ) -> tuple[str, list[Any], int]:
        """
        Get cached results or compute and cache new ones.

        If results for this query hash exist in cache, returns them.
        Otherwise, calls compute_fn to generate results and caches them.

        Args:
            query_hash: Unique hash identifying the query parameters
            zone_id: Zone ID for isolation and invalidation
            compute_fn: Function to compute results if not cached

        Returns:
            Tuple of (cursor_id, results, total_count)
        """
        with self._lock:
            # Check if we already have this query cached
            if query_hash in self._query_to_cursor:
                cursor_id = self._query_to_cursor[query_hash]
                if cursor_id in self._cache:
                    cached = self._cache[cursor_id]
                    if self._enable_metrics:
                        self._hits += 1
                    logger.debug(
                        f"Iterator cache hit for query_hash={query_hash}, "
                        f"cursor_id={cursor_id}, count={cached.total_count}"
                    )
                    return cursor_id, cached.results, cached.total_count
                else:
                    # Cursor expired, remove mapping
                    del self._query_to_cursor[query_hash]

            if self._enable_metrics:
                self._misses += 1

        # Compute results outside lock to avoid blocking
        logger.debug(f"Iterator cache miss for query_hash={query_hash}, computing...")
        start_time = time.perf_counter()
        results = compute_fn()
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            f"Computed {len(results)} results in {elapsed_ms:.1f}ms for query_hash={query_hash}"
        )

        with self._lock:
            # Create new cached result
            cursor_id = str(uuid.uuid4())
            cached = CachedResult(
                cursor_id=cursor_id,
                query_hash=query_hash,
                results=results,
                total_count=len(results),
                created_at=time.time(),
                zone_id=zone_id,
            )

            # Store in cache
            self._cache[cursor_id] = cached
            self._query_to_cursor[query_hash] = cursor_id

            if self._enable_metrics:
                self._total_results_cached += len(results)

            return cursor_id, results, len(results)

    def get_page(
        self,
        cursor_id: str,
        offset: int,
        limit: int,
    ) -> tuple[list[Any], str | None, int]:
        """
        Get a page of results from cached cursor.

        Args:
            cursor_id: Cursor ID from previous get_or_create call
            offset: Offset into results (0-based)
            limit: Maximum number of items to return

        Returns:
            Tuple of (items, next_cursor, total_count)
            next_cursor is None if no more results

        Raises:
            CursorExpiredError: If cursor has expired or been invalidated
        """
        with self._lock:
            if cursor_id not in self._cache:
                if self._enable_metrics:
                    self._expired_cursors += 1
                raise CursorExpiredError(f"Cursor {cursor_id} has expired or been invalidated")

            cached = self._cache[cursor_id]

            if self._enable_metrics:
                self._hits += 1

            # Get page slice
            items = cached.results[offset : offset + limit]

            # Determine if there are more results
            has_more = offset + limit < cached.total_count
            next_cursor = cursor_id if has_more else None

            return items, next_cursor, cached.total_count

    def invalidate_zone(self, zone_id: str) -> int:
        """
        Invalidate all cached results for a zone.

        Called when permissions change for a zone.

        Args:
            zone_id: Zone ID to invalidate

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            cursors_to_delete = []
            query_hashes_to_delete = []

            for cursor_id, cached in list(self._cache.items()):
                if cached.zone_id == zone_id:
                    cursors_to_delete.append(cursor_id)
                    query_hashes_to_delete.append(cached.query_hash)

            for cursor_id in cursors_to_delete:
                del self._cache[cursor_id]

            for query_hash in query_hashes_to_delete:
                if query_hash in self._query_to_cursor:
                    del self._query_to_cursor[query_hash]

            if self._enable_metrics:
                self._evictions += len(cursors_to_delete)

            if cursors_to_delete:
                logger.debug(
                    f"Iterator cache: Invalidated {len(cursors_to_delete)} entries "
                    f"for zone {zone_id}"
                )

            return len(cursors_to_delete)

    def invalidate_cursor(self, cursor_id: str) -> bool:
        """
        Invalidate a specific cursor.

        Args:
            cursor_id: Cursor ID to invalidate

        Returns:
            True if cursor was found and invalidated, False otherwise
        """
        with self._lock:
            if cursor_id not in self._cache:
                return False

            cached = self._cache[cursor_id]
            query_hash = cached.query_hash

            del self._cache[cursor_id]
            if query_hash in self._query_to_cursor:
                del self._query_to_cursor[query_hash]

            if self._enable_metrics:
                self._evictions += 1

            return True

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            self._query_to_cursor.clear()
            if self._enable_metrics:
                self._evictions += count
            logger.info(f"Iterator cache cleared ({count} entries)")

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0

            return {
                "max_size": self._max_size,
                "current_size": len(self._cache),
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate_percent": round(hit_rate, 2),
                "expired_cursors": self._expired_cursors,
                "evictions": self._evictions,
                "total_results_cached": self._total_results_cached,
                "active_queries": len(self._query_to_cursor),
            }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._expired_cursors = 0
            self._evictions = 0
            self._total_results_cached = 0
            logger.info("Iterator cache stats reset")
