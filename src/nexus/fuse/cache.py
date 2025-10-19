"""Cache implementations for FUSE mount performance optimization.

This module provides caching layers for file attributes, content, and parsed
content to optimize FUSE filesystem operations and reduce latency.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from cachetools import LRUCache, TTLCache

logger = logging.getLogger(__name__)


class FUSECacheManager:
    """Manages caching for FUSE operations.

    This class provides three types of caches:
    1. Attribute cache (TTL-based): Caches getattr() results
    2. Content cache (LRU-based): Caches raw file content
    3. Parsed cache (LRU-based): Caches parsed file content

    All caches are thread-safe and support invalidation on write/delete operations.

    Example:
        >>> cache_mgr = FUSECacheManager(
        ...     attr_cache_size=1024,
        ...     attr_cache_ttl=60,
        ...     content_cache_size=100,
        ...     parsed_cache_size=50
        ... )
        >>>
        >>> # Cache attribute lookup
        >>> cache_mgr.cache_attr("/file.txt", {"st_size": 1024, ...})
        >>> attrs = cache_mgr.get_attr("/file.txt")
        >>>
        >>> # Invalidate on write
        >>> cache_mgr.invalidate_path("/file.txt")
    """

    def __init__(
        self,
        attr_cache_size: int = 1024,
        attr_cache_ttl: int = 60,
        content_cache_size: int = 100,
        parsed_cache_size: int = 50,
        enable_metrics: bool = False,
    ) -> None:
        """Initialize cache manager.

        Args:
            attr_cache_size: Maximum number of attribute entries (default: 1024)
            attr_cache_ttl: TTL for attribute cache in seconds (default: 60)
            content_cache_size: Maximum number of content entries (default: 100)
            parsed_cache_size: Maximum number of parsed content entries (default: 50)
            enable_metrics: If True, track cache hit/miss metrics
        """
        # Attribute cache: TTL-based for freshness
        self._attr_cache: TTLCache[str, dict[str, Any]] = TTLCache(
            maxsize=attr_cache_size, ttl=attr_cache_ttl
        )

        # Content cache: LRU-based for frequently accessed files
        self._content_cache: LRUCache[str, bytes] = LRUCache(maxsize=content_cache_size)

        # Parsed content cache: LRU-based for expensive parsing operations
        self._parsed_cache: LRUCache[str, bytes] = LRUCache(maxsize=parsed_cache_size)

        # Thread safety
        self._attr_lock = threading.RLock()
        self._content_lock = threading.RLock()
        self._parsed_lock = threading.RLock()

        # Metrics
        self._enable_metrics = enable_metrics
        self._metrics = {
            "attr_hits": 0,
            "attr_misses": 0,
            "content_hits": 0,
            "content_misses": 0,
            "parsed_hits": 0,
            "parsed_misses": 0,
            "invalidations": 0,
        }
        self._metrics_lock = threading.Lock()

    # ============================================================
    # Attribute Cache
    # ============================================================

    def get_attr(self, path: str) -> dict[str, Any] | None:
        """Get cached file attributes.

        Args:
            path: File path

        Returns:
            Cached attributes dict or None if not cached
        """
        with self._attr_lock:
            result = self._attr_cache.get(path)

            if self._enable_metrics:
                with self._metrics_lock:
                    if result is not None:
                        self._metrics["attr_hits"] += 1
                    else:
                        self._metrics["attr_misses"] += 1

            return result

    def cache_attr(self, path: str, attrs: dict[str, Any]) -> None:
        """Cache file attributes.

        Args:
            path: File path
            attrs: Attributes dictionary to cache
        """
        with self._attr_lock:
            self._attr_cache[path] = attrs

    # ============================================================
    # Content Cache
    # ============================================================

    def get_content(self, path: str) -> bytes | None:
        """Get cached file content.

        Args:
            path: File path

        Returns:
            Cached content or None if not cached
        """
        with self._content_lock:
            result = self._content_cache.get(path)

            if self._enable_metrics:
                with self._metrics_lock:
                    if result is not None:
                        self._metrics["content_hits"] += 1
                    else:
                        self._metrics["content_misses"] += 1

            return result

    def cache_content(self, path: str, content: bytes) -> None:
        """Cache file content.

        Args:
            path: File path
            content: File content to cache
        """
        with self._content_lock:
            self._content_cache[path] = content

    # ============================================================
    # Parsed Content Cache
    # ============================================================

    def get_parsed(self, path: str, view_type: str) -> bytes | None:
        """Get cached parsed content.

        Args:
            path: File path
            view_type: View type (e.g., "txt", "md")

        Returns:
            Cached parsed content or None if not cached
        """
        cache_key = f"{path}:{view_type}"

        with self._parsed_lock:
            result = self._parsed_cache.get(cache_key)

            if self._enable_metrics:
                with self._metrics_lock:
                    if result is not None:
                        self._metrics["parsed_hits"] += 1
                    else:
                        self._metrics["parsed_misses"] += 1

            return result

    def cache_parsed(self, path: str, view_type: str, content: bytes) -> None:
        """Cache parsed content.

        Args:
            path: File path
            view_type: View type (e.g., "txt", "md")
            content: Parsed content to cache
        """
        cache_key = f"{path}:{view_type}"

        with self._parsed_lock:
            self._parsed_cache[cache_key] = content

    # ============================================================
    # Cache Invalidation
    # ============================================================

    def invalidate_path(self, path: str) -> None:
        """Invalidate all caches for a specific path.

        This should be called on write, delete, or rename operations.

        Args:
            path: File path to invalidate
        """
        with self._attr_lock:
            self._attr_cache.pop(path, None)

        with self._content_lock:
            self._content_cache.pop(path, None)

        with self._parsed_lock:
            # Invalidate all parsed views for this path
            keys_to_remove = [key for key in self._parsed_cache if key.startswith(f"{path}:")]
            for key in keys_to_remove:
                self._parsed_cache.pop(key, None)

        if self._enable_metrics:
            with self._metrics_lock:
                self._metrics["invalidations"] += 1

    def invalidate_all(self) -> None:
        """Invalidate all caches.

        This is useful for testing or when you need to clear all cached data.
        """
        with self._attr_lock:
            self._attr_cache.clear()

        with self._content_lock:
            self._content_cache.clear()

        with self._parsed_lock:
            self._parsed_cache.clear()

        logger.info("All FUSE caches invalidated")

    # ============================================================
    # Metrics
    # ============================================================

    def get_metrics(self) -> dict[str, Any]:
        """Get cache metrics.

        Returns:
            Dictionary with cache hit/miss statistics
        """
        if not self._enable_metrics:
            return {}

        with self._metrics_lock:
            total_attr = self._metrics["attr_hits"] + self._metrics["attr_misses"]
            total_content = self._metrics["content_hits"] + self._metrics["content_misses"]
            total_parsed = self._metrics["parsed_hits"] + self._metrics["parsed_misses"]

            return {
                "attr_hits": self._metrics["attr_hits"],
                "attr_misses": self._metrics["attr_misses"],
                "attr_hit_rate": (
                    self._metrics["attr_hits"] / total_attr if total_attr > 0 else 0.0
                ),
                "content_hits": self._metrics["content_hits"],
                "content_misses": self._metrics["content_misses"],
                "content_hit_rate": (
                    self._metrics["content_hits"] / total_content if total_content > 0 else 0.0
                ),
                "parsed_hits": self._metrics["parsed_hits"],
                "parsed_misses": self._metrics["parsed_misses"],
                "parsed_hit_rate": (
                    self._metrics["parsed_hits"] / total_parsed if total_parsed > 0 else 0.0
                ),
                "invalidations": self._metrics["invalidations"],
                "cache_sizes": {
                    "attr": len(self._attr_cache),
                    "content": len(self._content_cache),
                    "parsed": len(self._parsed_cache),
                },
            }

    def reset_metrics(self) -> None:
        """Reset all cache metrics."""
        if not self._enable_metrics:
            return

        with self._metrics_lock:
            for key in self._metrics:
                self._metrics[key] = 0
