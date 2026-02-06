"""In-memory caching layer for Nexus metadata operations.

This module provides thread-safe in-memory caching to reduce database queries
and improve performance for frequently accessed metadata.

Issue #911: Added compact metadata support for 3x memory reduction at scale.
Issue #715: Added adaptive TTL based on write frequency.
"""

import threading
import time
from typing import Any, cast

from cachetools import LRUCache

from nexus.core._compact_generated import CompactFileMetadata
from nexus.core._metadata_generated import FileMetadata
from nexus.core.adaptive_ttl import AdaptiveTTLMixin


class AdaptiveTTLCache(dict[str, tuple[Any, float]]):
    """LRU cache with per-entry adaptive TTL support.

    Each entry stores (value, expiry_time). Expired entries are lazily removed on access.
    Supports variable TTL per entry, unlike cachetools.TTLCache which uses fixed TTL.

    Issue #715: Enables adaptive TTL based on write frequency.
    """

    def __init__(self, maxsize: int):
        super().__init__()
        self._maxsize = maxsize
        self._order: list[str] = []  # Track access order for LRU

    @property
    def maxsize(self) -> int:
        return self._maxsize

    def get_with_expiry(self, key: str, default: Any = None) -> Any:
        """Get value if not expired."""
        if key not in self:
            return default

        value, expiry = self[key]
        if time.time() > expiry:
            # Expired - remove and return default
            self.pop(key, None)
            if key in self._order:
                self._order.remove(key)
            return default

        # Move to end (most recently used)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

        return value

    def set_with_ttl(self, key: str, value: Any, ttl: float) -> None:
        """Set value with specific TTL."""
        expiry = time.time() + ttl

        # Evict LRU entries if at capacity
        while len(self) >= self._maxsize and self._order:
            lru_key = self._order.pop(0)
            self.pop(lru_key, None)

        self[key] = (value, expiry)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)

    def pop_entry(self, key: str) -> Any:
        """Remove entry and return value."""
        if key in self._order:
            self._order.remove(key)
        entry = self.pop(key, None)
        return entry[0] if entry else None


class MetadataCache(AdaptiveTTLMixin):
    """
    Multi-level in-memory cache for metadata operations.

    Provides separate caches for different access patterns:
    - Path metadata cache: Caches get() results
    - Directory listing cache: Caches list() results
    - File metadata KV cache: Caches get_file_metadata() results
    - Existence cache: Caches exists() results

    Issue #911: Supports compact metadata storage for 3x memory reduction.
    When use_compact=True, FileMetadata objects are converted to CompactFileMetadata
    internally, reducing memory from ~200-300 bytes to ~64-100 bytes per entry.
    """

    def __init__(
        self,
        path_cache_size: int = 512,
        list_cache_size: int = 128,
        kv_cache_size: int = 256,
        exists_cache_size: int = 1024,
        ttl_seconds: int | None = None,
        use_compact: bool = True,
        enable_adaptive_ttl: bool = True,
    ):
        """
        Initialize metadata cache.

        Args:
            path_cache_size: Maximum entries for path metadata cache
            list_cache_size: Maximum entries for directory listing cache
            kv_cache_size: Maximum entries for file metadata KV cache
            exists_cache_size: Maximum entries for existence check cache
            ttl_seconds: Time-to-live for cache entries in seconds (None = no expiry)
            use_compact: If True, store FileMetadata as CompactFileMetadata internally
                        for 3x memory reduction (Issue #911). Default: True.
            enable_adaptive_ttl: If True, adjust TTL based on write frequency
                        (Issue #715). Default: True. Requires ttl_seconds to be set.
        """
        # Initialize adaptive TTL mixin
        base_ttl = ttl_seconds if ttl_seconds else 300
        AdaptiveTTLMixin.__init__(
            self,
            base_ttl=base_ttl,
            enable_adaptive_ttl=enable_adaptive_ttl and ttl_seconds is not None,
        )

        self._ttl_seconds = ttl_seconds
        self._use_compact = use_compact
        self._lock = threading.RLock()

        # Use adaptive TTL cache when TTL is enabled (Issue #715)
        # Otherwise use simple LRU cache (no expiry)
        if ttl_seconds:
            # Adaptive TTL cache supports per-entry TTL
            self._path_cache: (
                LRUCache[str, FileMetadata | CompactFileMetadata | None] | AdaptiveTTLCache
            ) = AdaptiveTTLCache(maxsize=path_cache_size)
            self._list_cache: (
                LRUCache[str, list[FileMetadata] | list[CompactFileMetadata]] | AdaptiveTTLCache
            ) = AdaptiveTTLCache(maxsize=list_cache_size)
            self._kv_cache: LRUCache[tuple[str, str], Any] | AdaptiveTTLCache = AdaptiveTTLCache(
                maxsize=kv_cache_size
            )
            self._exists_cache: LRUCache[str, bool] | AdaptiveTTLCache = AdaptiveTTLCache(
                maxsize=exists_cache_size
            )
        else:
            self._path_cache = LRUCache(maxsize=path_cache_size)
            self._list_cache = LRUCache(maxsize=list_cache_size)
            self._kv_cache = LRUCache(maxsize=kv_cache_size)
            self._exists_cache = LRUCache(maxsize=exists_cache_size)

    def get_path(self, path: str) -> FileMetadata | None | object:
        """
        Get cached path metadata.

        Args:
            path: Virtual path

        Returns:
            FileMetadata if cached, None if cached as not found, sentinel if not cached

        Note:
            Internally may store CompactFileMetadata for memory efficiency,
            but always returns FileMetadata to callers (Issue #911).
        """
        with self._lock:
            # Use adaptive TTL cache if enabled (Issue #715)
            if isinstance(self._path_cache, AdaptiveTTLCache):
                result = self._path_cache.get_with_expiry(path, _CACHE_MISS)
            else:
                result = self._path_cache.get(path, _CACHE_MISS)

            if result is _CACHE_MISS or result is None:
                return result
            # Convert from compact format if needed
            if self._use_compact and isinstance(result, CompactFileMetadata):
                return result.to_file_metadata()
            return cast(FileMetadata, result)

    def set_path(self, path: str, metadata: FileMetadata | None) -> None:
        """
        Cache path metadata.

        Args:
            path: Virtual path
            metadata: File metadata (None if path doesn't exist)

        Note:
            When use_compact=True, converts FileMetadata to CompactFileMetadata
            for 3x memory reduction (Issue #911).
            When adaptive TTL is enabled, TTL is based on write frequency (Issue #715).
        """
        with self._lock:
            # Convert to compact format for memory efficiency (Issue #911)
            value: FileMetadata | CompactFileMetadata | None = (
                metadata if metadata is None or not self._use_compact else metadata.to_compact()
            )

            # Use adaptive TTL if enabled (Issue #715)
            if isinstance(self._path_cache, AdaptiveTTLCache):
                ttl = self.get_adaptive_ttl(path)
                self._path_cache.set_with_ttl(path, value, ttl)
            else:
                self._path_cache[path] = value

    def get_list(self, prefix: str) -> list[FileMetadata] | None:
        """
        Get cached directory listing.

        Args:
            prefix: Path prefix

        Returns:
            List of FileMetadata if cached, None if not cached

        Note:
            Internally may store list[CompactFileMetadata] for memory efficiency,
            but always returns list[FileMetadata] to callers (Issue #911).
        """
        with self._lock:
            # Use adaptive TTL cache if enabled (Issue #715)
            if isinstance(self._list_cache, AdaptiveTTLCache):
                result = self._list_cache.get_with_expiry(prefix)
            else:
                result = self._list_cache.get(prefix)

            if result is None:
                return None
            # Convert from compact format if needed
            if self._use_compact and result and isinstance(result[0], CompactFileMetadata):
                return [cast(CompactFileMetadata, item).to_file_metadata() for item in result]
            return cast(list[FileMetadata], result)

    def set_list(self, prefix: str, files: list[FileMetadata]) -> None:
        """
        Cache directory listing.

        Args:
            prefix: Path prefix
            files: List of file metadata

        Note:
            When use_compact=True, converts list[FileMetadata] to list[CompactFileMetadata]
            for 3x memory reduction (Issue #911).
            When adaptive TTL is enabled, TTL is based on write frequency (Issue #715).
        """
        with self._lock:
            # Convert to compact format for memory efficiency (Issue #911)
            value = files if not self._use_compact or not files else [f.to_compact() for f in files]

            # Use adaptive TTL if enabled (Issue #715)
            if isinstance(self._list_cache, AdaptiveTTLCache):
                ttl = self.get_adaptive_ttl(prefix)
                self._list_cache.set_with_ttl(prefix, value, ttl)
            else:
                self._list_cache[prefix] = value

    def get_kv(self, path: str, key: str) -> Any | object:
        """
        Get cached file metadata key-value.

        Args:
            path: Virtual path
            key: Metadata key

        Returns:
            Metadata value if cached, sentinel if not cached
        """
        with self._lock:
            cache_key = f"{path}:{key}"
            if isinstance(self._kv_cache, AdaptiveTTLCache):
                return self._kv_cache.get_with_expiry(cache_key, _CACHE_MISS)
            return self._kv_cache.get((path, key), _CACHE_MISS)

    def set_kv(self, path: str, key: str, value: Any) -> None:
        """
        Cache file metadata key-value.

        Args:
            path: Virtual path
            key: Metadata key
            value: Metadata value
        """
        with self._lock:
            if isinstance(self._kv_cache, AdaptiveTTLCache):
                cache_key = f"{path}:{key}"
                ttl = self.get_adaptive_ttl(path)
                self._kv_cache.set_with_ttl(cache_key, value, ttl)
            else:
                self._kv_cache[(path, key)] = value

    def get_exists(self, path: str) -> bool | None:
        """
        Get cached existence check result.

        Args:
            path: Virtual path

        Returns:
            True/False if cached, None if not cached
        """
        with self._lock:
            if isinstance(self._exists_cache, AdaptiveTTLCache):
                result = self._exists_cache.get_with_expiry(path)
            else:
                result = self._exists_cache.get(path)
            return result

    def set_exists(self, path: str, exists: bool) -> None:
        """
        Cache existence check result.

        Args:
            path: Virtual path
            exists: Whether the path exists
        """
        with self._lock:
            if isinstance(self._exists_cache, AdaptiveTTLCache):
                ttl = self.get_adaptive_ttl(path)
                self._exists_cache.set_with_ttl(path, exists, ttl)
            else:
                self._exists_cache[path] = exists

    def invalidate_path(self, path: str) -> None:
        """
        Invalidate all cache entries related to a path.

        Called when a file is created, updated, or deleted.
        Also tracks the write for adaptive TTL calculation (Issue #715).

        Args:
            path: Virtual path
        """
        # Track write for adaptive TTL (Issue #715)
        # This must be called even if cache entry doesn't exist
        self.track_write(path)

        with self._lock:
            # Invalidate path metadata cache
            if isinstance(self._path_cache, AdaptiveTTLCache):
                self._path_cache.pop_entry(path)
            else:
                self._path_cache.pop(path, None)

            # Invalidate existence cache
            if isinstance(self._exists_cache, AdaptiveTTLCache):
                self._exists_cache.pop_entry(path)
            else:
                self._exists_cache.pop(path, None)

            # Invalidate list cache entries that might contain this path
            # Need to invalidate all prefixes that could include this path
            # Cache keys are in format:
            #   - Old: "prefix:r" or "prefix:nr"
            #   - New (Issue #904): "prefix:r:t=zone_id" or "prefix:nr:t=zone_id"
            cache_keys_to_invalidate = []
            for cache_key in list(self._list_cache.keys()):
                # Extract prefix from cache key
                # Format: "prefix:r" or "prefix:nr" or "prefix:r:t=X" or "prefix:nr:t=X"
                prefix = cache_key

                # Try to extract prefix by finding :r or :nr pattern
                if ":nr:" in cache_key:
                    # Format: prefix:nr:t=X
                    prefix = cache_key.split(":nr:")[0]
                elif ":r:" in cache_key:
                    # Format: prefix:r:t=X
                    prefix = cache_key.split(":r:")[0]
                elif cache_key.endswith(":nr"):
                    # Format: prefix:nr (old format)
                    prefix = cache_key[:-3]
                elif cache_key.endswith(":r"):
                    # Format: prefix:r (old format)
                    prefix = cache_key[:-2]

                # If path starts with prefix, the listing might be affected
                if path.startswith(prefix):
                    cache_keys_to_invalidate.append(cache_key)

            for cache_key in cache_keys_to_invalidate:
                if isinstance(self._list_cache, AdaptiveTTLCache):
                    self._list_cache.pop_entry(cache_key)
                else:
                    self._list_cache.pop(cache_key, None)

            # Invalidate all KV cache entries for this path
            if isinstance(self._kv_cache, AdaptiveTTLCache):
                kv_keys_to_invalidate = [
                    k for k in list(self._kv_cache.keys()) if k.startswith(f"{path}:")
                ]
                for kv_key in kv_keys_to_invalidate:
                    self._kv_cache.pop_entry(kv_key)
            else:
                kv_keys_to_invalidate = [
                    (p, k) for (p, k) in list(self._kv_cache.keys()) if p == path
                ]
                for kv_key in kv_keys_to_invalidate:
                    self._kv_cache.pop(kv_key, None)

    def invalidate_kv(self, path: str, key: str) -> None:
        """
        Invalidate a specific file metadata key-value cache entry.

        Args:
            path: Virtual path
            key: Metadata key
        """
        with self._lock:
            if isinstance(self._kv_cache, AdaptiveTTLCache):
                cache_key = f"{path}:{key}"
                self._kv_cache.pop_entry(cache_key)
            else:
                self._kv_cache.pop((path, key), None)

    def clear(self) -> None:
        """Clear all cache entries."""
        with self._lock:
            self._path_cache.clear()
            self._list_cache.clear()
            self._kv_cache.clear()
            self._exists_cache.clear()

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics including compact mode and adaptive TTL status
        """
        with self._lock:
            stats: dict[str, Any] = {
                "path_cache_size": len(self._path_cache),
                "list_cache_size": len(self._list_cache),
                "kv_cache_size": len(self._kv_cache),
                "exists_cache_size": len(self._exists_cache),
                "path_cache_maxsize": self._path_cache.maxsize,
                "list_cache_maxsize": self._list_cache.maxsize,
                "kv_cache_maxsize": self._kv_cache.maxsize,
                "exists_cache_maxsize": self._exists_cache.maxsize,
                "ttl_seconds": self._ttl_seconds,
                "use_compact": self._use_compact,  # Issue #911
            }

            # Add interning pool stats when using compact mode
            if self._use_compact:
                from nexus.core._compact_generated import get_intern_pool_stats as get_pool_stats

                stats["intern_pools"] = get_pool_stats()

            # Add adaptive TTL stats (Issue #715)
            stats["adaptive_ttl"] = self.get_adaptive_ttl_stats()

            return stats


# Sentinel object to distinguish "not in cache" from "cached as None"
_CACHE_MISS = object()
