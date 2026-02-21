"""Content cache for fast read operations.

LRU cache that stores file content by hash to avoid disk I/O for frequently
accessed files. Uses size-based eviction to prevent memory bloat.

Supports transparent LZ4 compression for 3-4x capacity improvement (Issue #908).
Supports priority-aware eviction from IOProfile cache_priority (Issue #2427).
"""

import logging
import threading
from collections import OrderedDict

import lz4.frame

logger = logging.getLogger(__name__)

# LZ4 frame magic bytes for detection
_LZ4_MAGIC = b"\x04\x22\x4d\x18"


class ContentCache:
    """
    LRU cache for file content indexed by content hash.

    Thread-safe cache that stores file content in memory to avoid repeated
    disk reads. Uses size-based LRU eviction to limit memory usage.

    Features:
    - Size-based eviction (tracks total bytes, not just entry count)
    - Thread-safe operations with fine-grained locking
    - Fast O(1) get/put operations
    - Priority-aware two-pass eviction: low-priority items evicted first
    - Transparent LZ4 compression for content > threshold (3-4x capacity)

    Example:
        >>> cache = ContentCache(max_size_mb=256)
        >>> cache.put("abc123...", b"file content", priority=2)
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
        # Values: (stored_content_bytes, priority)
        self._cache: OrderedDict[str, tuple[bytes, int]] = OrderedDict()
        self._lock = threading.Lock()

        # Compression settings
        self._compression_threshold = compression_threshold
        self._min_compression_ratio = min_compression_ratio

        # Compression stats
        self._original_bytes_total = 0  # Sum of original sizes
        self._compressed_entries = 0  # Number of compressed entries
        self._compression_savings = 0  # Bytes saved by compression

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
            content, _priority = self._cache[content_hash]

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

        Priority values (from IOProfile.cache_priority):
            0 = ARCHIVE — lowest priority, evicted first
            1 = FAST_WRITE / BALANCED — normal priority
            2 = EDIT — elevated priority
            3 = FAST_READ — highest priority, evicted last

        Eviction uses two-pass strategy:
            Pass 1: Evict items with priority <= 1 in LRU order
            Pass 2 (fallback): Evict any item in pure LRU order

        Args:
            content_hash: SHA-256 hash of content
            content: Content bytes to cache
            priority: Cache priority (0-3, default 0). Higher = evicted later.

        Notes:
            - If content is larger than max cache size, it won't be cached
            - Evicts LRU items until there's enough space for new content
            - Updates existing entries and moves them to end of queue
            - Re-putting with higher priority upgrades the stored priority
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

        with self._lock:
            # If already exists, update and move to end
            if content_hash in self._cache:
                old_content, old_priority = self._cache[content_hash]
                old_size = len(old_content)
                old_was_compressed = len(old_content) >= 4 and old_content[:4] == _LZ4_MAGIC

                # Update size tracking
                self._current_size_bytes -= old_size
                if old_was_compressed:
                    self._compressed_entries -= 1

                # Use max of old and new priority (upgrade only)
                effective_priority = max(old_priority, priority)

                self._cache[content_hash] = (stored_content, effective_priority)
                self._current_size_bytes += content_size

                if is_compressed:
                    self._compressed_entries += 1
                    self._compression_savings += original_size - content_size
                    self._original_bytes_total += original_size
                else:
                    self._original_bytes_total += original_size

                self._cache.move_to_end(content_hash)
                return

            # Two-pass eviction
            self._evict_until_fits(content_size)

            # Add new content
            self._cache[content_hash] = (stored_content, priority)
            self._current_size_bytes += content_size

            # Update compression stats
            if is_compressed:
                self._compressed_entries += 1
                self._compression_savings += original_size - content_size
            self._original_bytes_total += original_size

    def _evict_until_fits(self, needed_bytes: int) -> None:
        """Evict entries until needed_bytes can fit. Must hold self._lock.

        Uses two-pass strategy:
        - Pass 1: evict low-priority items (priority <= 1) in LRU order
        - Pass 2: evict any item in pure LRU order (fallback)
        """
        # Pass 1: evict low-priority items first
        while self._current_size_bytes + needed_bytes > self._max_size_bytes and self._cache:
            evicted = False
            for key in list(self._cache.keys()):
                stored_content, pri = self._cache[key]
                if pri <= 1:
                    del self._cache[key]
                    self._current_size_bytes -= len(stored_content)
                    if len(stored_content) >= 4 and stored_content[:4] == _LZ4_MAGIC:
                        self._compressed_entries -= 1
                    evicted = True
                    break
            if not evicted:
                break

        # Pass 2: fallback — evict any item (pure LRU from front)
        while self._current_size_bytes + needed_bytes > self._max_size_bytes and self._cache:
            _lru_hash, (lru_content, _pri) = self._cache.popitem(last=False)
            self._current_size_bytes -= len(lru_content)
            if len(lru_content) >= 4 and lru_content[:4] == _LZ4_MAGIC:
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

            content, _priority = self._cache.pop(content_hash)
            content_size = len(content)
            self._current_size_bytes -= content_size

            # Update compression stats
            if len(content) >= 4 and content[:4] == _LZ4_MAGIC:
                self._compressed_entries -= 1

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

    def get_stats(self) -> dict[str, int | float | dict[int, int]]:
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
                - priority_distribution: Count of entries per priority level
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

            # Priority distribution
            priority_dist: dict[int, int] = {0: 0, 1: 0, 2: 0, 3: 0}
            for _content, pri in self._cache.values():
                priority_dist[pri] = priority_dist.get(pri, 0) + 1

            return {
                "entries": entries,
                "size_bytes": self._current_size_bytes,
                "size_mb": self._current_size_bytes // (1024 * 1024),
                "max_size_mb": self._max_size_bytes // (1024 * 1024),
                "compressed_entries": self._compressed_entries,
                "compression_ratio": round(compression_ratio, 3),
                "compression_savings_bytes": self._compression_savings,
                "effective_capacity_mb": int(effective_capacity // (1024 * 1024)),
                "priority_distribution": priority_dist,
            }
