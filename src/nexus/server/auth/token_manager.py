"""Backward-compatibility shim — use nexus.auth.oauth.token_manager instead.

Issue #2281: Moved to nexus.auth.oauth.token_manager (auth brick).
"""

from nexus.bricks.auth.oauth.token_manager import (  # noqa: F401
    _LOCK_ACQUIRE_TIMEOUT_SECONDS,
    _MAX_REFRESH_LOCKS,
    _PROVIDER_REFRESH_TIMEOUT_SECONDS,
    _REFRESH_COOLDOWN_SECONDS,
    _TOKEN_CACHE_TTL_SECONDS,
    TokenManager,
    _hash_token,
)

__all__ = ["TokenManager"]
