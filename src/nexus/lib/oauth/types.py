"""OAuth brick core types.

Frozen, immutable data classes for OAuth credentials and pending registrations.
All types use ``@dataclass(frozen=True, slots=True)`` for safety and performance.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


class OAuthError(Exception):
    """OAuth operation failed."""


@dataclass(frozen=True, slots=True)
class OAuthCredential:
    """Immutable OAuth 2.0 credential.

    Use ``dataclasses.replace()`` to create modified copies.
    ``scopes`` is a tuple (hashable) instead of a list.
    ``__repr__`` masks tokens for safe logging.
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: tuple[str, ...] | None = None
    provider: str | None = None
    user_email: str | None = None
    client_id: str | None = None
    token_uri: str | None = None
    metadata: dict[str, Any] | None = field(default=None, hash=False, compare=False)

    def is_expired(self) -> bool:
        """Check if the access token is expired (within 60s safety margin)."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= (self.expires_at - timedelta(seconds=60))

    def needs_refresh(self) -> bool:
        """True if token is expired AND refresh_token is available."""
        return self.is_expired() and self.refresh_token is not None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage/serialization."""
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": list(self.scopes) if self.scopes else None,
            "provider": self.provider,
            "user_email": self.user_email,
            "client_id": self.client_id,
            "token_uri": self.token_uri,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthCredential":
        """Create from dictionary (inverse of ``to_dict``)."""
        expires_at = None
        if data.get("expires_at"):
            if isinstance(data["expires_at"], str):
                expires_at = datetime.fromisoformat(data["expires_at"])
            elif isinstance(data["expires_at"], datetime):
                expires_at = data["expires_at"]

        raw_scopes = data.get("scopes")
        scopes = tuple(raw_scopes) if raw_scopes else None

        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
            provider=data.get("provider"),
            user_email=data.get("user_email"),
            client_id=data.get("client_id"),
            token_uri=data.get("token_uri"),
            metadata=data.get("metadata"),
        )

    def __repr__(self) -> str:
        """Mask tokens for safe logging."""
        masked_access = _mask_token(self.access_token)
        masked_refresh = _mask_token(self.refresh_token) if self.refresh_token else None
        return (
            f"OAuthCredential(access_token='{masked_access}', "
            f"refresh_token='{masked_refresh}', "
            f"provider={self.provider!r}, "
            f"user_email={self.user_email!r})"
        )


@dataclass(frozen=True, slots=True)
class PendingOAuthRegistration:
    """Immutable pending OAuth registration data."""

    provider: str
    provider_user_id: str
    provider_email: str | None
    email_verified: bool
    name: str | None
    picture: str | None
    oauth_credential: Any
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


def _mask_token(token: str) -> str:
    """Mask a token for safe logging: show first 4 chars + '...' + last 1 char."""
    if len(token) <= 6:
        return "***"
    return f"{token[:4]}...{token[-1]}"
