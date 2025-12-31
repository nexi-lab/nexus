"""Content cache for fast read operations.

LRU cache that stores file content by hash to avoid disk I/O for frequently
accessed files. Uses size-based eviction to prevent memory bloat.

Supports transparent LZ4 compression for 3-4x capacity improvement (Issue #908).
"""

import contextlib
import threading
from collections import OrderedDict

import lz4.frame

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
    - Automatic eviction of least-recently-used content when size limit exceeded
    - Transparent LZ4 compression for content > threshold (3-4x capacity)

    Example:
        >>> cache = ContentCache(max_size_mb=256)
        >>> cache.put("abc123...", b"file content")
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
        self._cache: OrderedDict[str, bytes] = OrderedDict()
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
            content = self._cache[content_hash]

            # Transparently decompress if LZ4-compressed
            if len(content) >= 4 and content[:4] == _LZ4_MAGIC:
                # Suppress decompression errors for content that starts with
                # LZ4 magic but isn't valid LZ4 (e.g., random binary data)
                with contextlib.suppress(Exception):
                    content = lz4.frame.decompress(content)

            return content

    def put(self, content_hash: str, content: bytes) -> None:
        """
        Add content to cache with LRU eviction.

        Thread-safe operation that adds content to cache and evicts least
        recently used items if necessary to stay within size limit.
        Content above compression threshold is transparently LZ4-compressed.

        Args:
            content_hash: SHA-256 hash of content
            content: Content bytes to cache

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

        with self._lock:
            # If already exists, update and move to end
            if content_hash in self._cache:
                old_content = self._cache[content_hash]
                old_size = len(old_content)
                old_was_compressed = len(old_content) >= 4 and old_content[:4] == _LZ4_MAGIC

                # Update size tracking
                self._current_size_bytes -= old_size
                if old_was_compressed:
                    self._compressed_entries -= 1
                    # Approximate: we stored compressed, so original was larger
                    # We'll lose some precision here, but it's stats only

                self._cache[content_hash] = stored_content
                self._current_size_bytes += content_size

                if is_compressed:
                    self._compressed_entries += 1
                    self._compression_savings += original_size - content_size
                    self._original_bytes_total += original_size
                else:
                    self._original_bytes_total += original_size

                self._cache.move_to_end(content_hash)
                return

            # Evict LRU items until we have space
            while self._current_size_bytes + content_size > self._max_size_bytes and self._cache:
                # Remove least recently used (first item)
                lru_hash, lru_content = self._cache.popitem(last=False)
                lru_size = len(lru_content)
                self._current_size_bytes -= lru_size

                # Update compression stats for evicted entry
                if len(lru_content) >= 4 and lru_content[:4] == _LZ4_MAGIC:
                    self._compressed_entries -= 1

            # Add new content
            self._cache[content_hash] = stored_content
            self._current_size_bytes += content_size

            # Update compression stats
            if is_compressed:
                self._compressed_entries += 1
                self._compression_savings += original_size - content_size
            self._original_bytes_total += original_size

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

            content = self._cache.pop(content_hash)
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
            }
