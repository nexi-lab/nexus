"""Auth result caching with invalidation support (Decisions #2, #15).

Extracted from server/dependencies.py. Provides TTL-based caching
for authentication results with explicit invalidation for revocation.
"""

import hashlib
import logging
from typing import Any

from cachetools import TTLCache

logger = logging.getLogger(__name__)

# Defaults
_DEFAULT_TTL = 900  # 15 minutes
_DEFAULT_MAX_SIZE = 1000

class AuthCache:
    """TTL cache for auth results with invalidation support.

    Thread-safe for concurrent reads (cachetools TTLCache is thread-safe
    for single operations). Callers get copies to prevent mutation.
    """

    def __init__(
        self,
        ttl: int = _DEFAULT_TTL,
        max_size: int = _DEFAULT_MAX_SIZE,
    ) -> None:
        self._cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=max_size, ttl=ttl)

    @staticmethod
    def _token_hash(token: str) -> str:
        """Compute a truncated SHA-256 hash for cache key."""
        return hashlib.sha256(token.encode()).hexdigest()[:32]

    def get(self, token: str) -> dict[str, Any] | None:
        """Retrieve cached auth result (returns a copy for mutation safety).

        Args:
            token: Raw token string.

        Returns:
            Shallow copy of cached result dict, or None on miss.
        """
        cached = self._cache.get(self._token_hash(token))
        if cached is not None:
            return dict(cached)
        return None

    def set(self, token: str, result: dict[str, Any]) -> None:
        """Cache an auth result.

        Args:
            token: Raw token string.
            result: Auth result dict (will be stored as-is; caller
                    should strip per-request fields before caching).
        """
        self._cache[self._token_hash(token)] = result

    def invalidate(self, token: str) -> None:
        """Remove a specific token from the cache (Decision #15).

        Called on key revocation to ensure immediate effect.

        Args:
            token: Raw token string to invalidate.
        """
        key = self._token_hash(token)
        try:
            del self._cache[key]
            logger.debug("Cache invalidated for token hash %s...", key[:8])
        except KeyError:
            pass

    def clear(self) -> None:
        """Clear the entire cache. Used by tests for isolation."""
        self._cache.clear()

    @property
    def size(self) -> int:
        """Current number of entries in the cache."""
        return len(self._cache)
