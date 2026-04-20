"""OAuth CSRF state store (in-memory, TTL-bound) with browser-session binding.

Implements RFC 6749 §10.12 plus the OAuth 2.0 Security BCP recommendation to
bind ``state`` to the user-agent that started the flow. On each authorize
redirect the server generates two values:

* ``state`` — opaque, embedded in the Google redirect URL and echoed back.
* ``binding_nonce`` — stored server-side against the state AND set in an
  HttpOnly, SameSite=Lax cookie on the browser. Never leaves the origin.

On callback the server requires both to match. A state leaked out of the
authorization URL (e.g. an attacker who completed OAuth in their own
browser and forwarded ``(code, state)`` to a victim) is not replayable
in a different browser because that browser does not hold the bound
``binding_nonce`` cookie — this blocks OAuth login-fixation / account
takeover.

.. warning::

   **Process-local; not safe across multiple workers or server nodes.**
   The store lives in a single Python process. If the authorize request
   lands on worker A and the callback on worker B, the callback will be
   rejected as "invalid state". Deployments running multiple workers
   (``--workers >1``) or multiple server replicas behind a load balancer
   MUST either:

   1. Use sticky sessions pinned to one backend, or
   2. Replace this store with a shared backend (Redis, database row).

   This mirrors the constraint on ``PendingOAuthManager``. Single-worker
   single-node deployments are unaffected.
"""

from __future__ import annotations

import hmac

from cachetools import TTLCache


class OAuthStateStore:
    """Tracks outstanding OAuth ``state → binding_nonce`` pairs.

    The store is a TTL+maxsize bounded mapping: entries expire after
    ``ttl_seconds`` (10 minutes by default) and the cache evicts
    least-recently-inserted items once ``maxsize`` is reached, so the
    store cannot grow without bound even under a flood of authorize
    requests.
    """

    def __init__(self, ttl_seconds: int = 600, maxsize: int = 10_000) -> None:
        self._cache: TTLCache[str, str] = TTLCache(maxsize=maxsize, ttl=ttl_seconds)

    def register(self, state: str, binding_nonce: str) -> None:
        """Record a state value bound to a browser nonce.

        Both values must be non-empty; ``binding_nonce`` is what the caller
        will also set in an HttpOnly cookie on the initiating browser.
        """
        if not state:
            raise ValueError("state must be a non-empty string")
        if not binding_nonce:
            raise ValueError("binding_nonce must be a non-empty string")
        self._cache[state] = binding_nonce

    def consume(self, state: str | None, binding_nonce: str | None) -> bool:
        """Pop a state and verify it was bound to ``binding_nonce``.

        Returns ``True`` only if the state was outstanding AND the supplied
        nonce matches the one registered. Comparison is constant-time.

        Single-use: a second consume() for the same state returns ``False``
        even with the correct nonce, which blocks replay of an already-used
        callback URL.
        """
        if not state or not binding_nonce:
            return False
        stored = self._cache.pop(state, None)
        if stored is None:
            return False
        return hmac.compare_digest(stored, binding_nonce)


_state_store: OAuthStateStore | None = None


def get_oauth_state_store() -> OAuthStateStore:
    """Return the process-wide OAuth state store, creating it on first call.

    See module docstring for the multi-worker / multi-node caveat.
    """
    global _state_store
    if _state_store is None:
        _state_store = OAuthStateStore()
    return _state_store
