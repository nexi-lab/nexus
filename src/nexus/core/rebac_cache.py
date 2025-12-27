"""In-memory caching layer for ReBAC permission checks.

This module provides a high-performance L1 cache for permission checks,
reducing latency from ~5ms (database) to <1ms (memory).

Architecture:
- L1 Cache (in-memory): This module - <1ms lookup, 10k entries
- L2 Cache (database): rebac_check_cache table - 5-10ms lookup
- L3 Compute: Graph traversal - 50-500ms

Quantization (Issue #842):
- Cache keys include a time bucket for distributed cache sharing
- Multiple instances can independently generate the same key within a window
- Based on SpiceDB/Google Zanzibar quantization approach
- See: https://authzed.com/blog/how-caching-works-in-spicedb
"""

import logging
import threading
import time
from collections.abc import Callable
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
        >>> cache = ReBACPermissionCache(max_size=10000, ttl_seconds=300)
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
        ttl_seconds: int = 300,
        denial_ttl_seconds: int = 60,
        enable_metrics: bool = True,
        enable_adaptive_ttl: bool = False,
        quantization_interval: int = 5,
    ):
        """
        Initialize ReBAC permission cache.

        Args:
            max_size: Maximum number of entries (default: 10k)
            ttl_seconds: Time-to-live for grant (True) cache entries (default: 300s)
            denial_ttl_seconds: Time-to-live for denial (False) cache entries (default: 60s)
                Shorter TTL for denials ensures revoked access is reflected quickly (Issue #877)
            enable_metrics: Track hit rates and latency (default: True)
            enable_adaptive_ttl: Adjust TTL based on write frequency (default: False)
            quantization_interval: Time bucket size in seconds for cache key quantization
                (default: 5s). Enables distributed cache sharing. Set to 0 to disable.
        """
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._denial_ttl_seconds = denial_ttl_seconds
        self._enable_metrics = enable_metrics
        self._enable_adaptive_ttl = enable_adaptive_ttl
        self._quantization_interval = quantization_interval
        self._lock = threading.RLock()

        # Split caches for grants and denials (Issue #877)
        # Denials use shorter TTL for security - revoked access should be reflected quickly
        # Key format: "subject_type:subject_id:permission:object_type:object_id:tenant_id:t{bucket}"
        grant_cache_size = max_size // 2  # Split capacity between grant and denial caches
        denial_cache_size = max_size - grant_cache_size
        self._grant_cache: TTLCache[str, bool] = TTLCache(maxsize=grant_cache_size, ttl=ttl_seconds)
        self._denial_cache: TTLCache[str, bool] = TTLCache(
            maxsize=denial_cache_size, ttl=denial_ttl_seconds
        )

        # Metrics tracking
        self._hits = 0
        self._misses = 0
        self._grant_hits = 0  # Issue #877: Track grant cache hits separately
        self._denial_hits = 0  # Issue #877: Track denial cache hits separately
        self._sets = 0
        self._grant_sets = 0  # Issue #877: Track grant cache sets
        self._denial_sets = 0  # Issue #877: Track denial cache sets
        self._invalidations = 0
        self._total_lookup_time_ms = 0.0  # Total time spent on lookups
        self._lookup_count = 0  # Number of lookups

        # Write frequency tracking for adaptive TTL
        # Maps object path -> (write_count, last_reset_time)
        self._write_frequency: dict[str, tuple[int, float]] = {}
        self._write_frequency_window = 300.0  # 5-minute window

        # Stampede prevention (Issue #878)
        # Prevents thundering herd when cache entries expire
        # Maps key -> Event that signals when computation is complete
        self._computing: dict[str, threading.Event] = {}
        self._stampede_waits = 0  # Number of requests that waited
        self._stampede_timeouts = 0  # Number of waits that timed out
        self._stampede_timeout_seconds = 5.0  # Max time to wait for computation

    def _get_time_bucket(self) -> int:
        """Get current time bucket for cache key quantization."""
        if self._quantization_interval <= 0:
            return 0
        return int(time.time() // self._quantization_interval)

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
            Cache key string with time bucket for distributed cache sharing
        """
        tenant_part = tenant_id if tenant_id else "default"
        time_bucket = self._get_time_bucket()
        return f"{subject_type}:{subject_id}:{permission}:{object_type}:{object_id}:{tenant_part}:t{time_bucket}"

    def _parse_key(self, key: str) -> tuple[str, str, str, str, str, str] | None:
        """Parse a cache key into components (excluding time bucket).

        Returns:
            Tuple of (subject_type, subject_id, permission, object_type, object_id, tenant_id)
            or None if key format is invalid.
        """
        parts = key.split(":")
        if len(parts) < 7:
            return None
        subject_type = parts[0]
        subject_id = parts[1]
        permission = parts[2]
        object_type = parts[3]
        tenant_id = parts[-2]
        object_id = ":".join(parts[4:-2])
        return (subject_type, subject_id, permission, object_type, object_id, tenant_id)

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
            # Check grant cache first (Issue #877)
            result = self._grant_cache.get(key)
            is_grant_hit = result is not None

            # If not in grant cache, check denial cache
            if result is None:
                result = self._denial_cache.get(key)

            # Track metrics
            if self._enable_metrics:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                self._total_lookup_time_ms += elapsed_ms
                self._lookup_count += 1

                if result is not None:
                    self._hits += 1
                    if is_grant_hit:
                        self._grant_hits += 1
                    else:
                        self._denial_hits += 1
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

        Grants (True) are cached with longer TTL, denials (False) with shorter TTL.
        This ensures revoked access is reflected quickly while maximizing cache benefit
        for allowed operations (Issue #877).

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
            # Route to appropriate cache based on result (Issue #877)
            # Grants get longer TTL, denials get shorter TTL for security
            if result:
                self._grant_cache[key] = result
                if self._enable_metrics:
                    self._grant_sets += 1
            else:
                self._denial_cache[key] = result
                if self._enable_metrics:
                    self._denial_sets += 1

            if self._enable_metrics:
                self._sets += 1

    # ============================================================
    # Stampede Prevention (Issue #878)
    # ============================================================

    def try_acquire_compute(
        self,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str | None = None,
    ) -> tuple[bool, str]:
        """
        Try to acquire the right to compute a permission check.

        Used for stampede prevention. When multiple requests need the same
        permission check simultaneously, only the first one computes while
        others wait.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            tenant_id: Optional tenant ID

        Returns:
            Tuple of (should_compute, cache_key):
            - (True, key) if caller should compute and then call release_compute()
            - (False, key) if another request is computing, caller should call wait_for_compute()
        """
        key = self._make_key(
            subject_type, subject_id, permission, object_type, object_id, tenant_id
        )

        with self._lock:
            # Check if already being computed
            if key in self._computing:
                return (False, key)

            # We're the leader - set up the event for others to wait on
            self._computing[key] = threading.Event()
            return (True, key)

    def wait_for_compute(self, key: str) -> bool | None:
        """
        Wait for another request to finish computing a permission.

        Args:
            key: Cache key returned by try_acquire_compute()

        Returns:
            Cached result after computation completes, or None if timeout/not found
        """
        with self._lock:
            event = self._computing.get(key)
            if event is None:
                # Computation already finished, check cache
                # Parse key to get components for cache lookup
                parsed = self._parse_key(key)
                if parsed:
                    return self.get(
                        parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5]
                    )
                return None

        # Wait outside the lock
        if self._enable_metrics:
            with self._lock:
                self._stampede_waits += 1

        success = event.wait(timeout=self._stampede_timeout_seconds)

        if not success:
            # Timeout - computation took too long
            if self._enable_metrics:
                with self._lock:
                    self._stampede_timeouts += 1
            logger.warning(f"Stampede wait timeout for key: {key[:50]}...")
            return None

        # Computation finished, get result from cache
        with self._lock:
            parsed = self._parse_key(key)
            if parsed:
                return self.get(parsed[0], parsed[1], parsed[2], parsed[3], parsed[4], parsed[5])
            return None

    def release_compute(
        self,
        key: str,
        result: bool,
        subject_type: str,
        subject_id: str,
        permission: str,
        object_type: str,
        object_id: str,
        tenant_id: str | None = None,
    ) -> None:
        """
        Release compute lock and cache the result.

        Must be called after try_acquire_compute() returns (True, key),
        even if computation failed.

        Args:
            key: Cache key returned by try_acquire_compute()
            result: Computed permission result
            subject_type: Type of subject
            subject_id: Subject identifier
            permission: Permission to check
            object_type: Type of object
            object_id: Object identifier
            tenant_id: Optional tenant ID
        """
        with self._lock:
            # Cache the result first
            self.set(
                subject_type, subject_id, permission, object_type, object_id, result, tenant_id
            )

            # Signal waiting requests and clean up
            event = self._computing.pop(key, None)
            if event:
                event.set()

    def cancel_compute(self, key: str) -> None:
        """
        Cancel a computation without caching a result.

        Use this if computation failed and you don't want to cache the error.

        Args:
            key: Cache key returned by try_acquire_compute()
        """
        with self._lock:
            event = self._computing.pop(key, None)
            if event:
                event.set()

    def _invalidate_from_both_caches(self, keys_to_delete: list[str]) -> int:
        """Helper to delete keys from both grant and denial caches."""
        count = 0
        for key in keys_to_delete:
            if key in self._grant_cache:
                del self._grant_cache[key]
                count += 1
            if key in self._denial_cache:
                del self._denial_cache[key]
                count += 1
        return count

    def _collect_matching_keys(
        self,
        match_fn: Callable[[str], bool],
    ) -> list[str]:
        """Collect keys from both caches matching a predicate."""
        keys = []
        for key in list(self._grant_cache.keys()):
            if match_fn(key):
                keys.append(key)
        for key in list(self._denial_cache.keys()):
            if match_fn(key) and key not in keys:
                keys.append(key)
        return keys

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

        with self._lock:

            def match_fn(key: str) -> bool:
                parsed = self._parse_key(key)
                return bool(
                    parsed
                    and parsed[0] == subject_type
                    and parsed[1] == subject_id
                    and parsed[5] == tenant_part
                )

            keys_to_delete = self._collect_matching_keys(match_fn)
            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for subject {subject_type}:{subject_id}"
            )
            return count

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

        with self._lock:

            def match_fn(key: str) -> bool:
                parsed = self._parse_key(key)
                return bool(
                    parsed
                    and parsed[3] == object_type
                    and parsed[4] == object_id
                    and parsed[5] == tenant_part
                )

            keys_to_delete = self._collect_matching_keys(match_fn)
            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for object {object_type}:{object_id}"
            )
            return count

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

        with self._lock:

            def match_fn(key: str) -> bool:
                parsed = self._parse_key(key)
                return bool(
                    parsed
                    and parsed[0] == subject_type
                    and parsed[1] == subject_id
                    and parsed[3] == object_type
                    and parsed[4] == object_id
                    and parsed[5] == tenant_part
                )

            keys_to_delete = self._collect_matching_keys(match_fn)
            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for pair "
                f"{subject_type}:{subject_id} <-> {object_type}:{object_id}"
            )
            return count

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

        with self._lock:

            def match_fn(key: str) -> bool:
                parsed = self._parse_key(key)
                return bool(
                    parsed
                    and parsed[3] == object_type
                    and parsed[4].startswith(object_id_prefix)
                    and parsed[5] == tenant_part
                )

            keys_to_delete = self._collect_matching_keys(match_fn)
            count = self._invalidate_from_both_caches(keys_to_delete)

            if self._enable_metrics:
                self._invalidations += count

            logger.debug(
                f"L1 cache: Invalidated {count} entries for prefix {object_type}:{object_id_prefix}"
            )
            return count

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
        """Clear all cache entries from both grant and denial caches."""
        with self._lock:
            self._grant_cache.clear()
            self._denial_cache.clear()
            if self._enable_metrics:
                logger.info("L1 cache cleared (grant and denial caches)")

    def get_stats(self) -> dict[str, Any]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics including hit rate, latency,
            and grant/denial breakdown (Issue #877)
        """
        with self._lock:
            total_requests = self._hits + self._misses
            hit_rate = (self._hits / total_requests * 100) if total_requests > 0 else 0.0
            avg_lookup_time_ms = (
                (self._total_lookup_time_ms / self._lookup_count) if self._lookup_count > 0 else 0.0
            )

            # Calculate grant/denial hit rates (Issue #877)
            grant_hit_rate = (self._grant_hits / self._hits * 100) if self._hits > 0 else 0.0
            denial_hit_rate = (self._denial_hits / self._hits * 100) if self._hits > 0 else 0.0

            return {
                "max_size": self._max_size,
                "current_size": len(self._grant_cache) + len(self._denial_cache),
                "grant_cache_size": len(self._grant_cache),
                "denial_cache_size": len(self._denial_cache),
                "ttl_seconds": self._ttl_seconds,
                "denial_ttl_seconds": self._denial_ttl_seconds,
                "quantization_interval": self._quantization_interval,
                "hits": self._hits,
                "grant_hits": self._grant_hits,
                "denial_hits": self._denial_hits,
                "grant_hit_rate_percent": round(grant_hit_rate, 2),
                "denial_hit_rate_percent": round(denial_hit_rate, 2),
                "misses": self._misses,
                "sets": self._sets,
                "grant_sets": self._grant_sets,
                "denial_sets": self._denial_sets,
                "invalidations": self._invalidations,
                "hit_rate_percent": round(hit_rate, 2),
                "total_requests": total_requests,
                "avg_lookup_time_ms": round(avg_lookup_time_ms, 3),
                "enable_metrics": self._enable_metrics,
                "enable_adaptive_ttl": self._enable_adaptive_ttl,
                # Stampede prevention metrics (Issue #878)
                "stampede_waits": self._stampede_waits,
                "stampede_timeouts": self._stampede_timeouts,
                "stampede_active_computes": len(self._computing),
            }

    def reset_stats(self) -> None:
        """Reset metrics counters."""
        with self._lock:
            self._hits = 0
            self._misses = 0
            self._grant_hits = 0
            self._denial_hits = 0
            self._sets = 0
            self._grant_sets = 0
            self._denial_sets = 0
            self._invalidations = 0
            self._total_lookup_time_ms = 0.0
            self._lookup_count = 0
            self._stampede_waits = 0
            self._stampede_timeouts = 0
            logger.info("L1 cache stats reset")
