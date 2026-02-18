"""Pending OAuth registration manager using cachetools.TTLCache.

Replaces the hand-rolled TTL dict with a bounded, thread-safe TTL cache.
"""

from __future__ import annotations

import secrets
from typing import Any

from cachetools import TTLCache

from nexus.auth.oauth.types import PendingOAuthRegistration


class PendingOAuthManager:
    """Manages pending OAuth registrations with TTL + maxsize eviction."""

    def __init__(self, ttl_seconds: int = 600, maxsize: int = 1000) -> None:
        self._cache: TTLCache[str, PendingOAuthRegistration] = TTLCache(
            maxsize=maxsize, ttl=ttl_seconds
        )

    def create(
        self,
        provider: str,
        provider_user_id: str,
        provider_email: str | None,
        email_verified: bool,
        name: str | None,
        picture: str | None,
        oauth_credential: Any,
    ) -> str:
        """Create a pending registration and return a secure token."""
        token = secrets.token_urlsafe(32)
        import time

        self._cache[token] = PendingOAuthRegistration(
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=provider_email,
            email_verified=email_verified,
            name=name,
            picture=picture,
            oauth_credential=oauth_credential,
            expires_at=time.time() + self._cache.ttl,
        )
        return token

    def get(self, token: str) -> PendingOAuthRegistration | None:
        """Get pending registration by token (returns None if expired/missing)."""
        return self._cache.get(token)

    def consume(self, token: str) -> PendingOAuthRegistration | None:
        """Get and remove pending registration (one-time use)."""
        return self._cache.pop(token, None)


# Global instance
_pending_oauth_manager: PendingOAuthManager | None = None


def get_pending_oauth_manager() -> PendingOAuthManager:
    """Get the global pending OAuth manager instance."""
    global _pending_oauth_manager
    if _pending_oauth_manager is None:
        _pending_oauth_manager = PendingOAuthManager()
    return _pending_oauth_manager
