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
    """Minimal identity fields needed by MCP tool handlers.

    zone_set/zone_perms coexist for back-compat (#3785 F3c): zone_perms is
    canonical when both are passed; otherwise the missing one is derived.
    """

    subject_id: str
    zone_id: str
    is_admin: bool
    tier: str  # "anonymous" | "authenticated" | "premium"
    subject_type: str = "user"
    agent_generation: int | None = None
    inherit_permissions: bool | None = None
    zone_set: tuple[str, ...] = ()  # #3785: full zone allow-list for this token
    zone_perms: tuple[tuple[str, str], ...] = ()  # #3785 F3c: per-zone perms

    def __post_init__(self) -> None:
        # Frozen dataclass: must use object.__setattr__ to sync the two fields.
        if self.zone_perms:
            object.__setattr__(self, "zone_set", tuple(z for z, _ in self.zone_perms))
        elif self.zone_set:
            object.__setattr__(self, "zone_perms", tuple((z, "rw") for z in self.zone_set))


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
            return self._cache.get(key_hash)

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
        # Lock held across resolver call so concurrent misses for the
        # same key don't each fire a 10s auth round-trip.
        with self._lock:
            hit = self._cache.get(key_hash)
            if hit is not None:
                return hit
            resolved = resolver()
            if resolved is not None:
                self._cache[key_hash] = resolved
            return resolved


def hash_api_key(api_key: str) -> str:
    """Returns a 16-char hex prefix of sha256(api_key).

    Used only as a cache/lookup key — not a cryptographic commitment.
    Raw keys are never stored.
    """
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
