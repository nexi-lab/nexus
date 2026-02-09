"""Intelligent cache warmup for predictable workloads (Issue #1076).

Pre-populate caches based on access patterns to reduce cold-start latency
and improve first-access performance.

Use Cases:
    1. Application startup: Pre-cache configuration and frequently accessed files
    2. User session start: Pre-cache user's recent files
    3. Scheduled jobs: Warm cache before batch processing
    4. FUSE mount: Pre-cache directory tree metadata

Architecture:
    CacheWarmer orchestrates warming across multiple cache layers:
    - MetadataCache (L1 in-memory) - file metadata
    - LocalDiskCache (L2 SSD) - file content
    - ReBACPermissionCache - permission results
    - TigerCache - pre-materialized permission bitmaps

References:
    - ObjectiveFS: https://objectivefs.com/howto/disk-cache-warming
    - JuiceFS warmup: https://juicefs.com/docs/community/guide/cache/
    - Issue #921: HotspotDetector pattern
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS
    from nexus.storage.local_disk_cache import LocalDiskCache

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================


@dataclass
class WarmupConfig:
    """Configuration for cache warmup operations.

    Attributes:
        enabled: Enable/disable cache warmup
        max_files: Maximum files to warm per operation (default: 1000)
        max_size_mb: Maximum total content size to warm in MB (default: 100)
        include_content: Whether to warm file content (not just metadata)
        depth: Maximum directory depth for directory warmup (default: 2)
        parallel_workers: Number of parallel warmup workers (default: 4)
        small_file_threshold_kb: Files smaller than this are considered "small"
        warmup_timeout_seconds: Timeout for warmup operations

    Note:
        Default values are tuned for typical workspaces:
        - 1000 files covers most active projects
        - Depth 2 reaches /workspace/project/* level
        - Metadata-only warmup is fast (~1-2s for 1000 files)
    """

    enabled: bool = True
    max_files: int = 1000  # Covers most active workspaces, ~200KB memory
    max_size_mb: int = 100
    include_content: bool = False  # Metadata only by default (fast)
    depth: int = 2  # /workspace/project/* level
    parallel_workers: int = 4
    small_file_threshold_kb: int = 1024  # 1MB
    warmup_timeout_seconds: int = 300  # 5 minutes


@dataclass
class BackgroundWarmupConfig:
    """Configuration for background cache warming.

    Attributes:
        enabled: Enable background warming
        interval_seconds: How often to run warmup cycle
        hot_threshold: Access count to consider a path "hot"
        window_seconds: Rolling window for access counting
        max_warmup_per_cycle: Max items to warm per cycle
    """

    enabled: bool = True
    interval_seconds: int = 60
    hot_threshold: int = 10
    window_seconds: int = 300  # 5 minutes
    max_warmup_per_cycle: int = 50


# =============================================================================
# Statistics
# =============================================================================


@dataclass
class WarmupStats:
    """Statistics for warmup operations."""

    files_warmed: int = 0
    metadata_warmed: int = 0
    content_warmed: int = 0
    permissions_warmed: int = 0
    bytes_warmed: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "files_warmed": self.files_warmed,
            "metadata_warmed": self.metadata_warmed,
            "content_warmed": self.content_warmed,
            "permissions_warmed": self.permissions_warmed,
            "bytes_warmed": self.bytes_warmed,
            "bytes_warmed_mb": round(self.bytes_warmed / (1024 * 1024), 2),
            "errors": self.errors,
            "duration_seconds": round(self.duration_seconds, 2),
            "skipped": self.skipped,
        }


# =============================================================================
# File Access Tracker
# =============================================================================


@dataclass
class FileAccessEntry:
    """A file access tracking entry."""

    path: str
    zone_id: str
    user_id: str | None
    access_count: int
    last_access: float
    total_bytes: int = 0

    @property
    def cache_key(self) -> tuple[str, str]:
        """Return (zone_id, path) tuple for cache lookup."""
        return (self.zone_id, self.path)


class FileAccessTracker:
    """Track file access patterns for history-based warmup.

    Similar to HotspotDetector but tracks file paths instead of permission paths.
    Uses rolling window to identify frequently accessed files.

    Thread-safe implementation with minimal overhead.
    """

    def __init__(
        self,
        window_seconds: int = 300,
        hot_threshold: int = 10,
        max_tracked_paths: int = 10000,
    ):
        """Initialize file access tracker.

        Args:
            window_seconds: Rolling window for access counting
            hot_threshold: Accesses within window to be "hot"
            max_tracked_paths: Maximum paths to track (LRU eviction)
        """
        self._window_seconds = window_seconds
        self._hot_threshold = hot_threshold
        self._max_tracked_paths = max_tracked_paths

        # Access log: (zone_id, path) -> list[timestamps]
        self._access_log: dict[tuple[str, str], list[float]] = {}
        # User tracking: (zone_id, path) -> set[user_ids]
        self._user_access: dict[tuple[str, str], set[str]] = {}
        # Size tracking: (zone_id, path) -> total_bytes
        self._size_cache: dict[tuple[str, str], int] = {}

        self._lock = threading.RLock()
        self._total_accesses = 0

    def record_access(
        self,
        path: str,
        zone_id: str = "default",
        user_id: str | None = None,
        size_bytes: int = 0,
    ) -> None:
        """Record a file access.

        Low-overhead operation designed to be called on every file access.

        Args:
            path: File path accessed
            zone_id: Zone identifier
            user_id: User who accessed (optional)
            size_bytes: File size in bytes (optional, for prioritization)
        """
        key = (zone_id, path)
        now = time.time()

        with self._lock:
            # Initialize if new
            if key not in self._access_log:
                # LRU eviction if at capacity
                if len(self._access_log) >= self._max_tracked_paths:
                    self._evict_oldest()
                self._access_log[key] = []
                self._user_access[key] = set()

            # Record timestamp
            self._access_log[key].append(now)
            self._total_accesses += 1

            # Track user
            if user_id:
                self._user_access[key].add(user_id)

            # Track size
            if size_bytes > 0:
                self._size_cache[key] = size_bytes

            # Lazy prune if list is getting long
            if len(self._access_log[key]) > self._hot_threshold * 3:
                cutoff = now - self._window_seconds
                self._access_log[key] = [t for t in self._access_log[key] if t > cutoff]

    def _evict_oldest(self) -> None:
        """Evict oldest entry (must hold lock)."""
        if not self._access_log:
            return

        # Find key with oldest last access
        oldest_key = min(
            self._access_log.keys(),
            key=lambda k: max(self._access_log[k]) if self._access_log[k] else 0,
        )
        del self._access_log[oldest_key]
        self._user_access.pop(oldest_key, None)
        self._size_cache.pop(oldest_key, None)

    def get_hot_files(
        self,
        zone_id: str | None = None,
        user_id: str | None = None,
        limit: int | None = None,
    ) -> list[FileAccessEntry]:
        """Get frequently accessed files.

        Args:
            zone_id: Filter by zone (None = all zones)
            user_id: Filter by user (None = all users)
            limit: Maximum entries to return

        Returns:
            List of FileAccessEntry sorted by access_count descending
        """
        now = time.time()
        cutoff = now - self._window_seconds
        hot: list[FileAccessEntry] = []

        with self._lock:
            for key, timestamps in self._access_log.items():
                key_zone, path = key

                # Filter by zone
                if zone_id and key_zone != zone_id:
                    continue

                # Filter by user
                if user_id:
                    users = self._user_access.get(key, set())
                    if user_id not in users:
                        continue

                # Count recent accesses
                recent = [t for t in timestamps if t > cutoff]
                if len(recent) >= self._hot_threshold:
                    hot.append(
                        FileAccessEntry(
                            path=path,
                            zone_id=key_zone,
                            user_id=user_id,
                            access_count=len(recent),
                            last_access=max(recent) if recent else 0,
                            total_bytes=self._size_cache.get(key, 0),
                        )
                    )

        # Sort by access count (hottest first)
        hot.sort(key=lambda x: x.access_count, reverse=True)

        if limit:
            return hot[:limit]
        return hot

    def get_user_recent_files(
        self,
        user_id: str,
        zone_id: str = "default",
        hours: int = 24,
        limit: int = 100,
    ) -> list[FileAccessEntry]:
        """Get files recently accessed by a specific user.

        Args:
            user_id: User to get files for
            zone_id: Zone identifier
            hours: Look back N hours
            limit: Maximum files to return

        Returns:
            List of FileAccessEntry sorted by last_access descending
        """
        now = time.time()
        cutoff = now - (hours * 3600)
        recent: list[FileAccessEntry] = []

        with self._lock:
            for key, timestamps in self._access_log.items():
                key_zone, path = key

                if key_zone != zone_id:
                    continue

                users = self._user_access.get(key, set())
                if user_id not in users:
                    continue

                # Get accesses in time range
                in_range = [t for t in timestamps if t > cutoff]
                if in_range:
                    recent.append(
                        FileAccessEntry(
                            path=path,
                            zone_id=key_zone,
                            user_id=user_id,
                            access_count=len(in_range),
                            last_access=max(in_range),
                            total_bytes=self._size_cache.get(key, 0),
                        )
                    )

        # Sort by last access (most recent first)
        recent.sort(key=lambda x: x.last_access, reverse=True)
        return recent[:limit]

    def cleanup_stale_entries(self) -> int:
        """Remove stale entries outside the window.

        Returns:
            Number of entries removed
        """
        now = time.time()
        cutoff = now - self._window_seconds * 2
        removed = 0

        with self._lock:
            stale_keys = []
            for key, timestamps in self._access_log.items():
                if not timestamps or max(timestamps) < cutoff:
                    stale_keys.append(key)

            for key in stale_keys:
                del self._access_log[key]
                self._user_access.pop(key, None)
                self._size_cache.pop(key, None)
                removed += 1

        if removed > 0:
            logger.debug(f"[WARMUP] Cleaned up {removed} stale file access entries")

        return removed

    def get_stats(self) -> dict[str, Any]:
        """Get tracker statistics."""
        with self._lock:
            return {
                "tracked_paths": len(self._access_log),
                "total_accesses": self._total_accesses,
                "window_seconds": self._window_seconds,
                "hot_threshold": self._hot_threshold,
            }

    def clear(self) -> None:
        """Clear all tracking data."""
        with self._lock:
            self._access_log.clear()
            self._user_access.clear()
            self._size_cache.clear()
            self._total_accesses = 0


# =============================================================================
# CacheWarmer
# =============================================================================


class CacheWarmer:
    """Pre-populate caches based on access patterns.

    Orchestrates warming across multiple cache layers:
    - MetadataCache: File metadata (getattr, exists, list)
    - LocalDiskCache: File content
    - ReBACPermissionCache: Permission check results
    - TigerCache: Pre-materialized permission bitmaps

    Example:
        >>> warmer = CacheWarmer(nexus_fs)
        >>> stats = await warmer.warmup_directory("/workspace/project", depth=3)
        >>> print(f"Warmed {stats.files_warmed} files")

        >>> # Warm based on user history
        >>> stats = await warmer.warmup_from_history(user="alice", hours=24)
    """

    def __init__(
        self,
        nexus_fs: NexusFS,
        config: WarmupConfig | None = None,
        file_tracker: FileAccessTracker | None = None,
        local_disk_cache: LocalDiskCache | None = None,
    ):
        """Initialize cache warmer.

        Args:
            nexus_fs: NexusFS instance for file operations
            config: Warmup configuration
            file_tracker: File access tracker for history-based warmup
            local_disk_cache: L2 disk cache for content warming
        """
        self._nexus = nexus_fs
        self._config = config or WarmupConfig()
        self._file_tracker = file_tracker
        self._local_disk_cache = local_disk_cache

        # Semaphore for parallel workers
        self._semaphore = asyncio.Semaphore(self._config.parallel_workers)

        # Track current warmup state
        self._is_warming = False
        self._current_stats = WarmupStats()

    @property
    def config(self) -> WarmupConfig:
        """Get current configuration."""
        return self._config

    @property
    def is_warming(self) -> bool:
        """Check if warmup is in progress."""
        return self._is_warming

    async def warmup_directory(
        self,
        path: str,
        depth: int | None = None,
        include_content: bool | None = None,
        max_files: int | None = None,
        zone_id: str = "default",
        context: Any | None = None,
    ) -> WarmupStats:
        """Pre-cache directory tree metadata and optionally content.

        Args:
            path: Directory path to warm
            depth: Maximum depth to traverse (default: config.depth)
            include_content: Warm file content too (default: config.include_content)
            max_files: Maximum files to warm (default: config.max_files)
            zone_id: Zone identifier
            context: Operation context for permission checks

        Returns:
            WarmupStats with results
        """
        if not self._config.enabled:
            logger.debug("[WARMUP] Disabled, skipping directory warmup")
            return WarmupStats()

        depth = depth if depth is not None else self._config.depth
        include_content = (
            include_content if include_content is not None else self._config.include_content
        )
        max_files = max_files if max_files is not None else self._config.max_files

        self._is_warming = True
        self._current_stats = WarmupStats()
        start_time = time.time()

        try:
            logger.info(
                f"[WARMUP] Starting directory warmup: path={path}, depth={depth}, "
                f"include_content={include_content}, max_files={max_files}"
            )

            # Get files using glob
            pattern = self._build_glob_pattern(path, depth)
            files = self._nexus.glob(pattern, path="/", context=context)
            files = files[:max_files]

            logger.info(f"[WARMUP] Found {len(files)} files to warm")

            # Parallel metadata warmup
            metadata_tasks = [self._warmup_metadata(f, zone_id, context) for f in files]
            await asyncio.gather(*metadata_tasks, return_exceptions=True)

            # Content warmup for small files if requested
            if include_content:
                small_files = await self._filter_small_files(files, context)
                content_limit = min(100, max_files // 10)  # Limit content warming
                small_files = small_files[:content_limit]

                content_tasks = [self._warmup_content(f, zone_id, context) for f in small_files]
                await asyncio.gather(*content_tasks, return_exceptions=True)

            self._current_stats.duration_seconds = time.time() - start_time
            self._current_stats.files_warmed = len(files)

            logger.info(f"[WARMUP] Directory warmup complete: {self._current_stats.to_dict()}")

            return self._current_stats

        except Exception as e:
            logger.error(f"[WARMUP] Directory warmup failed: {e}", exc_info=True)
            self._current_stats.errors += 1
            raise

        finally:
            self._is_warming = False

    async def warmup_from_history(
        self,
        user: str | None = None,
        hours: int = 24,
        max_files: int | None = None,
        zone_id: str = "default",
        context: Any | None = None,
    ) -> WarmupStats:
        """Pre-cache files based on user's recent access patterns.

        Args:
            user: User to warm cache for (None = all users)
            hours: Look back N hours
            max_files: Maximum files to warm (default: config.max_files)
            zone_id: Zone identifier
            context: Operation context

        Returns:
            WarmupStats with results
        """
        if not self._config.enabled:
            logger.debug("[WARMUP] Disabled, skipping history warmup")
            return WarmupStats()

        if not self._file_tracker:
            logger.warning("[WARMUP] No file tracker configured, cannot warm from history")
            return WarmupStats()

        max_files = max_files if max_files is not None else self._config.max_files

        self._is_warming = True
        self._current_stats = WarmupStats()
        start_time = time.time()

        try:
            logger.info(
                f"[WARMUP] Starting history warmup: user={user}, hours={hours}, "
                f"max_files={max_files}"
            )

            # Get recent files from tracker
            if user:
                recent_files = self._file_tracker.get_user_recent_files(
                    user_id=user, zone_id=zone_id, hours=hours, limit=max_files
                )
            else:
                recent_files = self._file_tracker.get_hot_files(zone_id=zone_id, limit=max_files)

            # Identify hot files (accessed multiple times)
            hot_files = [f for f in recent_files if f.access_count >= 2]
            logger.info(f"[WARMUP] Found {len(hot_files)} hot files from history")

            # Warm content for hot files
            content_tasks = [self._warmup_content(f.path, zone_id, context) for f in hot_files]
            await asyncio.gather(*content_tasks, return_exceptions=True)

            self._current_stats.duration_seconds = time.time() - start_time
            self._current_stats.files_warmed = len(hot_files)

            logger.info(f"[WARMUP] History warmup complete: {self._current_stats.to_dict()}")

            return self._current_stats

        except Exception as e:
            logger.error(f"[WARMUP] History warmup failed: {e}", exc_info=True)
            self._current_stats.errors += 1
            raise

        finally:
            self._is_warming = False

    async def warmup_permissions(
        self,
        user: str,
        zone_id: str = "default",
        paths: list[str] | None = None,
    ) -> WarmupStats:
        """Pre-cache permission graph for user.

        Args:
            user: User to warm permissions for
            zone_id: Zone identifier
            paths: Specific paths to warm (None = common paths)

        Returns:
            WarmupStats with results
        """
        if not self._config.enabled:
            logger.debug("[WARMUP] Disabled, skipping permission warmup")
            return WarmupStats()

        self._is_warming = True
        self._current_stats = WarmupStats()
        start_time = time.time()

        try:
            logger.info(f"[WARMUP] Starting permission warmup: user={user}")

            # Get rebac manager
            rebac_manager = getattr(self._nexus, "_rebac_manager", None)
            if not rebac_manager:
                logger.warning("[WARMUP] No ReBACManager, skipping permission warmup")
                return self._current_stats

            # Get common paths if not specified
            if paths is None:
                paths = await self._get_common_paths(zone_id)

            # Warm permission checks for common paths
            for path in paths[: self._config.max_files]:
                try:
                    # Trigger permission check to warm cache
                    await self._warmup_permission_check(rebac_manager, user, path, zone_id)
                    self._current_stats.permissions_warmed += 1
                except Exception as e:
                    logger.debug(f"[WARMUP] Permission check failed for {path}: {e}")
                    self._current_stats.errors += 1

            self._current_stats.duration_seconds = time.time() - start_time

            logger.info(f"[WARMUP] Permission warmup complete: {self._current_stats.to_dict()}")

            return self._current_stats

        except Exception as e:
            logger.error(f"[WARMUP] Permission warmup failed: {e}", exc_info=True)
            self._current_stats.errors += 1
            raise

        finally:
            self._is_warming = False

    async def warmup_paths(
        self,
        paths: list[str],
        include_content: bool = False,
        zone_id: str = "default",
        context: Any | None = None,
    ) -> WarmupStats:
        """Warm specific paths.

        Args:
            paths: List of paths to warm
            include_content: Warm content too (not just metadata)
            zone_id: Zone identifier
            context: Operation context

        Returns:
            WarmupStats with results
        """
        if not self._config.enabled:
            return WarmupStats()

        self._is_warming = True
        self._current_stats = WarmupStats()
        start_time = time.time()

        try:
            logger.info(f"[WARMUP] Warming {len(paths)} specific paths")

            # Warm metadata
            metadata_tasks = [self._warmup_metadata(p, zone_id, context) for p in paths]
            await asyncio.gather(*metadata_tasks, return_exceptions=True)

            # Warm content if requested
            if include_content:
                content_tasks = [
                    self._warmup_content(p, zone_id, context)
                    for p in paths[: self._config.max_files]
                ]
                await asyncio.gather(*content_tasks, return_exceptions=True)

            self._current_stats.duration_seconds = time.time() - start_time
            self._current_stats.files_warmed = len(paths)

            return self._current_stats

        finally:
            self._is_warming = False

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _build_glob_pattern(self, path: str, depth: int) -> str:
        """Build glob pattern for depth-limited search."""
        path = path.rstrip("/")
        if depth <= 0 or depth == 1:
            return f"{path}/*"
        elif depth == 2:
            return f"{path}/**/*"
        else:
            # For deeper depths, use recursive glob
            return f"{path}/**/*"

    async def _warmup_metadata(
        self,
        path: str,
        _zone_id: str,
        _context: Any | None,
    ) -> None:
        """Warm metadata cache for a single file."""
        async with self._semaphore:
            try:
                # Check if file exists (warms exists cache)
                exists = self._nexus.exists(path)
                if not exists:
                    self._current_stats.skipped += 1
                    return

                # Get metadata (warms path cache)
                # This internally triggers the metadata cache
                metadata = self._nexus.metadata.get(path)
                if metadata:
                    self._current_stats.metadata_warmed += 1

            except Exception as e:
                logger.debug(f"[WARMUP] Metadata warmup failed for {path}: {e}")
                self._current_stats.errors += 1

    async def _warmup_content(
        self,
        path: str,
        zone_id: str,
        context: Any | None,
    ) -> None:
        """Warm content cache for a single file."""
        async with self._semaphore:
            try:
                # Read file content
                content = self._nexus.read(path, context=context)
                if content:
                    content_bytes = content if isinstance(content, bytes) else b""
                    self._current_stats.content_warmed += 1
                    self._current_stats.bytes_warmed += len(content_bytes)

                    # Also warm L2 disk cache if available
                    # In CAS storage, physical_path is the content hash
                    if self._local_disk_cache and isinstance(content, bytes):
                        metadata = self._nexus.metadata.get(path)
                        if metadata and metadata.physical_path:
                            # Extract hash from physical_path (format: "cas/{hash}")
                            content_hash = metadata.physical_path
                            if "/" in content_hash:
                                content_hash = content_hash.split("/")[-1]
                            self._local_disk_cache.put(
                                content_hash,
                                content,
                                zone_id=zone_id,
                            )

            except Exception as e:
                logger.debug(f"[WARMUP] Content warmup failed for {path}: {e}")
                self._current_stats.errors += 1

    async def _filter_small_files(
        self,
        paths: list[str],
        _context: Any | None,
    ) -> list[str]:
        """Filter paths to only small files."""
        threshold = self._config.small_file_threshold_kb * 1024
        small_files: list[str] = []

        for path in paths:
            try:
                metadata = self._nexus.metadata.get(path)
                if metadata and metadata.size and metadata.size < threshold:
                    small_files.append(path)
            except Exception:
                pass

        return small_files

    async def _warmup_permission_check(
        self,
        rebac_manager: Any,
        user: str,
        path: str,
        zone_id: str,
    ) -> None:
        """Warm permission cache for a user/path combination."""
        try:
            # Check read permission (most common)
            if hasattr(rebac_manager, "check_permission"):
                await asyncio.to_thread(
                    rebac_manager.check_permission,
                    subject_type="user",
                    subject_id=user,
                    permission="read",
                    resource_type="file",
                    resource_id=path,
                    zone_id=zone_id,
                )
        except Exception as e:
            logger.debug(f"[WARMUP] Permission check warming failed: {e}")

    async def _get_common_paths(self, _zone_id: str) -> list[str]:
        """Get common paths for permission warming."""
        # Get root directories and common paths
        common: list[str] = []

        try:
            # List root directory
            root_entries = self._nexus.list("/", recursive=False)
            if isinstance(root_entries, list):
                # Handle both list[str] and list[dict] return types
                for entry in root_entries[:50]:
                    if isinstance(entry, str):
                        common.append(entry)
                    elif isinstance(entry, dict) and "path" in entry:
                        common.append(entry["path"])
        except Exception:
            pass

        # Add known common paths
        common.extend(["/workspace", "/config", "/data"])

        return list(set(common))

    def get_stats(self) -> dict[str, Any]:
        """Get current warmup statistics."""
        return {
            "is_warming": self._is_warming,
            "config": {
                "enabled": self._config.enabled,
                "max_files": self._config.max_files,
                "max_size_mb": self._config.max_size_mb,
                "parallel_workers": self._config.parallel_workers,
            },
            "current": self._current_stats.to_dict(),
            "file_tracker": self._file_tracker.get_stats() if self._file_tracker else None,
        }


# =============================================================================
# Background Cache Warmer
# =============================================================================


class BackgroundCacheWarmer:
    """Continuously warm cache based on access patterns.

    Runs as a background task, monitoring file access patterns
    and proactively warming frequently accessed files.

    Similar to HotspotPrefetcher but for file content caching.

    Example:
        >>> warmer = BackgroundCacheWarmer(cache_warmer)
        >>> asyncio.create_task(warmer.run())
    """

    def __init__(
        self,
        cache_warmer: CacheWarmer,
        file_tracker: FileAccessTracker,
        config: BackgroundWarmupConfig | None = None,
    ):
        """Initialize background warmer.

        Args:
            cache_warmer: CacheWarmer instance
            file_tracker: FileAccessTracker for access patterns
            config: Background warmup configuration
        """
        self._warmer = cache_warmer
        self._tracker = file_tracker
        self._config = config or BackgroundWarmupConfig()
        self._running = False
        self._cycles_completed = 0
        self._last_cycle_duration: float = 0

    async def run(self) -> None:
        """Run background warmup loop.

        Runs until stop() is called.
        """
        if not self._config.enabled:
            logger.info("[WARMUP] Background warmer disabled")
            return

        self._running = True
        logger.info(
            f"[WARMUP] Starting background warmer (interval: {self._config.interval_seconds}s)"
        )

        cleanup_counter = 0

        while self._running:
            try:
                start_time = time.time()
                await self._warmup_cycle()
                self._last_cycle_duration = time.time() - start_time
                self._cycles_completed += 1

                # Periodic cleanup
                cleanup_counter += 1
                if cleanup_counter >= 6:  # Every 6 cycles
                    self._tracker.cleanup_stale_entries()
                    cleanup_counter = 0

            except Exception as e:
                logger.error(f"[WARMUP] Background cycle failed: {e}", exc_info=True)

            await asyncio.sleep(self._config.interval_seconds)

    def stop(self) -> None:
        """Stop background warmup."""
        self._running = False
        logger.info("[WARMUP] Stopping background warmer")

    async def _warmup_cycle(self) -> None:
        """Single warmup cycle."""
        # Get hot files
        hot_files = self._tracker.get_hot_files(limit=self._config.max_warmup_per_cycle)

        if not hot_files:
            return

        # Warm hot files
        paths = [f.path for f in hot_files]
        await self._warmer.warmup_paths(paths, include_content=True)

        logger.debug(f"[WARMUP] Background cycle warmed {len(paths)} files")

    def get_stats(self) -> dict[str, Any]:
        """Get background warmer statistics."""
        return {
            "running": self._running,
            "cycles_completed": self._cycles_completed,
            "last_cycle_duration": round(self._last_cycle_duration, 3),
            "config": {
                "enabled": self._config.enabled,
                "interval_seconds": self._config.interval_seconds,
                "hot_threshold": self._config.hot_threshold,
                "max_warmup_per_cycle": self._config.max_warmup_per_cycle,
            },
            "tracker_stats": self._tracker.get_stats(),
        }


# =============================================================================
# Convenience Functions
# =============================================================================


async def warmup_on_mount(
    nexus_fs: NexusFS,
    mount_path: str,
    depth: int = 2,
    include_content: bool = False,
    max_files: int = 1000,
    zone_id: str = "default",
) -> WarmupStats:
    """Convenience function: Warm cache after FUSE mount.

    Args:
        nexus_fs: NexusFS instance
        mount_path: Path that was mounted
        depth: Directory depth to warm
        include_content: Warm content too
        max_files: Maximum files
        zone_id: Zone identifier

    Returns:
        WarmupStats
    """
    warmer = CacheWarmer(nexus_fs)
    return await warmer.warmup_directory(
        path=mount_path,
        depth=depth,
        include_content=include_content,
        max_files=max_files,
        zone_id=zone_id,
    )


async def background_warmup_task(
    cache_warmer: CacheWarmer,
    file_tracker: FileAccessTracker,
    config: BackgroundWarmupConfig | None = None,
) -> None:
    """Background task: Run background cache warmer.

    Convenience function for starting the warmer as an asyncio task.

    Args:
        cache_warmer: CacheWarmer instance
        file_tracker: FileAccessTracker instance
        config: Optional configuration override

    Example:
        >>> asyncio.create_task(background_warmup_task(warmer, tracker))
    """
    background = BackgroundCacheWarmer(cache_warmer, file_tracker, config)
    await background.run()


# =============================================================================
# Global Instance Management
# =============================================================================

_default_tracker: FileAccessTracker | None = None
_tracker_lock = threading.Lock()


def get_file_access_tracker() -> FileAccessTracker:
    """Get or create global FileAccessTracker instance."""
    global _default_tracker

    if _default_tracker is None:
        with _tracker_lock:
            if _default_tracker is None:
                _default_tracker = FileAccessTracker()

    return _default_tracker


def set_file_access_tracker(tracker: FileAccessTracker | None) -> None:
    """Set the global FileAccessTracker instance."""
    global _default_tracker
    with _tracker_lock:
        _default_tracker = tracker
