"""Intelligent readahead and prefetch for FUSE sequential reads (Issue #1073).

This module implements proactive readahead to dramatically improve sequential
read performance by anticipating access patterns and prefetching data into
the L1/L2 cache before it's requested.

Architecture:
    Read Request → Pattern Detection → Prefetch Trigger → Cache Warming
                         ↓                    ↓
                   ReadSession          ThreadPoolExecutor
                   (per file)           (async prefetch)
                         ↓                    ↓
                   Sequential?    →    Prefetch next N blocks
                         ↓                    ↓
                   Adjust window      Store in L1/L2 cache

Key Features:
- Session-based pattern tracking (like JuiceFS)
- Adaptive window sizing (like Linux kernel on-demand readahead)
- No prefetch on random access (like GeeseFS)
- Bounded memory pool to prevent OOM
- Integration with LocalDiskCache for persistent prefetch

Performance expectations:
- Sequential read: 5-10x improvement (network-bound → cache-bound)
- Random read: No overhead (prefetch disabled)
- Memory usage: Bounded by buffer_pool_size_mb

References:
- JuiceFS readahead: https://juicefs.com/en/blog/engineering/optimize-read-performance
- Linux on-demand readahead: https://lwn.net/Articles/235164/
- GeeseFS: https://github.com/yandex-cloud/geesefs
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.storage.local_disk_cache import LocalDiskCache

logger = logging.getLogger(__name__)


class AccessPattern(Enum):
    """Detected file access pattern."""

    UNKNOWN = "unknown"
    SEQUENTIAL = "sequential"
    RANDOM = "random"


# =============================================================================
# Configuration
# =============================================================================

# Default configuration values
# Optimized for remote/network filesystems (Docker sandbox, remote Nexus)
# Based on best practices from GCS FUSE, AWS Mountpoint, JuiceFS
DEFAULT_BUFFER_POOL_MB = 128  # Increased for parallel prefetch
DEFAULT_PREFETCH_WORKERS = 8  # More workers to hide network latency
DEFAULT_BLOCK_SIZE = 4 * 1024 * 1024  # 4MB (matches LocalDiskCache)
DEFAULT_MIN_SEQUENTIAL_COUNT = 2  # Trigger prefetch after 2 sequential reads
DEFAULT_INITIAL_WINDOW = 512 * 1024  # 512KB initial (larger for network)
DEFAULT_MAX_WINDOW = 64 * 1024 * 1024  # 64MB max (like GCS FUSE)
DEFAULT_SEQUENTIAL_TOLERANCE = 64 * 1024  # 64KB tolerance for sequential detection
DEFAULT_MAX_BLOCKS_PER_TRIGGER = 8  # Prefetch 8 blocks in parallel (32MB ahead)
DEFAULT_PREFETCH_ON_OPEN = True  # Start prefetching when file is opened


@dataclass
class ReadaheadConfig:
    """Configuration for readahead behavior.

    Optimized defaults for remote/network filesystems based on:
    - GCS FUSE: Parallel downloads, large buffers
    - AWS Mountpoint: 2GB prefetch windows
    - JuiceFS: Adaptive window sizing
    """

    enabled: bool = True
    buffer_pool_mb: int = DEFAULT_BUFFER_POOL_MB
    prefetch_workers: int = DEFAULT_PREFETCH_WORKERS
    block_size: int = DEFAULT_BLOCK_SIZE
    min_sequential_count: int = DEFAULT_MIN_SEQUENTIAL_COUNT
    initial_window: int = DEFAULT_INITIAL_WINDOW
    max_window: int = DEFAULT_MAX_WINDOW
    sequential_tolerance: int = DEFAULT_SEQUENTIAL_TOLERANCE
    warm_l2_cache: bool = True  # Also store prefetched data in L2 SSD cache
    max_blocks_per_trigger: int = DEFAULT_MAX_BLOCKS_PER_TRIGGER  # Parallel prefetch count
    prefetch_on_open: bool = DEFAULT_PREFETCH_ON_OPEN  # Start prefetching on file open

    @classmethod
    def from_dict(cls, config: dict[str, Any]) -> ReadaheadConfig:
        """Create config from dictionary."""
        return cls(
            enabled=config.get("readahead_enabled", True),
            buffer_pool_mb=config.get("readahead_buffer_mb", DEFAULT_BUFFER_POOL_MB),
            prefetch_workers=config.get("readahead_workers", DEFAULT_PREFETCH_WORKERS),
            block_size=config.get("readahead_block_size", DEFAULT_BLOCK_SIZE),
            min_sequential_count=config.get(
                "readahead_min_sequential", DEFAULT_MIN_SEQUENTIAL_COUNT
            ),
            initial_window=config.get("readahead_initial_window", DEFAULT_INITIAL_WINDOW),
            max_window=config.get("readahead_max_window", DEFAULT_MAX_WINDOW),
            sequential_tolerance=config.get(
                "readahead_sequential_tolerance", DEFAULT_SEQUENTIAL_TOLERANCE
            ),
            warm_l2_cache=config.get("readahead_warm_l2_cache", True),
            max_blocks_per_trigger=config.get(
                "readahead_max_blocks", DEFAULT_MAX_BLOCKS_PER_TRIGGER
            ),
            prefetch_on_open=config.get("readahead_prefetch_on_open", DEFAULT_PREFETCH_ON_OPEN),
        )


# =============================================================================
# ReadSession: Per-file access pattern tracking
# =============================================================================


@dataclass
class ReadSession:
    """Tracks access pattern for a single file handle.

    Each open file handle gets its own session to track:
    - Last read offset/size for sequential detection
    - Sequential read count for prefetch triggering
    - Adaptive readahead window size
    - Pending prefetch operations

    Based on JuiceFS session tracking and Linux kernel readahead state.
    """

    path: str
    fh: int  # File handle
    last_offset: int = 0
    last_size: int = 0
    sequential_count: int = 0
    readahead_window: int = DEFAULT_INITIAL_WINDOW
    max_window: int = DEFAULT_MAX_WINDOW
    sequential_tolerance: int = DEFAULT_SEQUENTIAL_TOLERANCE

    # Track pending prefetch offsets to avoid duplicate fetches
    prefetch_pending: set[int] = field(default_factory=set)
    prefetch_completed: set[int] = field(default_factory=set)

    # Statistics
    prefetch_hits: int = 0
    prefetch_misses: int = 0
    total_reads: int = 0

    # Timestamps
    created_at: float = field(default_factory=time.time)
    last_access: float = field(default_factory=time.time)

    def update_access(self, offset: int, size: int) -> AccessPattern:
        """Update session state and detect access pattern.

        Sequential detection logic (based on Linux kernel):
        - Sequential if new offset is within tolerance of expected offset
        - Expected offset = last_offset + last_size
        - Tolerance allows for small gaps (e.g., skipped headers)

        Window sizing (based on Linux kernel on-demand readahead):
        - Start with small window (128KB)
        - Double window on each sequential hit
        - Cap at max_window (16MB)
        - Reset on random access

        Args:
            offset: Current read offset
            size: Current read size

        Returns:
            Detected access pattern (SEQUENTIAL or RANDOM)
        """
        self.total_reads += 1
        self.last_access = time.time()

        # Calculate expected offset for sequential access
        expected_offset = self.last_offset + self.last_size

        # Check if this is a sequential read
        # Sequential means: reading near where we expect, moving forward
        # Allow some tolerance for small gaps (headers, metadata, etc.)
        # BUT: backward jumps are ALWAYS random, even if within tolerance
        offset_diff = offset - expected_offset  # Signed difference
        is_sequential = (
            offset_diff >= -self.sequential_tolerance  # Not too far back
            and offset_diff <= self.sequential_tolerance  # Not too far forward
            and offset >= self.last_offset  # Not going backwards significantly
        )

        # Also consider forward jumps within a reasonable range as sequential
        # This handles cases where app skips some data but still reading forward
        is_forward_sequential = (
            offset > self.last_offset and offset <= expected_offset + self.readahead_window
        )

        if is_sequential or (self.sequential_count > 0 and is_forward_sequential):
            self.sequential_count += 1

            # Grow window exponentially (like Linux kernel)
            # Double the window after each sequential read, up to max
            if self.sequential_count >= 2:
                new_window = min(self.readahead_window * 2, self.max_window)
                if new_window != self.readahead_window:
                    logger.debug(
                        f"[READAHEAD] Window growth: {self.readahead_window} -> {new_window} "
                        f"(sequential_count={self.sequential_count})"
                    )
                self.readahead_window = new_window

            pattern = AccessPattern.SEQUENTIAL
        else:
            # Random access detected - reset state
            if self.sequential_count > 0:
                logger.debug(
                    f"[READAHEAD] Random access detected: expected={expected_offset}, "
                    f"actual={offset}, diff={offset_diff}"
                )
            self.sequential_count = 0
            self.readahead_window = DEFAULT_INITIAL_WINDOW

            # Clear pending prefetches on random seek (like GeeseFS)
            self.prefetch_pending.clear()

            pattern = AccessPattern.RANDOM

        # Update last read info
        self.last_offset = offset
        self.last_size = size

        return pattern

    def mark_prefetch_pending(self, block_offset: int) -> bool:
        """Mark a block as pending prefetch.

        Returns:
            True if marked (not already pending/completed), False otherwise
        """
        if block_offset in self.prefetch_pending or block_offset in self.prefetch_completed:
            return False
        self.prefetch_pending.add(block_offset)
        return True

    def mark_prefetch_completed(self, block_offset: int) -> None:
        """Mark a block as completed prefetch."""
        self.prefetch_pending.discard(block_offset)
        self.prefetch_completed.add(block_offset)

    def cancel_prefetch(self, block_offset: int) -> None:
        """Cancel a pending prefetch."""
        self.prefetch_pending.discard(block_offset)

    def record_prefetch_hit(self) -> None:
        """Record that a prefetch was used."""
        self.prefetch_hits += 1

    def record_prefetch_miss(self) -> None:
        """Record that a prefetch was not available."""
        self.prefetch_misses += 1

    @property
    def prefetch_hit_rate(self) -> float:
        """Calculate prefetch hit rate."""
        total = self.prefetch_hits + self.prefetch_misses
        return self.prefetch_hits / total if total > 0 else 0.0

    def get_stats(self) -> dict[str, Any]:
        """Get session statistics."""
        return {
            "path": self.path,
            "fh": self.fh,
            "sequential_count": self.sequential_count,
            "readahead_window": self.readahead_window,
            "prefetch_pending": len(self.prefetch_pending),
            "prefetch_completed": len(self.prefetch_completed),
            "prefetch_hits": self.prefetch_hits,
            "prefetch_misses": self.prefetch_misses,
            "prefetch_hit_rate": self.prefetch_hit_rate,
            "total_reads": self.total_reads,
            "age_seconds": time.time() - self.created_at,
        }


# =============================================================================
# PrefetchBufferPool: Bounded memory for prefetched data
# =============================================================================


@dataclass
class BufferEntry:
    """Entry in the prefetch buffer."""

    path: str
    offset: int
    data: bytes
    created_at: float = field(default_factory=time.time)
    access_count: int = 0


class PrefetchBufferPool:
    """Bounded memory pool for prefetched data.

    Provides fast in-memory access to prefetched blocks before they're
    requested. Uses simple LRU eviction when full.

    This is separate from the L1 content cache because:
    - Prefetch data is speculative (may never be used)
    - Different eviction policy (aggressive for unused prefetch)
    - Keyed by (path, offset) not just path

    Thread-safe for concurrent prefetch workers.
    """

    def __init__(self, max_size_bytes: int):
        """Initialize buffer pool.

        Args:
            max_size_bytes: Maximum memory to use for prefetch buffers
        """
        self._max_size = max_size_bytes
        self._current_size = 0
        self._buffers: dict[str, dict[int, BufferEntry]] = {}  # path -> {offset: entry}
        self._access_order: list[tuple[str, int]] = []  # LRU tracking
        self._lock = threading.RLock()

        # Statistics
        self._stats = {
            "hits": 0,
            "misses": 0,
            "evictions": 0,
            "bytes_stored": 0,
            "bytes_evicted": 0,
        }

        logger.info(
            f"[READAHEAD] PrefetchBufferPool initialized: max_size={max_size_bytes / (1024 * 1024):.1f}MB"
        )

    def get(self, path: str, offset: int, size: int) -> bytes | None:
        """Get prefetched data if available.

        Checks if the requested range is covered by a prefetched block.

        Args:
            path: File path
            offset: Read offset
            size: Read size

        Returns:
            Data bytes if available, None otherwise
        """
        with self._lock:
            path_buffers = self._buffers.get(path)
            if path_buffers is None:
                self._stats["misses"] += 1
                return None

            # Find a block that covers this range
            # Blocks are stored at aligned offsets
            for block_offset, entry in path_buffers.items():
                block_end = block_offset + len(entry.data)

                # Check if this block covers the requested range
                if block_offset <= offset < block_end:
                    # Calculate slice within the block
                    start_in_block = offset - block_offset
                    end_in_block = min(start_in_block + size, len(entry.data))
                    available_size = end_in_block - start_in_block

                    if available_size >= size:
                        # Full hit
                        entry.access_count += 1
                        self._stats["hits"] += 1

                        # Update LRU order
                        with contextlib.suppress(ValueError):
                            self._access_order.remove((path, block_offset))
                        self._access_order.append((path, block_offset))

                        return entry.data[start_in_block:end_in_block]

            self._stats["misses"] += 1
            return None

    def put(self, path: str, offset: int, data: bytes) -> bool:
        """Store prefetched data.

        Evicts least-recently-used entries if necessary.

        Args:
            path: File path
            offset: Block offset
            data: Block data

        Returns:
            True if stored, False if couldn't fit
        """
        data_size = len(data)

        # Don't store if larger than entire pool
        if data_size > self._max_size:
            logger.warning(
                f"[READAHEAD] Block too large for buffer pool: {data_size} > {self._max_size}"
            )
            return False

        with self._lock:
            # Check if already exists
            if path in self._buffers and offset in self._buffers[path]:
                return True  # Already have it

            # Evict until we have space
            while self._current_size + data_size > self._max_size:
                if not self._access_order:
                    return False  # Can't fit

                # Evict oldest
                old_path, old_offset = self._access_order.pop(0)
                self._evict(old_path, old_offset)

            # Store new entry
            if path not in self._buffers:
                self._buffers[path] = {}

            self._buffers[path][offset] = BufferEntry(
                path=path,
                offset=offset,
                data=data,
            )
            self._current_size += data_size
            self._stats["bytes_stored"] += data_size
            self._access_order.append((path, offset))

            logger.debug(
                f"[READAHEAD] Buffered {data_size} bytes at {path}:{offset} "
                f"(pool: {self._current_size / (1024 * 1024):.1f}MB)"
            )
            return True

    def _evict(self, path: str, offset: int) -> None:
        """Evict a buffer entry (must hold lock)."""
        if path not in self._buffers:
            return

        entry = self._buffers[path].pop(offset, None)
        if entry is None:
            return

        self._current_size -= len(entry.data)
        self._stats["evictions"] += 1
        self._stats["bytes_evicted"] += len(entry.data)

        # Clean up empty path dict
        if not self._buffers[path]:
            del self._buffers[path]

        logger.debug(f"[READAHEAD] Evicted buffer {path}:{offset}")

    def invalidate_path(self, path: str) -> int:
        """Invalidate all buffers for a path.

        Called on file write/delete.

        Args:
            path: File path to invalidate

        Returns:
            Number of entries invalidated
        """
        with self._lock:
            if path not in self._buffers:
                return 0

            count = len(self._buffers[path])
            for _offset, entry in list(self._buffers[path].items()):
                self._current_size -= len(entry.data)

            del self._buffers[path]

            # Clean up access order
            self._access_order = [(p, o) for p, o in self._access_order if p != path]

            logger.debug(f"[READAHEAD] Invalidated {count} buffers for {path}")
            return count

    def clear(self) -> None:
        """Clear all buffers."""
        with self._lock:
            self._buffers.clear()
            self._access_order.clear()
            self._current_size = 0
            logger.info("[READAHEAD] Buffer pool cleared")

    def get_stats(self) -> dict[str, Any]:
        """Get buffer pool statistics."""
        with self._lock:
            total_entries = sum(len(buffers) for buffers in self._buffers.values())
            return {
                "entries": total_entries,
                "paths": len(self._buffers),
                "size_bytes": self._current_size,
                "size_mb": self._current_size / (1024 * 1024),
                "max_size_mb": self._max_size / (1024 * 1024),
                "utilization": self._current_size / self._max_size if self._max_size > 0 else 0,
                **self._stats,
                "hit_rate": (
                    self._stats["hits"] / (self._stats["hits"] + self._stats["misses"])
                    if (self._stats["hits"] + self._stats["misses"]) > 0
                    else 0
                ),
            }


# =============================================================================
# ReadaheadManager: Main orchestration
# =============================================================================


class ReadaheadManager:
    """Intelligent readahead manager for FUSE operations.

    Orchestrates:
    - Per-file session tracking for pattern detection
    - Async prefetch workers
    - Buffer pool management
    - L2 cache warming

    Usage:
        manager = ReadaheadManager(config, read_func, local_disk_cache)

        # On each read
        prefetched = manager.on_read(fh, path, offset, size)
        if prefetched:
            return prefetched  # Fast path
        else:
            content = backend_read(...)  # Slow path
            manager.on_read_complete(fh, path, offset, content)

        # On file close
        manager.on_release(fh)
    """

    def __init__(
        self,
        config: ReadaheadConfig,
        read_func: Callable[[str, int, int], bytes],
        local_disk_cache: LocalDiskCache | None = None,
        content_hash_func: Callable[[str], str | None] | None = None,
        tenant_id: str | None = None,
    ):
        """Initialize readahead manager.

        Args:
            config: Readahead configuration
            read_func: Function to read data: (path, offset, size) -> bytes
            local_disk_cache: Optional L2 cache for persistent prefetch
            content_hash_func: Function to get content hash for a path
            tenant_id: Tenant ID for multi-tenant cache isolation
        """
        self._config = config
        self._read_func = read_func
        self._local_disk_cache = local_disk_cache
        self._content_hash_func = content_hash_func
        self._tenant_id = tenant_id

        # Session tracking: fh -> ReadSession
        self._sessions: dict[int, ReadSession] = {}
        self._sessions_lock = threading.RLock()

        # Prefetch buffer pool
        self._buffer_pool = PrefetchBufferPool(config.buffer_pool_mb * 1024 * 1024)

        # Prefetch thread pool
        self._executor = ThreadPoolExecutor(
            max_workers=config.prefetch_workers,
            thread_name_prefix="nexus-prefetch",
        )
        self._pending_futures: dict[tuple[str, int], Future] = {}
        self._futures_lock = threading.Lock()

        # Statistics
        self._stats = {
            "prefetch_triggered": 0,
            "prefetch_completed": 0,
            "prefetch_failed": 0,
            "prefetch_cancelled": 0,
            "bytes_prefetched": 0,
            "l2_cache_warms": 0,
        }
        self._stats_lock = threading.Lock()

        # Shutdown flag
        self._shutdown = False

        logger.info(
            f"[READAHEAD] Manager initialized: workers={config.prefetch_workers}, "
            f"buffer={config.buffer_pool_mb}MB, block_size={config.block_size / (1024 * 1024):.1f}MB, "
            f"max_blocks={config.max_blocks_per_trigger}, prefetch_on_open={config.prefetch_on_open}"
        )

    def on_open(self, fh: int, path: str, file_size: int | None = None) -> None:
        """Called when a file is opened.

        If prefetch_on_open is enabled, immediately starts prefetching the
        first blocks of the file. This is based on GCS FUSE's approach where
        most files are read sequentially from offset 0.

        Args:
            fh: File handle
            path: File path
            file_size: Optional file size (for smarter prefetch decisions)
        """
        if not self._config.enabled or not self._config.prefetch_on_open or self._shutdown:
            return

        # Create session for this file handle
        session = self._get_or_create_session(fh, path)

        # Immediately trigger prefetch from offset 0
        # This assumes most files will be read sequentially from start
        # (common pattern for Docker sandbox: cat, head, reading config files)
        logger.debug(f"[READAHEAD] Prefetch on open: {path} (fh={fh})")

        # Set sequential count to trigger prefetch immediately
        session.sequential_count = self._config.min_sequential_count

        # Determine how many blocks to prefetch
        # If we know file size, be smarter about it
        if file_size is not None and file_size > 0:
            # Don't prefetch more than the file
            max_prefetch = min(
                self._config.max_blocks_per_trigger * self._config.block_size,
                file_size,
            )
            num_blocks = max(1, max_prefetch // self._config.block_size)
            num_blocks = min(num_blocks, self._config.max_blocks_per_trigger)
        else:
            # Unknown size, prefetch a reasonable amount
            num_blocks = min(4, self._config.max_blocks_per_trigger)

        # Submit prefetch tasks for first N blocks
        for i in range(num_blocks):
            block_offset = i * self._config.block_size

            if not session.mark_prefetch_pending(block_offset):
                continue

            key = (path, block_offset)
            with self._futures_lock:
                if key in self._pending_futures:
                    continue

                future = self._executor.submit(
                    self._prefetch_block,
                    path,
                    block_offset,
                    self._config.block_size,
                    session,
                )
                self._pending_futures[key] = future

                with self._stats_lock:
                    self._stats["prefetch_triggered"] += 1

        logger.info(f"[READAHEAD] Prefetch on open started: {path} ({num_blocks} blocks)")

    def on_read(self, fh: int, path: str, offset: int, size: int) -> bytes | None:
        """Called before each read operation.

        Checks prefetch buffer for data and triggers new prefetches
        if sequential access is detected.

        Args:
            fh: File handle
            path: File path
            offset: Read offset
            size: Read size

        Returns:
            Prefetched data if available, None otherwise
        """
        if not self._config.enabled or self._shutdown:
            return None

        # Get or create session
        session = self._get_or_create_session(fh, path)

        # Update access pattern
        pattern = session.update_access(offset, size)

        # Check prefetch buffer first
        prefetched = self._buffer_pool.get(path, offset, size)
        if prefetched is not None:
            session.record_prefetch_hit()
            logger.debug(f"[READAHEAD] HIT: {path}:{offset}+{size}")
            return prefetched

        session.record_prefetch_miss()

        # Trigger prefetch if sequential pattern established
        if (
            pattern == AccessPattern.SEQUENTIAL
            and session.sequential_count >= self._config.min_sequential_count
        ):
            self._trigger_prefetch(session, offset + size)

        return None

    def on_read_complete(
        self,
        _fh: int,
        _path: str,
        _offset: int,
        _data: bytes,
    ) -> None:
        """Called after a read completes (cache miss path).

        Can be used for post-read prefetch decisions.

        Args:
            _fh: File handle (unused, reserved for future)
            _path: File path (unused, reserved for future)
            _offset: Read offset (unused, reserved for future)
            _data: Data that was read (unused, reserved for future)
        """
        # Could implement post-read prefetch here if needed
        pass

    def on_release(self, fh: int) -> None:
        """Called when a file handle is released.

        Cleans up session state and cancels pending prefetches.

        Args:
            fh: File handle being released
        """
        with self._sessions_lock:
            session = self._sessions.pop(fh, None)

        if session:
            # Cancel pending prefetches
            with self._futures_lock:
                keys_to_cancel = [
                    (path, offset)
                    for (path, offset) in self._pending_futures
                    if path == session.path
                ]
                for key in keys_to_cancel:
                    future = self._pending_futures.pop(key, None)
                    if future and not future.done():
                        future.cancel()
                        with self._stats_lock:
                            self._stats["prefetch_cancelled"] += 1

            # Invalidate buffers for this path if no other sessions
            other_sessions = [s for s in self._sessions.values() if s.path == session.path]
            if not other_sessions:
                self._buffer_pool.invalidate_path(session.path)

            logger.debug(
                f"[READAHEAD] Session released: {session.path} "
                f"(hits={session.prefetch_hits}, misses={session.prefetch_misses})"
            )

    def invalidate_path(self, path: str) -> None:
        """Invalidate all prefetch state for a path.

        Called on write/delete operations.

        Args:
            path: File path to invalidate
        """
        self._buffer_pool.invalidate_path(path)

        # Cancel pending prefetches for this path
        with self._futures_lock:
            keys_to_cancel = [(p, o) for (p, o) in self._pending_futures if p == path]
            for key in keys_to_cancel:
                future = self._pending_futures.pop(key, None)
                if future and not future.done():
                    future.cancel()

    def _get_or_create_session(self, fh: int, path: str) -> ReadSession:
        """Get or create a read session for a file handle."""
        with self._sessions_lock:
            if fh not in self._sessions:
                self._sessions[fh] = ReadSession(
                    path=path,
                    fh=fh,
                    max_window=self._config.max_window,
                    sequential_tolerance=self._config.sequential_tolerance,
                )
                logger.debug(f"[READAHEAD] New session: fh={fh}, path={path}")
            return self._sessions[fh]

    def _trigger_prefetch(self, session: ReadSession, start_offset: int) -> None:
        """Trigger async prefetch of blocks ahead of current read.

        Submits multiple blocks to ThreadPoolExecutor for truly parallel
        network fetches. This is key for hiding network latency in Docker sandbox.

        Based on GCS FUSE parallel downloads approach.

        Args:
            session: Read session
            start_offset: Offset to start prefetching from
        """
        # Align to block boundary
        block_size = self._config.block_size
        block_start = (start_offset // block_size) * block_size

        # Calculate number of blocks to prefetch based on window size
        # Use max_blocks_per_trigger to control parallelism
        num_blocks = max(1, session.readahead_window // block_size)
        num_blocks = min(num_blocks, self._config.max_blocks_per_trigger)

        blocks_submitted = 0
        for i in range(num_blocks):
            block_offset = block_start + i * block_size

            # Skip if already pending or completed
            if not session.mark_prefetch_pending(block_offset):
                continue

            # Skip if already in buffer
            if self._buffer_pool.get(session.path, block_offset, 1) is not None:
                session.mark_prefetch_completed(block_offset)
                continue

            # Submit prefetch task
            key = (session.path, block_offset)
            with self._futures_lock:
                if key in self._pending_futures:
                    continue

                future = self._executor.submit(
                    self._prefetch_block,
                    session.path,
                    block_offset,
                    block_size,
                    session,
                )
                self._pending_futures[key] = future
                blocks_submitted += 1

                with self._stats_lock:
                    self._stats["prefetch_triggered"] += 1

        if blocks_submitted > 0:
            logger.debug(
                f"[READAHEAD] Prefetch triggered: {session.path}:{block_start} "
                f"({blocks_submitted} blocks, window={session.readahead_window})"
            )

    def _prefetch_block(
        self,
        path: str,
        offset: int,
        size: int,
        session: ReadSession,
    ) -> None:
        """Prefetch a single block (runs in worker thread).

        Args:
            path: File path
            offset: Block offset
            size: Block size
            session: Read session
        """
        if self._shutdown:
            return

        try:
            # Fetch from backend
            data = self._read_func(path, offset, size)

            if data:
                # Store in prefetch buffer
                self._buffer_pool.put(path, offset, data)

                # Warm L2 SSD cache if enabled
                if self._config.warm_l2_cache and self._local_disk_cache:
                    self._warm_l2_cache(path, offset, data)

                session.mark_prefetch_completed(offset)

                with self._stats_lock:
                    self._stats["prefetch_completed"] += 1
                    self._stats["bytes_prefetched"] += len(data)

                logger.debug(f"[READAHEAD] Prefetch complete: {path}:{offset} ({len(data)} bytes)")
            else:
                session.cancel_prefetch(offset)
                with self._stats_lock:
                    self._stats["prefetch_failed"] += 1

        except Exception as e:
            logger.warning(f"[READAHEAD] Prefetch failed: {path}:{offset} - {e}")
            session.cancel_prefetch(offset)
            with self._stats_lock:
                self._stats["prefetch_failed"] += 1

        finally:
            # Clean up future tracking
            key = (path, offset)
            with self._futures_lock:
                self._pending_futures.pop(key, None)

    def _warm_l2_cache(self, path: str, offset: int, data: bytes) -> None:
        """Store prefetched data in L2 SSD cache.

        This makes prefetched data persist across process restarts.

        Args:
            path: File path
            offset: Block offset
            data: Block data
        """
        if self._local_disk_cache is None:
            return

        try:
            # Get content hash for cache key
            if self._content_hash_func:
                content_hash = self._content_hash_func(path)
                if content_hash:
                    # Create block-specific hash
                    block_hash = f"{content_hash}:{offset}"

                    self._local_disk_cache.put(
                        block_hash,
                        data,
                        tenant_id=self._tenant_id,
                        priority=1,  # Higher priority for prefetched data
                    )

                    with self._stats_lock:
                        self._stats["l2_cache_warms"] += 1

                    logger.debug(f"[READAHEAD] L2 cache warmed: {path}:{offset}")

        except Exception as e:
            logger.debug(f"[READAHEAD] L2 cache warm failed: {path}:{offset} - {e}")

    def get_stats(self) -> dict[str, Any]:
        """Get readahead statistics."""
        stats: dict[str, Any] = {}

        with self._stats_lock:
            stats.update(self._stats)

        with self._sessions_lock:
            stats["active_sessions"] = len(self._sessions)
            stats["sessions"] = [s.get_stats() for s in self._sessions.values()]

        stats["buffer_pool"] = self._buffer_pool.get_stats()

        with self._futures_lock:
            stats["pending_prefetches"] = len(self._pending_futures)

        return stats

    def shutdown(self) -> None:
        """Shutdown readahead manager."""
        self._shutdown = True

        # Cancel pending futures
        with self._futures_lock:
            for future in self._pending_futures.values():
                future.cancel()
            self._pending_futures.clear()

        # Shutdown executor
        self._executor.shutdown(wait=False)

        # Clear state
        with self._sessions_lock:
            self._sessions.clear()

        self._buffer_pool.clear()

        logger.info("[READAHEAD] Manager shutdown complete")
