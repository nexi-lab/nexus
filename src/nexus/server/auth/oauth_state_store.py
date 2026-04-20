"""OAuth CSRF state store (in-memory, TTL-bound).

Implements RFC 6749 §10.12: the authorization server issues a random ``state``
value with each authorize redirect and requires it back on the callback. The
store tracks which state values are currently outstanding; the callback
validates the returned state against it. Tokens are 256-bit random URL-safe
strings, so an attacker cannot forge a value that is present in the store.

Single-node only — state is held in process memory. Deployments running
multiple server instances behind a load balancer need sticky sessions or a
shared store; this mirrors ``PendingOAuthManager`` which is also in-memory.
"""

from __future__ import annotations

from cachetools import TTLCache


class OAuthStateStore:
    """Tracks outstanding OAuth ``state`` values for CSRF protection.

    The store is a TTL+maxsize bounded set: entries expire after ``ttl_seconds``
    (10 minutes by default) and the cache evicts least-recently-inserted items
    once ``maxsize`` is reached, so the store cannot grow without bound even
    under a flood of authorize requests.
    """

    def __init__(self, ttl_seconds: int = 600, maxsize: int = 10_000) -> None:
        self._cache: TTLCache[str, bool] = TTLCache(maxsize=maxsize, ttl=ttl_seconds)

    def register(self, state: str) -> None:
        """Record a state value as outstanding."""
        if not state:
            raise ValueError("state must be a non-empty string")
        self._cache[state] = True

    def consume(self, state: str | None) -> bool:
        """Pop a state and return whether it was outstanding.

        Single-use: a second consume() for the same state returns False, which
        blocks replay of an already-used callback URL.
        """
        if not state:
            return False
        return self._cache.pop(state, None) is not None


_state_store: OAuthStateStore | None = None


def get_oauth_state_store() -> OAuthStateStore:
    """Return the process-wide OAuth state store, creating it on first call."""
    global _state_store
    if _state_store is None:
        _state_store = OAuthStateStore()
    return _state_store
