"""Cache implementations for FUSE mount performance optimization.

This module provides caching layers for file attributes, content, and parsed
content to optimize FUSE filesystem operations and reduce latency.
"""

import logging
import threading
from pathlib import PurePosixPath
from typing import Any

from cachetools import LRUCache

from nexus.cache.index_store import IndexKey, MemoryIndexCache

logger = logging.getLogger(__name__)


def _stat_key(path: str, scope_id: str = "default") -> IndexKey:
    return IndexKey("fuse", scope_id, path, "stat")


def _listing_key(path: str, scope_id: str = "default") -> IndexKey:
    return IndexKey("fuse", scope_id, path, "listing")


def _parent_path(path: str) -> str:
    return str(PurePosixPath(path).parent) or "/"


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
        ...     content_cache_size=10000,
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
        listing_cache_size: int = 1024,
        listing_cache_ttl: int | None = None,
        content_cache_size: int = 10000,
        parsed_cache_size: int = 50,
        enable_metrics: bool = False,
    ) -> None:
        """Initialize cache manager.

        Args:
            attr_cache_size: Maximum number of attribute entries (default: 1024)
            attr_cache_ttl: TTL for attribute cache in seconds (default: 60)
            listing_cache_size: Maximum number of listing entries (default: 1024)
            listing_cache_ttl: TTL for directory listings in seconds (defaults to attr TTL)
            content_cache_size: Maximum number of content entries (default: 10000)
            parsed_cache_size: Maximum number of parsed content entries (default: 50)
            enable_metrics: If True, track cache hit/miss metrics
        """
        self._attr_ttl = attr_cache_ttl
        self._listing_ttl = attr_cache_ttl if listing_cache_ttl is None else listing_cache_ttl
        self._index_cache = MemoryIndexCache()
        self._max_index_entries = max(1, attr_cache_size + listing_cache_size)
        self._index_order: dict[IndexKey, None] = {}

        # Content cache: LRU-based for frequently accessed files
        self._content_cache: LRUCache[str, bytes] = LRUCache(maxsize=content_cache_size)

        # Parsed content cache: LRU-based for expensive parsing operations
        self._parsed_cache: LRUCache[str, bytes] = LRUCache(maxsize=parsed_cache_size)

        # Thread safety
        self._index_lock = threading.RLock()
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

    def _remember_index_key(self, key: IndexKey) -> None:
        self._index_order.pop(key, None)
        self._index_order[key] = None
        while len(self._index_order) > self._max_index_entries:
            evicted_key = next(iter(self._index_order))
            self._index_order.pop(evicted_key, None)
            self._index_cache.invalidate_path(evicted_key)

    def _forget_index_key(self, key: IndexKey) -> None:
        self._index_order.pop(key, None)

    def _index_size(self, kind: str | None = None) -> int:
        if kind is None:
            return len(self._index_order)
        return sum(1 for key in self._index_order if key.kind == kind)

    def get_attr(self, path: str, scope_id: str = "default") -> dict[str, Any] | None:
        """Get cached file attributes.

        Args:
            path: File path

        Returns:
            Cached attributes dict or None if not cached
        """
        key = _stat_key(path, scope_id)
        with self._index_lock:
            result = self._index_cache.get(key)
            if result is not None:
                self._remember_index_key(key)
            else:
                self._forget_index_key(key)

        if self._enable_metrics:
            with self._metrics_lock:
                if result is not None:
                    self._metrics["attr_hits"] += 1
                else:
                    self._metrics["attr_misses"] += 1

        return result

    def cache_attr(self, path: str, attrs: dict[str, Any], scope_id: str = "default") -> None:
        """Cache file attributes.

        Args:
            path: File path
            attrs: Attributes dictionary to cache
        """
        key = _stat_key(path, scope_id)
        with self._index_lock:
            self._index_cache.put(key, attrs, ttl_seconds=self._attr_ttl)
            self._remember_index_key(key)

    # ============================================================
    # Directory Listing Cache
    # ============================================================

    def get_listing(self, path: str, scope_id: str = "default") -> list[str] | None:
        """Get cached directory entries.

        Args:
            path: Directory path

        Returns:
            Cached entry names or None if not cached
        """
        key = _listing_key(path, scope_id)
        with self._index_lock:
            result = self._index_cache.get(key)
            if result is not None:
                self._remember_index_key(key)
            else:
                self._forget_index_key(key)
        if result is None:
            return None
        return list(result)

    def cache_listing(
        self,
        path: str,
        entries: list[str],
        scope_id: str = "default",
    ) -> None:
        """Cache directory entries.

        Args:
            path: Directory path
            entries: Directory entry names to cache
        """
        key = _listing_key(path, scope_id)
        with self._index_lock:
            self._index_cache.put(
                key,
                list(entries),
                ttl_seconds=self._listing_ttl,
            )
            self._remember_index_key(key)

    def invalidate_parent_listing(self, path: str, scope_id: str = "default") -> None:
        """Invalidate only the immediate parent directory listing for a path."""
        key = _listing_key(_parent_path(path), scope_id)
        with self._index_lock:
            self._index_cache.invalidate_path(key)
            self._forget_index_key(key)

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

    def get_parsed_size(self, path: str, view_type: str) -> int | None:
        """Get the size of cached parsed content without returning the bytes.

        Lightweight alternative to get_parsed() when only the size is needed
        (e.g., for getattr st_size resolution). Avoids copying large byte arrays.

        Args:
            path: File path
            view_type: View type (e.g., "txt", "md")

        Returns:
            Size in bytes of cached parsed content, or None if not cached
        """
        cache_key = f"{path}:{view_type}"
        with self._parsed_lock:
            result = self._parsed_cache.get(cache_key)
            if result is not None:
                return len(result)
            return None

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

    def invalidate_path(self, path: str, scope_id: str = "default") -> None:
        """Invalidate all caches for a specific path.

        This should be called on write, delete, or rename operations.

        Args:
            path: File path to invalidate
        """
        key = _stat_key(path, scope_id)
        with self._index_lock:
            self._index_cache.invalidate_path(key)
            self._forget_index_key(key)

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

    def invalidate_path_all_scopes(self, path: str) -> None:
        """Invalidate stat entries for a path across all logical scopes."""
        with self._index_lock:
            keys = [
                key
                for key in self._index_order
                if key.path == path and key.kind in {"stat", "negative"}
            ]
            for key in keys:
                self._index_cache.invalidate_path(key)
                self._forget_index_key(key)

        with self._content_lock:
            self._content_cache.pop(path, None)

        with self._parsed_lock:
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
        with self._index_lock:
            self._index_cache = MemoryIndexCache()
            self._index_order.clear()

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
                    "attr": self._index_size("stat"),
                    "listing": self._index_size("listing"),
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

    # ============================================================
    # Lease revocation integration (Issue #3400, Decision 7A)
    # ============================================================

    def on_lease_revoked(self, path: str) -> None:
        """Handle lease revocation by invalidating caches for the path.

        This is the sync entry point called by the lease revocation
        callback bridge.  It delegates to ``invalidate_path_all_scopes()``
        so scoped metadata cannot survive a remote lease revocation.

        Args:
            path: Virtual file path whose lease was revoked.
        """
        self.invalidate_path_all_scopes(path)
