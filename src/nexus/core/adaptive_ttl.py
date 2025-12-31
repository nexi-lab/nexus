"""Adaptive TTL module for write-frequency-based cache expiration.

This module provides shared adaptive TTL logic that can be used by any cache
to adjust TTL based on write frequency. High-write objects get shorter TTL
(fresher data), stable objects get longer TTL (better cache efficiency).

Issue #715: Extracted from ReBAC cache for reuse across content caches.

Usage:
    class MyCache(AdaptiveTTLMixin):
        def __init__(self):
            super().__init__(base_ttl=300)

        def on_write(self, path: str):
            self.track_write(path)

        def get_ttl_for(self, path: str) -> int:
            return self.get_adaptive_ttl(path)

References:
    - SpiceDB caching: https://authzed.com/blog/how-caching-works-in-spicedb
    - ACM research on TTL strategies: https://dl.acm.org/doi/10.1145/2505515.2507886
"""

import threading
import time
from typing import Any


class AdaptiveTTLMixin:
    """Mixin providing adaptive TTL based on write frequency.

    Tracks write operations per key and adjusts TTL accordingly:
    - Very high write rate (>10/min): TTL = base_ttl / 6 (min 10s)
    - High write rate (>5/min): TTL = base_ttl / 3 (min 30s)
    - Moderate write rate (>1/min): TTL = base_ttl / 2 (min 60s)
    - Low write rate: TTL = base_ttl * 2 (max 5 min)

    Thread-safe implementation with minimal overhead.
    """

    def __init__(
        self,
        base_ttl: int = 300,
        window_seconds: float = 300.0,
        enable_adaptive_ttl: bool = True,
        min_ttl: int = 10,
        max_ttl: int = 600,
    ):
        """Initialize adaptive TTL tracking.

        Args:
            base_ttl: Base TTL in seconds (default: 300s = 5 min)
            window_seconds: Sliding window for write frequency (default: 300s)
            enable_adaptive_ttl: Enable/disable adaptive TTL (default: True)
            min_ttl: Minimum TTL in seconds (default: 10s)
            max_ttl: Maximum TTL in seconds (default: 600s = 10 min)
        """
        self._base_ttl = base_ttl
        self._window_seconds = window_seconds
        self._enable_adaptive_ttl = enable_adaptive_ttl
        self._min_ttl = min_ttl
        self._max_ttl = max_ttl

        # Maps key -> (write_count, window_start_time)
        self._write_frequency: dict[str, tuple[int, float]] = {}
        self._write_frequency_lock = threading.Lock()

        # Metrics with explicit typing
        self._total_writes_tracked: int = 0
        self._ttl_adjustments: dict[str, int] = {
            "very_high": 0,
            "high": 0,
            "moderate": 0,
            "low": 0,
        }

    def track_write(self, key: str) -> None:
        """Track a write operation for adaptive TTL calculation.

        Should be called whenever the cached object is written/modified.
        Thread-safe with minimal lock contention.

        Args:
            key: Cache key (e.g., file path, object ID)
        """
        if not self._enable_adaptive_ttl:
            return

        current_time = time.time()

        with self._write_frequency_lock:
            if key in self._write_frequency:
                count, window_start = self._write_frequency[key]

                # Reset counter if outside window
                if current_time - window_start > self._window_seconds:
                    self._write_frequency[key] = (1, current_time)
                else:
                    self._write_frequency[key] = (count + 1, window_start)
            else:
                self._write_frequency[key] = (1, current_time)

            self._total_writes_tracked += 1

    def track_write_bulk(self, keys: list[str]) -> None:
        """Track write operations for multiple keys.

        More efficient than calling track_write() in a loop.

        Args:
            keys: List of cache keys
        """
        if not self._enable_adaptive_ttl or not keys:
            return

        current_time = time.time()

        with self._write_frequency_lock:
            for key in keys:
                if key in self._write_frequency:
                    count, window_start = self._write_frequency[key]

                    if current_time - window_start > self._window_seconds:
                        self._write_frequency[key] = (1, current_time)
                    else:
                        self._write_frequency[key] = (count + 1, window_start)
                else:
                    self._write_frequency[key] = (1, current_time)

            self._total_writes_tracked += len(keys)

    def get_adaptive_ttl(self, key: str) -> int:
        """Get TTL adjusted for write frequency.

        Args:
            key: Cache key to get TTL for

        Returns:
            TTL in seconds, adjusted based on observed write frequency
        """
        if not self._enable_adaptive_ttl:
            return self._base_ttl

        with self._write_frequency_lock:
            if key not in self._write_frequency:
                return self._base_ttl

            count, window_start = self._write_frequency[key]
            current_time = time.time()

            # If outside window, use default TTL
            elapsed = current_time - window_start
            if elapsed > self._window_seconds:
                return self._base_ttl

            # Calculate writes per minute
            elapsed_minutes = max(elapsed / 60.0, 1 / 60.0)  # At least 1 second
            writes_per_minute = count / elapsed_minutes

            # Adaptive TTL based on write frequency
            if writes_per_minute > 10:  # Very high write rate
                ttl = max(self._min_ttl, self._base_ttl // 6)
                self._ttl_adjustments["very_high"] += 1
            elif writes_per_minute > 5:  # High write rate
                ttl = max(self._min_ttl * 3, self._base_ttl // 3)
                self._ttl_adjustments["high"] += 1
            elif writes_per_minute > 1:  # Moderate write rate
                ttl = max(self._min_ttl * 6, self._base_ttl // 2)
                self._ttl_adjustments["moderate"] += 1
            else:  # Low write rate - extend TTL
                ttl = min(self._max_ttl, self._base_ttl * 2)
                self._ttl_adjustments["low"] += 1

            return ttl

    def get_write_frequency(self, key: str) -> float:
        """Get current write frequency for a key.

        Args:
            key: Cache key

        Returns:
            Writes per minute, or 0.0 if no writes tracked
        """
        with self._write_frequency_lock:
            if key not in self._write_frequency:
                return 0.0

            count, window_start = self._write_frequency[key]
            current_time = time.time()
            elapsed = current_time - window_start

            if elapsed > self._window_seconds:
                return 0.0

            elapsed_minutes = max(elapsed / 60.0, 1 / 60.0)
            return count / elapsed_minutes

    def clear_write_frequency(self, key: str | None = None) -> None:
        """Clear write frequency tracking.

        Args:
            key: Specific key to clear, or None to clear all
        """
        with self._write_frequency_lock:
            if key is None:
                self._write_frequency.clear()
            else:
                self._write_frequency.pop(key, None)

    def cleanup_stale_entries(self) -> int:
        """Remove stale write frequency entries outside the window.

        Should be called periodically to prevent memory growth.

        Returns:
            Number of entries removed
        """
        current_time = time.time()
        removed = 0

        with self._write_frequency_lock:
            stale_keys = [
                key
                for key, (_, window_start) in self._write_frequency.items()
                if current_time - window_start > self._window_seconds * 2
            ]

            for key in stale_keys:
                del self._write_frequency[key]
                removed += 1

        return removed

    def get_adaptive_ttl_stats(self) -> dict[str, Any]:
        """Get adaptive TTL statistics.

        Returns:
            Dictionary with tracking statistics
        """
        with self._write_frequency_lock:
            return {
                "enabled": self._enable_adaptive_ttl,
                "base_ttl": self._base_ttl,
                "min_ttl": self._min_ttl,
                "max_ttl": self._max_ttl,
                "window_seconds": self._window_seconds,
                "tracked_keys": len(self._write_frequency),
                "total_writes_tracked": self._total_writes_tracked,
                "ttl_adjustments": dict(self._ttl_adjustments),
            }
