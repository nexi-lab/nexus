"""In-memory message deduplication.

Uses TTLCache to prevent processing duplicate messages within a time window.
"""

from __future__ import annotations

import logging
import threading

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Default settings
DEFAULT_TTL = 3600  # 1 hour
DEFAULT_MAX_SIZE = 100_000  # 100k entries


class Deduplicator:
    """Thread-safe message deduplication using TTLCache.

    Tracks message IDs to prevent duplicate processing within a time window.
    Entries automatically expire after TTL.
    """

    def __init__(
        self,
        ttl: int = DEFAULT_TTL,
        max_size: int = DEFAULT_MAX_SIZE,
    ) -> None:
        """Initialize the deduplicator.

        Args:
            ttl: Time-to-live in seconds for cache entries
            max_size: Maximum number of entries to track
        """
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=max_size, ttl=ttl)
        self._lock = threading.Lock()
        logger.debug(f"Deduplicator initialized: ttl={ttl}s, max_size={max_size}")

    def is_duplicate(self, message_id: str) -> bool:
        """Check if a message ID has been seen recently.

        Args:
            message_id: Unique message identifier

        Returns:
            True if message was already processed within TTL window
        """
        with self._lock:
            return message_id in self._cache

    def mark_processed(self, message_id: str) -> None:
        """Mark a message ID as processed.

        Args:
            message_id: Unique message identifier
        """
        with self._lock:
            self._cache[message_id] = True
            logger.debug(f"Marked message {message_id} as processed")

    def check_and_mark(self, message_id: str) -> bool:
        """Atomically check if duplicate and mark as processed.

        Args:
            message_id: Unique message identifier

        Returns:
            True if this is a NEW message (not a duplicate)
            False if this is a duplicate
        """
        with self._lock:
            if message_id in self._cache:
                return False
            self._cache[message_id] = True
            return True

    def clear(self) -> None:
        """Clear all tracked message IDs."""
        with self._lock:
            self._cache.clear()
            logger.debug("Deduplicator cache cleared")

    @property
    def size(self) -> int:
        """Get current number of tracked message IDs."""
        with self._lock:
            return len(self._cache)
