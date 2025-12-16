"""In-memory caching layer for ReBAC permission checks.

This module provides a high-performance L1 cache for permission checks,
reducing latency from ~5ms (database) to <1ms (memory).

Architecture:
- L1 Cache (in-memory): This module - <1ms lookup, 10k entries
- L2 Cache (database): rebac_check_cache table - 5-10ms lookup
- L3 Compute: Graph traversal - 50-500ms
"""

import logging
import threading
import time
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
        >>> cache = ReBACPermissionCache(max_size=10000, ttl_seconds=60)
        >>> # Check cache
        >>> result = cache.get("agent", "alice", "read", "file", "/doc.txt")
        >>> if result is None:
        >>>     # Cache miss - compute permission
        >>>     result = compute_permission(...)
        >>>     cache.set("agent", "alice", "read", "file", "/doc.txt", result)
    """

    def __init__(
        self,
        max_size: int = 10000,
        ttl_seconds: int = 60,
        enable_metrics: bool = True,
        enable_adaptive_ttl: bool = False,
    ):
        """
        Initialize ReBAC permission cache.

        Args:
            max_size: Maximum number of entries (default: 10k)
            ttl_seconds: Time-to-live for cache entries (default: 60s)
            enable_metrics: Track hit rates and latency (default: True)
            enable_adaptive_ttl: Adjust TTL based on write frequency (default: False)
        """
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._enable_metrics = enable_metrics
        self._enable_adaptive_ttl = enable_adaptive_ttl
        self._lock = threading.RLock()

        # Main cache: key -> (result, timestamp)
        # Key format: "subject_type:subject_id:permission:object_type:object_id:tenant_id"
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=max_size, ttl=ttl_seconds)

        # Metrics tracking
        self._hits = 0
        self._misses = 0
        self._sets = 0
        self._invalidations = 0
        self._total_lookup_time_ms = 0.0  # Total time spent on lookups
        self._lookup_count = 0  # Number of lookups

        # Write frequency tracking for adaptive TTL
        # Maps object path -> (write_count, last_reset_time)
        self._write_frequency: dict[str, tuple[int, float]] = {}
        self._write_frequency_window = 300.0  # 5-minute window

    def _make_key(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str | None = None,
    ) -> str:
        """Create cache key from permission check parameters.

        Args:
            subject_type: Type of subject (e.g., "agent", "user", "group")
            subject_id: Subject identifier
            permission: Permission to check (e.g., "read", "write")
            object_type: Type of object (e.g., "file", "memory")
            object_id: Object identifier (e.g., path)
            tenant_id: Optional tenant ID for multi-tenant isolation

        Returns:
            Cache key string
        """
        tenant_part = tenant_id if tenant_id else "default"
        return f"{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}:{tenant_part}"

    def get(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str | None = None,
    ) -> bool | None:
        """
        Get cached permission check result.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            tenant_id: Optional tenant ID

        Returns:
            True/False if cached, None if not cached or expired
        """
        start_time = time.perf_counter()
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )

        with self._lock:
            result = self._cache.get(key)

            # Track metrics
            if self._enable_metrics:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._total_lookup_time_ms += elapsed_ms
                self._lookup_count += 1

                if result is not None:
                    self._hits += 1
                else:
                    self._misses += 1

            return result

    def set(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        result: bool,
        tenant_id: str | None = None,
    ) -> None:
        """
        Cache permission check result.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            result: Permission check result (True/False)
            tenant_id: Optional tenant ID
        """
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )

        with self._lock:
            # Note: Adaptive TTL is not currently used because TTLCache doesn't support per-item TTL
            # We use the cache's global TTL setting instead
            self._cache[key] = result

            if self._enable_metrics:
                self._sets += 1

    def invalidate_subject(
        self, subject_type: str, subject_id: str, tenant_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for a specific subject.

        Used when subject's permissions change (e.g., user added to group).

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            tenant_id: Optional tenant ID

        Returns:
            Number of entries invalidated
        """
        tenant_part = tenant_id if tenant_id else "default"
        prefix = f"{subject_type}:{subject_id}:"

        with self._lock:
            keys_to_delete = [
                key
                for key in list(self._cache.keys())
                if key.startswith(prefix) and key.endswith(f":{tenant_part}")
            ]

            for key in keys_to_delete:
                del self._cache[key]

            if self._enable_metrics:
                self._invalidations += len(keys_to_delete)

            logger.debug(
                f"L1 cache: Invalidated {len(keys_to_delete)} entries for subject {subject_type}:{subject_id}"
            )
            return len(keys_to_delete)

    def invalidate_object(
        self, object_type: str, object_id: str, tenant_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for a specific object.

        Used when object's permissions change (e.g., file access granted).

        Args:
            object_type: Type of object
            object_id: Object identifier
            tenant_id: Optional tenant ID

        Returns:
            Number of entries invalidated
        """
        tenant_part = tenant_id if tenant_id else "default"
        # Need to search for entries with this object
        # Key format: "subject_type:subject_id:permission:object_type:object_id:tenant_id"
        object_suffix = f"{object_type}:{object_id}:{tenant_part}"

        with self._lock:
            keys_to_delete = [
                key for key in list(self._cache.keys()) if key.endswith(object_suffix)
            ]

            for key in keys_to_delete:
                del self._cache[key]

            if self._enable_metrics:
                self._invalidations += len(keys_to_delete)

            logger.debug(
                f"L1 cache: Invalidated {len(keys_to_delete)} entries for object {object_type}:{object_id}"
            )
            return len(keys_to_delete)

    def invalidate_subject_object_pair(
        self,
        subject_type: str,
        subject_id: str,
        object_type: str,
        object_id: str,
        tenant_id: str | None = None,
    ) -> int:
        """
        Invalidate cache entries for a specific subject-object pair.

        Most precise invalidation - only affects permissions between this subject and object.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            object_type: Type of object
            object_id: Object identifier
            tenant_id: Optional tenant ID

        Returns:
            Number of entries invalidated
        """
        tenant_part = tenant_id if tenant_id else "default"
        # Match: "subject_type:subject_id:*:object_type:object_id:tenant_id"
        prefix = f"{subject_type}:{subject_id}:"
        suffix = f":{object_type}:{object_id}:{tenant_part}"

        with self._lock:
            keys_to_delete = [
                key
                for key in list(self._cache.keys())
                if key.startswith(prefix) and key.endswith(suffix)
            ]

            for key in keys_to_delete:
                del self._cache[key]

            if self._enable_metrics:
                self._invalidations += len(keys_to_delete)

            logger.debug(
                f"L1 cache: Invalidated {len(keys_to_delete)} entries for pair "
                f"{subject_type}:{subject_id} <-> {object_type}:{object_id}"
            )
            return len(keys_to_delete)

    def invalidate_object_prefix(
        self, object_type: str, object_id_prefix: str, tenant_id: str | None = None
    ) -> int:
        """
        Invalidate all cache entries for objects matching a prefix.

        Used for directory operations (e.g., invalidate all files under /workspace/).

        Args:
            object_type: Type of object
            object_id_prefix: Object ID prefix (e.g., "/workspace/")
            tenant_id: Optional tenant ID

        Returns:
            Number of entries invalidated
        """
        tenant_part = tenant_id if tenant_id else "default"
        # Need to check if object_id in key starts with prefix
        # Key format: "subject_type:subject_id:permission:object_type:object_id:tenant_id"

        with self._lock:
            keys_to_delete = []
            for key in list(self._cache.keys()):
                parts = key.split(":")
                if len(parts) >= 6:
                    key_object_type = parts[3]
                    # Join remaining parts except last (tenant_id)
                    key_object_id = ":".join(parts[4:-1])
                    key_tenant = parts[-1]

                    if (
                        key_object_type == object_type
                        and key_object_id.startswith(object_id_prefix)
                        and key_tenant == tenant_part
                    ):
                        keys_to_delete.append(key)

            for key in keys_to_delete:
                del self._cache[key]

            if self._enable_metrics:
                self._invalidations += len(keys_to_delete)

            logger.debug(
                f"L1 cache: Invalidated {len(keys_to_delete)} entries for prefix "
                f"{object_type}:{object_id_prefix}"
            )
            return len(keys_to_delete)

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
        """Clear all cache entries."""
        with self._lock:
            self._cache.clear()
            if self._enable_metrics:
                logger.info("L1 cache cleared")

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics including hit rate and latency
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0
            avg_lookup_time_ms = (
                (self._total_lookup_time_ms / self._lookup_count) if self._lookup_count > 0 else 0.0
            )

            return {
                "max_size": self._max_size,
                "current_size": len(self._cache),
                "ttl_seconds": self._ttl_seconds,
                "hits": self._hits,
                "misses": self._misses,
                "sets": self._sets,
                "invalidations": self._invalidations,
                "hit_rate_percent": round(hit_rate, 2),
                "total_requests": total_requests,
                "avg_lookup_time_ms": round(avg_lookup_time_ms, 3),
                "enable_metrics": self._enable_metrics,
                "enable_adaptive_ttl": self._enable_adaptive_ttl,
            }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._sets = 0
            self._invalidations = 0
            self._total_lookup_time_ms = 0.0
            self._lookup_count = 0
            logger.info("L1 cache stats reset")
