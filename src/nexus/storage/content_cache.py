"""Content cache for fast read operations.

LRU cache that stores file content by hash to avoid disk I/O for frequently
accessed files. Uses size-based eviction to prevent memory bloat.

Supports transparent LZ4 compression for 3-4x capacity improvement (Issue #908).
Priority-aware two-pass eviction (Issue #2427).
"""

import logging
import threading
from collections import OrderedDict
from typing import NamedTuple

import lz4.frame

logger = logging.getLogger(__name__)

# LZ4 frame magic bytes for detection
_LZ4_MAGIC = b"\x04\x22\x4d\x18"


class _CacheEntry(NamedTuple):
    """Internal cache entry with metadata alongside stored content."""

    content: bytes  # Possibly LZ4-compressed
    priority: int  # 0=minimal, 1=low, 2=medium, 3=high
    original_size: int  # Pre-compression size for accurate stats


class ContentCache:
    """
    LRU cache for file content indexed by content hash.

    Thread-safe cache that stores file content in memory to avoid repeated
    disk reads. Uses size-based two-pass LRU eviction to limit memory usage.

    Features:
    - Size-based eviction (tracks total bytes, not just entry count)
    - Priority-aware two-pass eviction: first pass evicts priority=0 only,
      second pass evicts any entry (mirrors LocalDiskCache CLOCK pattern)
    - Thread-safe operations with fine-grained locking
    - Fast O(1) get/put operations
    - Automatic eviction of least-recently-used content when size limit exceeded
    - Transparent LZ4 compression for content > threshold (3-4x capacity)

    Example:
        >>> cache = ContentCache(max_size_mb=256)
        >>> cache.put("abc123...", b"file content", priority=3)
        >>> content = cache.get("abc123...")  # Fast memory read
    """

    def __init__(
        self,
        max_size_mb: int = 256,
        compression_threshold: int = 1024,
        min_compression_ratio: float = 0.9,
    ):
        """
        Initialize content cache.

        Args:
            max_size_mb: Maximum cache size in megabytes (default: 256 MB)
            compression_threshold: Minimum content size in bytes to compress (default: 1KB)
            min_compression_ratio: Only use compression if result is smaller than this
                                   ratio of original (default: 0.9 = 10% savings minimum)
        """
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._current_size_bytes = 0
        self._cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()

        # Compression settings
        self._compression_threshold = compression_threshold
        self._min_compression_ratio = min_compression_ratio

        # Compression stats
        self._original_bytes_total = 0  # Sum of original sizes
        self._compressed_entries = 0  # Number of compressed entries
        self._compression_savings = 0  # Bytes saved by compression

        # Priority tracking for fast first-pass skip
        self._priority_zero_count = 0

    def get(self, content_hash: str) -> bytes | None:
        """
        Get content from cache by hash.

        Thread-safe operation that moves the accessed item to the end of the
        LRU queue (most recently used position). Transparently decompresses
        LZ4-compressed content.

        Args:
            content_hash: SHA-256 hash of content to retrieve

        Returns:
            Content bytes if found in cache, None otherwise
        """
        with self._lock:
            if content_hash not in self._cache:
                return None

            # Move to end (most recently used)
            self._cache.move_to_end(content_hash)
            entry = self._cache[content_hash]
            content = entry.content

            # Transparently decompress if LZ4-compressed
            if len(content) >= 4 and content[:4] == _LZ4_MAGIC:
                # Suppress decompression errors for content that starts with
                # LZ4 magic but isn't valid LZ4 (e.g., random binary data)
                try:
                    content = lz4.frame.decompress(content)
                except (ValueError, Exception) as e:
                    logger.debug("[CACHE] Decompression failed, treating as miss: %s", e)

            return content

    def put(self, content_hash: str, content: bytes, *, priority: int = 0) -> None:
        """
        Add content to cache with priority-aware LRU eviction.

        Thread-safe operation that adds content to cache and evicts least
        recently used items if necessary to stay within size limit.
        Content above compression threshold is transparently LZ4-compressed.

        Two-pass eviction:
        - First pass: evict only priority=0 entries (LRU order)
        - Second pass: evict any entry (LRU order) if first pass insufficient

        Args:
            content_hash: SHA-256 hash of content
            content: Content bytes to cache
            priority: Cache priority (0=minimal, 1=low, 2=medium, 3=high).
                      Higher priority entries survive eviction longer.

        Notes:
            - If content is larger than max cache size, it won't be cached
            - Evicts LRU items until there's enough space for new content
            - Updates existing entries and moves them to end of queue
            - Content > compression_threshold is LZ4-compressed if beneficial
        """
        original_size = len(content)

        # Don't cache content larger than max cache size
        if original_size > self._max_size_bytes:
            return

        # Compress if above threshold
        stored_content = content
        is_compressed = False
        if original_size > self._compression_threshold:
            compressed = lz4.frame.compress(content, compression_level=0)
            # Only use compressed if actually smaller by min ratio
            if len(compressed) < original_size * self._min_compression_ratio:
                stored_content = compressed
                is_compressed = True

        content_size = len(stored_content)
        new_entry = _CacheEntry(
            content=stored_content,
            priority=priority,
            original_size=original_size,
        )

        with self._lock:
            # If already exists, update and move to end
            if content_hash in self._cache:
                old_entry = self._cache[content_hash]
                old_size = len(old_entry.content)

                # Update priority-zero counter
                if old_entry.priority == 0 and priority != 0:
                    self._priority_zero_count -= 1
                elif old_entry.priority != 0 and priority == 0:
                    self._priority_zero_count += 1

                # Update size tracking
                self._current_size_bytes -= old_size
                old_was_compressed = (
                    len(old_entry.content) >= 4 and old_entry.content[:4] == _LZ4_MAGIC
                )
                if old_was_compressed:
                    self._compressed_entries -= 1

                self._cache[content_hash] = new_entry
                self._current_size_bytes += content_size

                if is_compressed:
                    self._compressed_entries += 1
                    self._compression_savings += original_size - content_size
                self._original_bytes_total += original_size

                self._cache.move_to_end(content_hash)
                return

            # Evict entries using two-pass strategy
            self._evict_for_space(content_size)

            # Add new content
            self._cache[content_hash] = new_entry
            self._current_size_bytes += content_size
            if priority == 0:
                self._priority_zero_count += 1

            # Update compression stats
            if is_compressed:
                self._compressed_entries += 1
                self._compression_savings += original_size - content_size
            self._original_bytes_total += original_size

    def _evict_for_space(self, needed: int) -> None:
        """Two-pass LRU eviction: priority-0 first, then any.

        Must be called while holding self._lock.
        """
        space_needed = self._current_size_bytes + needed - self._max_size_bytes
        if space_needed <= 0:
            return

        freed = 0

        # First pass: only evict priority=0 entries (if any exist)
        if self._priority_zero_count > 0:
            to_evict: list[str] = []
            for key, entry in self._cache.items():
                if freed >= space_needed:
                    break
                if entry.priority == 0:
                    to_evict.append(key)
                    freed += len(entry.content)

            for key in to_evict:
                self._remove_entry_unlocked(key)

        # Second pass: evict any entry if first pass wasn't enough
        if freed < space_needed:
            to_evict_2: list[str] = []
            remaining = space_needed - freed
            for key, entry in self._cache.items():
                if remaining <= 0:
                    break
                to_evict_2.append(key)
                remaining -= len(entry.content)

            for key in to_evict_2:
                self._remove_entry_unlocked(key)

    def _remove_entry_unlocked(self, content_hash: str) -> None:
        """Remove a cache entry without acquiring the lock.

        Must be called while holding self._lock.
        """
        entry = self._cache.pop(content_hash)
        entry_size = len(entry.content)
        self._current_size_bytes -= entry_size

        if entry.priority == 0:
            self._priority_zero_count -= 1

        # Update compression stats for evicted entry
        if len(entry.content) >= 4 and entry.content[:4] == _LZ4_MAGIC:
            self._compressed_entries -= 1

    def remove(self, content_hash: str) -> bool:
        """
        Remove a specific entry from cache.

        Thread-safe operation that removes a single entry by key.

        Args:
            content_hash: Key of the entry to remove

        Returns:
            True if entry was removed, False if not found
        """
        with self._lock:
            if content_hash not in self._cache:
                return False
            self._remove_entry_unlocked(content_hash)
            return True

    def clear(self) -> None:
        """
        Clear all cached content.

        Thread-safe operation that removes all entries from cache.
        """
        with self._lock:
            self._cache.clear()
            self._current_size_bytes = 0
            self._original_bytes_total = 0
            self._compressed_entries = 0
            self._compression_savings = 0
            self._priority_zero_count = 0

    def get_stats(self) -> dict[str, int | float]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics:
                - entries: Number of cached items
                - size_bytes: Total size of cached content in bytes (compressed)
                - size_mb: Total size of cached content in megabytes (compressed)
                - max_size_mb: Maximum cache size in megabytes
                - compressed_entries: Number of LZ4-compressed entries
                - compression_ratio: Ratio of compressed to original size (lower is better)
                - compression_savings_bytes: Total bytes saved by compression
                - effective_capacity_mb: Estimated original content capacity
                - priority_zero_count: Number of entries with priority=0
        """
        with self._lock:
            entries = len(self._cache)
            compression_ratio = (
                self._current_size_bytes / self._original_bytes_total
                if self._original_bytes_total > 0
                else 1.0
            )
            # Effective capacity = how much original content we could store
            effective_capacity = (
                self._max_size_bytes / compression_ratio
                if compression_ratio > 0
                else self._max_size_bytes
            )

            return {
                "entries": entries,
                "size_bytes": self._current_size_bytes,
                "size_mb": self._current_size_bytes // (1024 * 1024),
                "max_size_mb": self._max_size_bytes // (1024 * 1024),
                "compressed_entries": self._compressed_entries,
                "compression_ratio": round(compression_ratio, 3),
                "compression_savings_bytes": self._compression_savings,
                "effective_capacity_mb": int(effective_capacity // (1024 * 1024)),
                "priority_zero_count": self._priority_zero_count,
            }
