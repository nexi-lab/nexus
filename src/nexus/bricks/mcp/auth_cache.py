"""In-process auth identity cache for MCP HTTP transport (#3779).

Caches the result of `auth_provider.authenticate(api_key)` for a short
TTL so that each MCP tool call does not incur a ~10s async-to-sync
round-trip. Only positive results are cached — failed auth retries
immediately (no negative caching).
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from cachetools import TTLCache


@dataclass(frozen=True)
class ResolvedIdentity:
    """Minimal identity fields needed by MCP tool handlers."""

    subject_id: str
    zone_id: str
    is_admin: bool
    tier: str  # "anonymous" | "authenticated" | "premium"


class AuthIdentityCache:
    """Thread-safe TTL cache keyed by a hash of the API key.

    Stores only positive results (`ResolvedIdentity`). `get_or_resolve()`
    calls the supplied resolver on miss and caches a non-None result.
    """

    def __init__(self, maxsize: int = 1024, ttl: int = 60) -> None:
        self._cache: TTLCache[str, ResolvedIdentity] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.RLock()

    def get(self, key_hash: str) -> ResolvedIdentity | None:
        with self._lock:
            result: ResolvedIdentity | None = self._cache.get(key_hash)
            return result

    def put(self, key_hash: str, identity: ResolvedIdentity) -> None:
        with self._lock:
            self._cache[key_hash] = identity

    def invalidate(self, key_hash: str) -> None:
        with self._lock:
            self._cache.pop(key_hash, None)

    def get_or_resolve(
        self,
        key_hash: str,
        resolver: Callable[[], ResolvedIdentity | None],
    ) -> ResolvedIdentity | None:
        hit = self.get(key_hash)
        if hit is not None:
            return hit
        resolved = resolver()
        if resolved is not None:
            self.put(key_hash, resolved)
        return resolved


def hash_api_key(api_key: str) -> str:
    """Return first 16 hex chars of sha256(api_key). Never stores raw keys."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


_SINGLETON_LOCK: Final = threading.Lock()
_singleton: AuthIdentityCache | None = None


def get_auth_identity_cache() -> AuthIdentityCache:
    """Process-wide singleton."""
    global _singleton
    with _SINGLETON_LOCK:
        if _singleton is None:
            _singleton = AuthIdentityCache()
        return _singleton


def _reset_singleton_for_tests() -> None:
    """Only for tests — clears the module-level singleton."""
    global _singleton
    with _SINGLETON_LOCK:
        _singleton = None
