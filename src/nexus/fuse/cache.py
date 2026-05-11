"""Cache implementations for FUSE mount performance optimization.

This module provides caching layers for file attributes, content, and parsed
content to optimize FUSE filesystem operations and reduce latency.
"""

import logging
import threading
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from nexus.cache.file_store import FileKey, MemoryFileCache
from nexus.cache.index_store import IndexKey, MemoryIndexCache
from nexus.cache.policy import index_ttl_for_backend as _policy_index_ttl_for_backend

logger = logging.getLogger(__name__)


def _stat_key(path: str, scope_id: str = "default") -> IndexKey:
    return IndexKey("fuse", scope_id, path, "stat")


def _listing_key(path: str, scope_id: str = "default") -> IndexKey:
    return IndexKey("fuse", scope_id, path, "listing")


def _parent_path(path: str) -> str:
    return str(PurePosixPath(path).parent) or "/"


def _file_key(path: str, namespace: str = "raw") -> FileKey:
    return FileKey("fuse", "default", path, namespace)


def _parsed_key(path: str, view_type: str) -> FileKey:
    return _file_key(path, f"parsed:{view_type}")


class FUSECacheManager:
    """Manages caching for FUSE operations.

    This class provides three types of caches:
    1. Attribute cache (TTL-based): Caches getattr() results
    2. Content cache (LRU-based): Caches raw file content
    3. Parsed cache (LRU-based): Caches parsed file content

    All caches are thread-safe and support invalidation on write/delete operations.

    Example:
        >>> cache_mgr = FUSECacheManager(
        ...     content_cache_bytes=512 * 1024 * 1024,
        ...     parsed_cache_bytes=64 * 1024 * 1024,
        ...     max_drain_bytes=16 * 1024 * 1024,
        ...     attr_cache_ttl=60,
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
        *,
        content_cache_bytes: int = 512 * 1024 * 1024,
        parsed_cache_bytes: int = 64 * 1024 * 1024,
        max_drain_bytes: int = 16 * 1024 * 1024,
        attr_cache_ttl: int = 60,
        listing_cache_ttl: int | None = None,
        index_ttl_overrides: Mapping[str, int] | None = None,
        enable_metrics: bool = False,
    ) -> None:
        """Initialize cache manager.

        Args:
            content_cache_bytes: Logical budget for raw content (default: 512 MiB).
                Combined with ``parsed_cache_bytes`` to size the underlying
                ``MemoryFileCache`` byte budget.
            parsed_cache_bytes: Logical budget for parsed/derived content
                (default: 64 MiB).
            max_drain_bytes: Hard cap (per-call) on bytes that
                :meth:`cache_content` will accept. Larger payloads are dropped
                (logged + metric-counted) to bound a single drain.
            attr_cache_ttl: TTL for attribute cache in seconds (default: 60)
            listing_cache_ttl: TTL for directory listings in seconds (defaults to attr TTL)
            index_ttl_overrides: Optional per-backend overrides for
                index-cache TTLs (passed through to
                :func:`nexus.cache.policy.index_ttl_for_backend`).
            enable_metrics: If True, track cache hit/miss metrics
        """
        total_file_bytes = content_cache_bytes + parsed_cache_bytes
        if max_drain_bytes > total_file_bytes:
            raise ValueError(
                f"max_drain_bytes ({max_drain_bytes}) must not exceed "
                f"content_cache_bytes + parsed_cache_bytes ({total_file_bytes})"
            )
        self._attr_ttl = attr_cache_ttl
        self._listing_ttl = attr_cache_ttl if listing_cache_ttl is None else listing_cache_ttl
        self._ttl_overrides: dict[str, int] = dict(index_ttl_overrides or {})

        self._index_cache = MemoryIndexCache()
        self._file_cache = MemoryFileCache(max_bytes=total_file_bytes)
        self._max_drain_bytes = max_drain_bytes

        # Thread safety
        self._index_lock = threading.RLock()
        self._file_lock = threading.RLock()

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
            "content_skipped_oversize": 0,
        }
        self._metrics_lock = threading.Lock()

    @property
    def max_drain_bytes(self) -> int:
        return self._max_drain_bytes

    def index_ttl_for_backend(self, backend_id: str) -> int:
        return _policy_index_ttl_for_backend(backend_id, self._ttl_overrides)

    async def content_lock(self, path: str, view_type: str | None = None) -> Any:
        """Per-path singleflight lock for content fetches.

        Callers wrap the L2/L3 fill in ``async with await coordinator.content_lock(path):``
        to ensure exactly one backend fetch under concurrent cold reads for the same path.
        """
        namespace = "raw" if view_type is None else f"parsed:{view_type}"
        return await self._file_cache.lock(_file_key(path, namespace))

    def _resolve_index_ttl(self, backend_id: str | None, *, default: int) -> int:
        """Pick a TTL for a metadata write.

        When ``backend_id`` is known, defer to the policy table (which
        consults ``index_ttl_overrides`` first, then per-backend defaults
        from ``INDEX_TTL_BY_BACKEND``). When the backend cannot be resolved,
        fall back to the caller's generic default.
        """
        if backend_id is None:
            return default
        return _policy_index_ttl_for_backend(backend_id, self._ttl_overrides)

    # ============================================================
    # Attribute Cache
    # ============================================================

    def _index_size(self, kind: str | None = None) -> int:
        if kind is None:
            return len(self._index_cache._entries)
        return sum(1 for key in self._index_cache._entries if key.kind == kind)

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

        if self._enable_metrics:
            with self._metrics_lock:
                if result is not None:
                    self._metrics["attr_hits"] += 1
                else:
                    self._metrics["attr_misses"] += 1

        return result

    def cache_attr(
        self,
        path: str,
        attrs: dict[str, Any],
        scope_id: str = "default",
        backend_id: str | None = None,
    ) -> None:
        """Cache file attributes.

        Args:
            path: File path
            attrs: Attributes dictionary to cache
            backend_id: Optional backend id; when supplied, per-backend TTL
                overrides from ``index_ttl_overrides`` apply.
        """
        ttl = self._resolve_index_ttl(backend_id, default=self._attr_ttl)
        key = _stat_key(path, scope_id)
        with self._index_lock:
            self._index_cache.put(key, attrs, ttl_seconds=ttl)

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
        if result is None:
            return None
        return list(result)

    def cache_listing(
        self,
        path: str,
        entries: list[str],
        scope_id: str = "default",
        backend_id: str | None = None,
    ) -> None:
        """Cache directory entries.

        Args:
            path: Directory path
            entries: Directory entry names to cache
            backend_id: Optional backend id; when supplied, per-backend TTL
                overrides from ``index_ttl_overrides`` apply.
        """
        ttl = self._resolve_index_ttl(backend_id, default=self._listing_ttl)
        key = _listing_key(path, scope_id)
        with self._index_lock:
            self._index_cache.put(
                key,
                list(entries),
                ttl_seconds=ttl,
            )

    def invalidate_parent_listing(self, path: str, scope_id: str = "default") -> None:
        """Invalidate only the immediate parent directory listing for a path."""
        key = _listing_key(_parent_path(path), scope_id)
        with self._index_lock:
            self._index_cache.invalidate_path(key)

    # ============================================================
    # Content Cache
    # ============================================================

    def _file_size(self, namespace_prefix: str | None = None) -> int:
        if namespace_prefix is None:
            return len(self._file_cache._entries)
        return sum(
            1 for key in self._file_cache._entries if key.namespace.startswith(namespace_prefix)
        )

    def get_content(self, path: str, expected_fingerprint: str | None = None) -> bytes | None:
        """Get cached file content.

        Args:
            path: File path

        Returns:
            Cached content or None if not cached
        """
        key = _file_key(path)
        with self._file_lock:
            result = self._file_cache.get_sync(key, expected_fingerprint)

        if self._enable_metrics:
            with self._metrics_lock:
                if result is not None:
                    self._metrics["content_hits"] += 1
                else:
                    self._metrics["content_misses"] += 1

        return result

    def cache_content(
        self,
        path: str,
        content: bytes,
        *,
        fingerprint: str | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        """Cache file content.

        Args:
            path: File path
            content: File content to cache
            fingerprint: Optional content fingerprint for staleness checks.
            ttl_seconds: Optional explicit TTL; defaults to ``attr_cache_ttl``
                when no fingerprint is provided.

        Payloads larger than ``max_drain_bytes`` are dropped (logged + metric
        ``content_skipped_oversize``) to bound any single drain.
        """
        if len(content) > self._max_drain_bytes:
            logger.warning(
                "FUSECacheManager skipping oversize content: path=%s size=%d max_drain=%d",
                path,
                len(content),
                self._max_drain_bytes,
            )
            if self._enable_metrics:
                with self._metrics_lock:
                    self._metrics["content_skipped_oversize"] += 1
            # Don't leave stale bytes behind: drop any prior cached entry +
            # parsed views for this path so callers fall back to origin.
            self.invalidate_file(path)
            return
        key = _file_key(path)
        if fingerprint is None and ttl_seconds is None:
            ttl_seconds = self._attr_ttl
        with self._file_lock:
            self._file_cache.put_sync(key, content, fingerprint, ttl_seconds)

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
        key = _parsed_key(path, view_type)
        with self._file_lock:
            result = self._file_cache.get_sync(key, expected_fingerprint=None)

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
        key = _parsed_key(path, view_type)
        with self._file_lock:
            result = self._file_cache.get_sync(key, expected_fingerprint=None)
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
        key = _parsed_key(path, view_type)
        with self._file_lock:
            self._file_cache.put_sync(
                key,
                content,
                fingerprint=None,
                ttl_seconds=self._attr_ttl,
            )

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

        self.invalidate_file(path)

    def invalidate_path_all_scopes(self, path: str) -> None:
        """Invalidate stat entries for a path across all logical scopes."""
        with self._index_lock:
            keys = [
                key
                for key in list(self._index_cache._entries)
                if key.path == path and key.kind in {"stat", "negative"}
            ]
            for key in keys:
                self._index_cache.invalidate_path(key)

        self.invalidate_file(path)

    def invalidate_file(self, path: str, namespace: str | None = None) -> None:
        """Invalidate raw content or all file-content views for a path."""
        with self._file_lock:
            if namespace is not None:
                key = _file_key(path, namespace)
                self._file_cache.invalidate_sync(key)
            else:
                self._file_cache.invalidate_path_sync(path)

        if self._enable_metrics:
            with self._metrics_lock:
                self._metrics["invalidations"] += 1

    def invalidate_all(self) -> None:
        """Invalidate all caches.

        This is useful for testing or when you need to clear all cached data.
        """
        with self._index_lock:
            self._index_cache = MemoryIndexCache()

        with self._file_lock:
            self._file_cache = MemoryFileCache(
                max_bytes=self._file_cache.max_bytes,
            )

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
                    "content": self._file_size("raw"),
                    "parsed": self._file_size("parsed:"),
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
