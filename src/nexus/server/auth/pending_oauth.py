"""Pending OAuth registration manager.

Stores temporary OAuth registration data for new users before account confirmation.
Uses in-memory storage with TTL for simplicity and security.
"""

import secrets
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class PendingOAuthRegistration:
    """Pending OAuth registration data."""

    provider: str
    provider_user_id: str
    provider_email: str | None
    email_verified: bool
    name: str | None
    picture: str | None
    oauth_credential: Any  # Store the OAuth credential object (with tokens)
    expires_at: float

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "provider": self.provider,
            "provider_user_id": self.provider_user_id,
            "provider_email": self.provider_email,
            "email_verified": self.email_verified,
            "name": self.name,
            "picture": self.picture,
        }


class PendingOAuthManager:
    """Manages pending OAuth registrations with TTL."""

    def __init__(self, ttl_seconds: int = 600):
        """Initialize pending OAuth manager.

        Args:
            ttl_seconds: Time-to-live for pending registrations (default: 10 minutes)
        """
        self._storage: dict[str, PendingOAuthRegistration] = {}
        self._ttl_seconds = ttl_seconds

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
        """Create a pending registration and return a token.

        Args:
            provider: OAuth provider name
            provider_user_id: User ID from provider
            provider_email: Email from provider
            email_verified: Whether email is verified
            name: Display name from provider
            picture: Avatar URL from provider
            oauth_credential: OAuth credential object (with access_token, refresh_token, etc.)

        Returns:
            Pending token (URL-safe random string)
        """
        # Clean up expired entries first
        self._cleanup_expired()

        # Generate secure token
        token = secrets.token_urlsafe(32)

        # Store registration
        self._storage[token] = PendingOAuthRegistration(
            provider=provider,
            provider_user_id=provider_user_id,
            provider_email=provider_email,
            email_verified=email_verified,
            name=name,
            picture=picture,
            oauth_credential=oauth_credential,
            expires_at=time.time() + self._ttl_seconds,
        )

        return token

    def get(self, token: str) -> PendingOAuthRegistration | None:
        """Get pending registration by token.

        Args:
            token: Pending token

        Returns:
            PendingOAuthRegistration if found and not expired, None otherwise
        """
        # Clean up expired entries
        self._cleanup_expired()

        registration = self._storage.get(token)
        if registration is None:
            return None

        # Double-check expiry
        if registration.expires_at < time.time():
            del self._storage[token]
            return None

        return registration

    def consume(self, token: str) -> PendingOAuthRegistration | None:
        """Get and remove pending registration (one-time use).

        Args:
            token: Pending token

        Returns:
            PendingOAuthRegistration if found and not expired, None otherwise
        """
        registration = self.get(token)
        if registration is None:
            return None

        # Remove from storage (one-time use)
        del self._storage[token]
        return registration

    def _cleanup_expired(self) -> None:
        """Remove expired registrations."""
        now = time.time()
        expired_tokens = [token for token, reg in self._storage.items() if reg.expires_at < now]
        for token in expired_tokens:
            del self._storage[token]


# Global instance (shared across all requests)
_pending_oauth_manager: PendingOAuthManager | None = None


def get_pending_oauth_manager() -> PendingOAuthManager:
    """Get the global pending OAuth manager instance."""
    global _pending_oauth_manager
    if _pending_oauth_manager is None:
        _pending_oauth_manager = PendingOAuthManager()
    return _pending_oauth_manager
