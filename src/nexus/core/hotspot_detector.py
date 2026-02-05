"""Hotspot Detection and Proactive Cache Prefetching (Issue #921).

Detects and prioritizes hot permission paths based on actual access patterns,
similar to Google Zanzibar Section 3.2.5 hot spot handling.

Key concepts:
- HotspotDetector: Tracks access frequency with rolling window
- HotspotPrefetcher: Background worker that proactively warms hot cache entries

References:
    - Zanzibar Paper Section 3.2.5: Hot spot handling
    - SpiceDB: Consistent hash routing for cache locality
    - JuiceFS: --prefetch for read patterns
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.tiger_cache import TigerCache, TigerCacheUpdater

logger = logging.getLogger(__name__)


@dataclass
class HotspotConfig:
    """Configuration for hotspot detection and prefetching.

    Attributes:
        enabled: Enable/disable hotspot detection
        window_seconds: Rolling window for access counting (default: 300s = 5min)
        hot_threshold: Accesses per window to be considered "hot" (default: 50)
        prefetch_before_expiry_seconds: Prefetch N seconds before TTL expires (default: 30s)
        max_prefetch_batch: Max entries to prefetch per cycle (default: 10)
        prefetch_interval_seconds: How often to run prefetch cycle (default: 10s)
        cleanup_interval_seconds: How often to cleanup stale entries (default: 60s)
    """

    enabled: bool = True
    window_seconds: int = 300
    hot_threshold: int = 50
    prefetch_before_expiry_seconds: int = 30
    max_prefetch_batch: int = 10
    prefetch_interval_seconds: int = 10
    cleanup_interval_seconds: int = 60


@dataclass
class HotspotEntry:
    """A hot permission path entry."""

    subject_type: str
    subject_id: str
    resource_type: str
    permission: str
    zone_id: str
    access_count: int
    last_access: float

    def cache_key_tuple(self) -> tuple[str, str, str, str, str]:
        """Return tuple for cache key lookup."""
        return (
            self.subject_type,
            self.subject_id,
            self.permission,
            self.resource_type,
            self.zone_id,
        )


class HotspotDetector:
    """Track and detect frequently accessed permission paths.

    Inspired by:
    - Zanzibar Section 3.2.5: Hot spot handling
    - SpiceDB: Consistent hash routing for cache locality
    - JuiceFS: --prefetch for read patterns

    Thread-safe implementation with minimal overhead.
    """

    def __init__(
        self,
        config: HotspotConfig | None = None,
    ):
        """Initialize hotspot detector.

        Args:
            config: Hotspot configuration (uses defaults if not provided)
        """
        self._config = config or HotspotConfig()

        # Rolling window counters: (subject_type, subject_id, resource_type, permission, zone_id) -> timestamps
        self._access_log: dict[tuple[str, str, str, str, str], list[float]] = {}
        self._lock = threading.RLock()

        # Metrics
        self._total_accesses: int = 0
        self._hot_entries_detected: int = 0
        self._prefetches_triggered: int = 0

    @property
    def config(self) -> HotspotConfig:
        """Get current configuration."""
        return self._config

    def record_access(
        self,
        subject_type: str,
        subject_id: str,
        resource_type: str,
        permission: str,
        zone_id: str = "default",
    ) -> None:
        """Record a permission check access.

        Low-overhead operation designed to be called on every permission check.
        Uses rolling window to track access frequency.

        Args:
            subject_type: Type of subject (e.g., "user", "agent")
            subject_id: Subject identifier
            resource_type: Type of resource (e.g., "file")
            permission: Permission being checked (e.g., "read", "write")
            zone_id: Tenant identifier
        """
        if not self._config.enabled:
            return

        key = (subject_type, subject_id, resource_type, permission, zone_id)
        now = time.time()
        cutoff = now - self._config.window_seconds

        with self._lock:
            if key not in self._access_log:
                self._access_log[key] = []

            # Add timestamp and prune old entries in one pass
            timestamps = self._access_log[key]
            timestamps.append(now)

            # Prune entries outside window (keep recent only)
            # Only prune if list is getting long to minimize overhead
            if len(timestamps) > self._config.hot_threshold * 2:
                self._access_log[key] = [t for t in timestamps if t > cutoff]

            self._total_accesses += 1

    def record_access_batch(
        self,
        accesses: list[tuple[str, str, str, str, str]],
    ) -> None:
        """Record multiple accesses efficiently.

        Args:
            accesses: List of (subject_type, subject_id, resource_type, permission, zone_id)
        """
        if not self._config.enabled or not accesses:
            return

        now = time.time()

        with self._lock:
            for key in accesses:
                if key not in self._access_log:
                    self._access_log[key] = []
                self._access_log[key].append(now)

            self._total_accesses += len(accesses)

    def get_hot_entries(self, limit: int | None = None) -> list[HotspotEntry]:
        """Return entries exceeding hot threshold, sorted by access count (hottest first).

        Args:
            limit: Maximum number of entries to return (None = all)

        Returns:
            List of HotspotEntry objects sorted by access_count descending
        """
        now = time.time()
        cutoff = now - self._config.window_seconds
        hot: list[HotspotEntry] = []

        with self._lock:
            for key, timestamps in self._access_log.items():
                # Count accesses in current window
                recent = [t for t in timestamps if t > cutoff]
                if len(recent) >= self._config.hot_threshold:
                    subject_type, subject_id, resource_type, permission, zone_id = key
                    hot.append(
                        HotspotEntry(
                            subject_type=subject_type,
                            subject_id=subject_id,
                            resource_type=resource_type,
                            permission=permission,
                            zone_id=zone_id,
                            access_count=len(recent),
                            last_access=max(recent) if recent else 0,
                        )
                    )

            self._hot_entries_detected = len(hot)

        # Sort by access count (hottest first)
        hot.sort(key=lambda x: x.access_count, reverse=True)

        if limit:
            return hot[:limit]
        return hot

    def get_access_count(
        self,
        subject_type: str,
        subject_id: str,
        resource_type: str,
        permission: str,
        zone_id: str = "default",
    ) -> int:
        """Get current access count for a specific key within the window.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            resource_type: Type of resource
            permission: Permission
            zone_id: Tenant identifier

        Returns:
            Number of accesses in current window
        """
        key = (subject_type, subject_id, resource_type, permission, zone_id)
        now = time.time()
        cutoff = now - self._config.window_seconds

        with self._lock:
            if key not in self._access_log:
                return 0
            return len([t for t in self._access_log[key] if t > cutoff])

    def is_hot(
        self,
        subject_type: str,
        subject_id: str,
        resource_type: str,
        permission: str,
        zone_id: str = "default",
    ) -> bool:
        """Check if a specific key is currently hot.

        Args:
            subject_type: Type of subject
            subject_id: Subject identifier
            resource_type: Type of resource
            permission: Permission
            zone_id: Tenant identifier

        Returns:
            True if access count exceeds hot threshold
        """
        count = self.get_access_count(subject_type, subject_id, resource_type, permission, zone_id)
        return count >= self._config.hot_threshold

    def get_prefetch_candidates(
        self,
        tiger_cache: TigerCache,
        cache_ttl: int = 300,
    ) -> list[HotspotEntry]:
        """Get hot entries that should be prefetched before expiry.

        Args:
            tiger_cache: TigerCache instance for checking cache age
            cache_ttl: Cache TTL in seconds (default: 300s = 5min)

        Returns:
            List of HotspotEntry objects that need prefetching
        """
        hot_entries = self.get_hot_entries()
        candidates: list[HotspotEntry] = []

        for entry in hot_entries:
            # Check if cache entry is about to expire
            cache_age = tiger_cache.get_cache_age(
                subject_type=entry.subject_type,
                subject_id=entry.subject_id,
                permission=entry.permission,
                resource_type=entry.resource_type,
                zone_id=entry.zone_id,
            )

            if cache_age is not None:
                time_until_expiry = cache_ttl - cache_age
                if time_until_expiry < self._config.prefetch_before_expiry_seconds:
                    candidates.append(entry)
                    logger.debug(
                        f"[HOTSPOT] Prefetch candidate: {entry.subject_type}:{entry.subject_id} "
                        f"-> {entry.permission} (expires in {time_until_expiry:.1f}s, "
                        f"accesses={entry.access_count})"
                    )

        return candidates[: self._config.max_prefetch_batch]

    def cleanup_stale_entries(self) -> int:
        """Remove stale access log entries outside the window.

        Should be called periodically to prevent memory growth.

        Returns:
            Number of keys removed
        """
        now = time.time()
        cutoff = now - self._config.window_seconds * 2  # Keep some buffer
        removed = 0

        with self._lock:
            stale_keys = []
            for key, timestamps in self._access_log.items():
                # Remove key if all timestamps are stale
                if not timestamps or max(timestamps) < cutoff:
                    stale_keys.append(key)

            for key in stale_keys:
                del self._access_log[key]
                removed += 1

        if removed > 0:
            logger.debug(f"[HOTSPOT] Cleaned up {removed} stale entries")

        return removed

    def get_stats(self) -> dict[str, Any]:
        """Get hotspot detector statistics.

        Returns:
            Dictionary with tracking statistics
        """
        with self._lock:
            return {
                "enabled": self._config.enabled,
                "window_seconds": self._config.window_seconds,
                "hot_threshold": self._config.hot_threshold,
                "tracked_keys": len(self._access_log),
                "total_accesses": self._total_accesses,
                "hot_entries_detected": self._hot_entries_detected,
                "prefetches_triggered": self._prefetches_triggered,
            }

    def reset(self) -> None:
        """Reset all tracking data."""
        with self._lock:
            self._access_log.clear()
            self._total_accesses = 0
            self._hot_entries_detected = 0
            self._prefetches_triggered = 0


class HotspotPrefetcher:
    """Background worker that proactively warms hot cache entries.

    Monitors HotspotDetector for hot entries approaching TTL expiry
    and queues them for prefetch via TigerCacheUpdater.
    """

    def __init__(
        self,
        detector: HotspotDetector,
        tiger_cache: TigerCache,
        tiger_updater: TigerCacheUpdater,
        config: HotspotConfig | None = None,
    ):
        """Initialize hotspot prefetcher.

        Args:
            detector: HotspotDetector instance for access pattern data
            tiger_cache: TigerCache instance for cache age checks
            tiger_updater: TigerCacheUpdater for queueing prefetch updates
            config: Configuration (uses detector's config if not provided)
        """
        self._detector = detector
        self._tiger_cache = tiger_cache
        self._tiger_updater = tiger_updater
        self._config = config or detector.config
        self._running = False
        self._prefetch_count = 0
        self._last_cycle_duration: float = 0

    async def start(self) -> None:
        """Start background prefetch loop.

        Runs until stop() is called. Safe to call from asyncio context.
        """
        self._running = True
        logger.info(
            f"[HOTSPOT] Starting prefetcher (interval: {self._config.prefetch_interval_seconds}s, "
            f"batch: {self._config.max_prefetch_batch})"
        )

        cleanup_counter = 0

        while self._running:
            try:
                start_time = time.time()
                prefetched = await self._prefetch_cycle()
                self._last_cycle_duration = time.time() - start_time

                if prefetched > 0:
                    logger.info(
                        f"[HOTSPOT] Prefetched {prefetched} entries in {self._last_cycle_duration:.2f}s"
                    )

                # Periodic cleanup
                cleanup_counter += 1
                if cleanup_counter >= (
                    self._config.cleanup_interval_seconds // self._config.prefetch_interval_seconds
                ):
                    self._detector.cleanup_stale_entries()
                    cleanup_counter = 0

            except Exception as e:
                logger.error(f"[HOTSPOT] Prefetch cycle failed: {e}", exc_info=True)

            await asyncio.sleep(self._config.prefetch_interval_seconds)

    def stop(self) -> None:
        """Stop the prefetch loop."""
        self._running = False
        logger.info("[HOTSPOT] Stopping prefetcher")

    async def _prefetch_cycle(self) -> int:
        """Single prefetch cycle - warm entries about to expire.

        Returns:
            Number of entries queued for prefetch
        """
        candidates = self._detector.get_prefetch_candidates(
            self._tiger_cache,
            cache_ttl=self._tiger_cache._cache_ttl,
        )

        if not candidates:
            return 0

        prefetched = 0
        for entry in candidates:
            try:
                # Queue high-priority update
                self._tiger_updater.queue_update(
                    subject_type=entry.subject_type,
                    subject_id=entry.subject_id,
                    permission=entry.permission,
                    resource_type=entry.resource_type,
                    zone_id=entry.zone_id,
                    priority=1,  # High priority for hot entries
                )
                prefetched += 1
                self._prefetch_count += 1
                self._detector._prefetches_triggered += 1

                logger.debug(
                    f"[HOTSPOT] Queued prefetch: {entry.subject_type}:{entry.subject_id} "
                    f"-> {entry.permission} (priority=1)"
                )

            except Exception as e:
                logger.warning(
                    f"[HOTSPOT] Failed to queue prefetch for {entry.subject_type}:{entry.subject_id}: {e}"
                )

        return prefetched

    def get_stats(self) -> dict[str, Any]:
        """Get prefetcher statistics.

        Returns:
            Dictionary with prefetcher statistics
        """
        return {
            "running": self._running,
            "prefetch_count": self._prefetch_count,
            "last_cycle_duration_seconds": self._last_cycle_duration,
            "detector_stats": self._detector.get_stats(),
        }


async def hotspot_prefetch_task(
    detector: HotspotDetector,
    tiger_cache: TigerCache,
    tiger_updater: TigerCacheUpdater,
    config: HotspotConfig | None = None,
) -> None:
    """Background task: Run hotspot prefetcher (Issue #921).

    Convenience function for starting the prefetcher as an asyncio task.

    Args:
        detector: HotspotDetector instance
        tiger_cache: TigerCache instance
        tiger_updater: TigerCacheUpdater instance
        config: Optional configuration override

    Examples:
        >>> # Start hotspot prefetch task
        >>> asyncio.create_task(hotspot_prefetch_task(detector, cache, updater))
    """
    prefetcher = HotspotPrefetcher(detector, tiger_cache, tiger_updater, config)
    await prefetcher.start()
