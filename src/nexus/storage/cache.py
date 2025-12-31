"""In-memory caching layer for Nexus metadata operations.

This module provides thread-safe in-memory caching to reduce database queries
and improve performance for frequently accessed metadata.

Issue #911: Added compact metadata support for 3x memory reduction at scale.
"""

import threading
from typing import Any, cast

from cachetools import LRUCache, TTLCache

from nexus.core.compact_metadata import CompactFileMetadata
from nexus.core.metadata import FileMetadata


class MetadataCache:
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
        """
        self._ttl_seconds = ttl_seconds
        self._use_compact = use_compact
        self._lock = threading.RLock()

        # Cache for path metadata (get operation)
        # When use_compact=True, stores CompactFileMetadata instead of FileMetadata
        # Type annotation covers both cases for compatibility
        if ttl_seconds:
            self._path_cache: (
                LRUCache[str, FileMetadata | CompactFileMetadata | None]
                | TTLCache[str, FileMetadata | CompactFileMetadata | None]
            ) = TTLCache(maxsize=path_cache_size, ttl=ttl_seconds)
        else:
            self._path_cache = LRUCache(maxsize=path_cache_size)

        # Cache for directory listings (list operation)
        # When use_compact=True, stores list[CompactFileMetadata]
        if ttl_seconds:
            self._list_cache: (
                LRUCache[str, list[FileMetadata] | list[CompactFileMetadata]]
                | TTLCache[str, list[FileMetadata] | list[CompactFileMetadata]]
            ) = TTLCache(maxsize=list_cache_size, ttl=ttl_seconds)
        else:
            self._list_cache = LRUCache(maxsize=list_cache_size)

        # Cache for file metadata key-value pairs (get_file_metadata operation)
        if ttl_seconds:
            self._kv_cache: LRUCache[tuple[str, str], Any] | TTLCache[tuple[str, str], Any] = (
                TTLCache(maxsize=kv_cache_size, ttl=ttl_seconds)
            )
        else:
            self._kv_cache = LRUCache(maxsize=kv_cache_size)

        # Cache for existence checks (exists operation)
        if ttl_seconds:
            self._exists_cache: LRUCache[str, bool] | TTLCache[str, bool] = TTLCache(
                maxsize=exists_cache_size, ttl=ttl_seconds
            )
        else:
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
            # Use object() as sentinel to distinguish "not in cache" from "cached as None"
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
        """
        with self._lock:
            if metadata is None or not self._use_compact:
                self._path_cache[path] = metadata
            else:
                # Convert to compact format for memory efficiency
                self._path_cache[path] = metadata.to_compact()

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
        """
        with self._lock:
            if not self._use_compact or not files:
                self._list_cache[prefix] = files
            else:
                # Convert to compact format for memory efficiency
                self._list_cache[prefix] = [f.to_compact() for f in files]

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
            result: bool | None = self._exists_cache.get(path)
            return result

    def set_exists(self, path: str, exists: bool) -> None:
        """
        Cache existence check result.

        Args:
            path: Virtual path
            exists: Whether the path exists
        """
        with self._lock:
            self._exists_cache[path] = exists

    def invalidate_path(self, path: str) -> None:
        """
        Invalidate all cache entries related to a path.

        Called when a file is created, updated, or deleted.

        Args:
            path: Virtual path
        """
        with self._lock:
            # Invalidate path metadata cache
            self._path_cache.pop(path, None)

            # Invalidate existence cache
            self._exists_cache.pop(path, None)

            # Invalidate list cache entries that might contain this path
            # Need to invalidate all prefixes that could include this path
            # Cache keys are in format:
            #   - Old: "prefix:r" or "prefix:nr"
            #   - New (Issue #904): "prefix:r:t=tenant_id" or "prefix:nr:t=tenant_id"
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
                self._list_cache.pop(cache_key, None)

            # Invalidate all KV cache entries for this path
            kv_keys_to_invalidate = [(p, k) for (p, k) in list(self._kv_cache.keys()) if p == path]
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
            Dictionary with cache statistics including compact mode status
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
                from nexus.core.compact_metadata import get_pool_stats

                stats["intern_pools"] = get_pool_stats()

            return stats


# Sentinel object to distinguish "not in cache" from "cached as None"
_CACHE_MISS = object()
