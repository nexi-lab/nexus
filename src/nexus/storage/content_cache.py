"""Content cache for fast read operations.

LRU cache that stores file content by hash to avoid disk I/O for frequently
accessed files. Uses size-based eviction to prevent memory bloat.
"""

import threading
from collections import OrderedDict


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

    Example:
        >>> cache = ContentCache(max_size_mb=256)
        >>> cache.put("abc123...", b"file content")
        >>> content = cache.get("abc123...")  # Fast memory read
    """

    def __init__(self, max_size_mb: int = 256):
        """
        Initialize content cache.

        Args:
            max_size_mb: Maximum cache size in megabytes (default: 256 MB)
        """
        self._max_size_bytes = max_size_mb * 1024 * 1024
        self._current_size_bytes = 0
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, content_hash: str) -> bytes | None:
        """
        Get content from cache by hash.

        Thread-safe operation that moves the accessed item to the end of the
        LRU queue (most recently used position).

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
            return self._cache[content_hash]

    def put(self, content_hash: str, content: bytes) -> None:
        """
        Add content to cache with LRU eviction.

        Thread-safe operation that adds content to cache and evicts least
        recently used items if necessary to stay within size limit.

        Args:
            content_hash: SHA-256 hash of content
            content: Content bytes to cache

        Notes:
            - If content is larger than max cache size, it won't be cached
            - Evicts LRU items until there's enough space for new content
            - Updates existing entries and moves them to end of queue
        """
        content_size = len(content)

        # Don't cache content larger than max cache size
        if content_size > self._max_size_bytes:
            return

        with self._lock:
            # If already exists, update and move to end
            if content_hash in self._cache:
                old_size = len(self._cache[content_hash])
                self._current_size_bytes -= old_size
                self._cache[content_hash] = content
                self._current_size_bytes += content_size
                self._cache.move_to_end(content_hash)
                return

            # Evict LRU items until we have space
            while self._current_size_bytes + content_size > self._max_size_bytes and self._cache:
                # Remove least recently used (first item)
                lru_hash, lru_content = self._cache.popitem(last=False)
                self._current_size_bytes -= len(lru_content)

            # Add new content
            self._cache[content_hash] = content
            self._current_size_bytes += content_size

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
            self._current_size_bytes -= len(content)
            return True

    def clear(self) -> None:
        """
        Clear all cached content.

        Thread-safe operation that removes all entries from cache.
        """
        with self._lock:
            self._cache.clear()
            self._current_size_bytes = 0

    def get_stats(self) -> dict[str, int]:
        """
        Get cache statistics.

        Returns:
            Dictionary with cache statistics:
                - entries: Number of cached items
                - size_bytes: Total size of cached content in bytes
                - size_mb: Total size of cached content in megabytes
                - max_size_mb: Maximum cache size in megabytes
        """
        with self._lock:
            return {
                "entries": len(self._cache),
                "size_bytes": self._current_size_bytes,
                "size_mb": self._current_size_bytes // (1024 * 1024),
                "max_size_mb": self._max_size_bytes // (1024 * 1024),
            }
