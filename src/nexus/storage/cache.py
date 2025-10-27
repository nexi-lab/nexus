"""In-memory caching layer for Nexus metadata operations.

This module provides thread-safe in-memory caching to reduce database queries
and improve performance for frequently accessed metadata.
"""

import threading
from typing import Any

from cachetools import LRUCache, TTLCache

from nexus.core.metadata import FileMetadata


class MetadataCache:
    """
    Multi-level in-memory cache for metadata operations.

    Provides separate caches for different access patterns:
    - Path metadata cache: Caches get() results
    - Directory listing cache: Caches list() results
    - File metadata KV cache: Caches get_file_metadata() results
    - Existence cache: Caches exists() results
    """

    def __init__(
        self,
        path_cache_size: int = 512,
        list_cache_size: int = 128,
        kv_cache_size: int = 256,
        exists_cache_size: int = 1024,
        ttl_seconds: int | None = None,
    ):
        """
        Initialize metadata cache.

        Args:
            path_cache_size: Maximum entries for path metadata cache
            list_cache_size: Maximum entries for directory listing cache
            kv_cache_size: Maximum entries for file metadata KV cache
            exists_cache_size: Maximum entries for existence check cache
            ttl_seconds: Time-to-live for cache entries in seconds (None = no expiry)
        """
        self._ttl_seconds = ttl_seconds
        self._lock = threading.RLock()

        # Cache for path metadata (get operation)
        if ttl_seconds:
            self._path_cache: (
                LRUCache[str, FileMetadata | None] | TTLCache[str, FileMetadata | None]
            ) = TTLCache(maxsize=path_cache_size, ttl=ttl_seconds)
        else:
            self._path_cache = LRUCache(maxsize=path_cache_size)

        # Cache for directory listings (list operation)
        if ttl_seconds:
            self._list_cache: (
                LRUCache[str, list[FileMetadata]] | TTLCache[str, list[FileMetadata]]
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
        """
        with self._lock:
            # Use object() as sentinel to distinguish "not in cache" from "cached as None"
            result: FileMetadata | None | object = self._path_cache.get(path, _CACHE_MISS)
            return result

    def set_path(self, path: str, metadata: FileMetadata | None) -> None:
        """
        Cache path metadata.

        Args:
            path: Virtual path
            metadata: File metadata (None if path doesn't exist)
        """
        with self._lock:
            self._path_cache[path] = metadata

    def get_list(self, prefix: str) -> list[FileMetadata] | None:
        """
        Get cached directory listing.

        Args:
            prefix: Path prefix

        Returns:
            List of FileMetadata if cached, None if not cached
        """
        with self._lock:
            result: list[FileMetadata] | None = self._list_cache.get(prefix)
            return result

    def set_list(self, prefix: str, files: list[FileMetadata]) -> None:
        """
        Cache directory listing.

        Args:
            prefix: Path prefix
            files: List of file metadata
        """
        with self._lock:
            self._list_cache[prefix] = files

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
            # Cache keys are in format "prefix:r" or "prefix:nr" where prefix is the path prefix
            cache_keys_to_invalidate = []
            for cache_key in list(self._list_cache.keys()):
                # Extract prefix from cache key (format: "prefix:r" or "prefix:nr")
                # Split by last ":" to handle paths that contain ":"
                if ":r" in cache_key or ":nr" in cache_key:
                    # Find the last occurrence of :r or :nr
                    if cache_key.endswith(":r"):
                        prefix = cache_key[:-2]  # Remove ":r"
                    elif cache_key.endswith(":nr"):
                        prefix = cache_key[:-3]  # Remove ":nr"
                    else:
                        # Fallback: treat the whole key as prefix
                        prefix = cache_key
                else:
                    prefix = cache_key

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
            Dictionary with cache statistics
        """
        with self._lock:
            return {
                "path_cache_size": len(self._path_cache),
                "list_cache_size": len(self._list_cache),
                "kv_cache_size": len(self._kv_cache),
                "exists_cache_size": len(self._exists_cache),
                "path_cache_maxsize": self._path_cache.maxsize,
                "list_cache_maxsize": self._list_cache.maxsize,
                "kv_cache_maxsize": self._kv_cache.maxsize,
                "exists_cache_maxsize": self._exists_cache.maxsize,
                "ttl_seconds": self._ttl_seconds,
            }


# Sentinel object to distinguish "not in cache" from "cached as None"
_CACHE_MISS = object()
