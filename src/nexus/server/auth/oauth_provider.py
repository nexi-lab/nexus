"""OAuth 2.0 provider abstraction.

Defines the interface for OAuth providers (Google, Microsoft, etc.) and common
OAuth credential structures. Each provider implements the OAuthProvider interface
to handle provider-specific OAuth flows.

Based on MindsDB's provider-specific manager pattern but with centralized management.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class OAuthCredential:
    """OAuth 2.0 credential data structure.

    This represents the decrypted OAuth tokens and metadata.
    For storage, tokens are encrypted using OAuthCrypto.

    Attributes:
        access_token: OAuth access token for API calls
        refresh_token: OAuth refresh token (optional, used to get new access tokens)
        token_type: Token type (usually "Bearer")
        expires_at: When the access token expires (UTC)
        scopes: List of granted OAuth scopes
        provider: OAuth provider name (google, microsoft, etc.)
        user_email: User's email address
        client_id: OAuth client ID (optional)
        token_uri: Token refresh endpoint (optional)
        metadata: Additional provider-specific metadata

    Example:
        >>> cred = OAuthCredential(
        ...     access_token="ya29.a0ARrdaM...",
        ...     refresh_token="1//0e...",
        ...     token_type="Bearer",
        ...     expires_at=datetime.now(UTC) + timedelta(hours=1),
        ...     scopes=["https://www.googleapis.com/auth/drive"],
        ...     provider="google",
        ...     user_email="alice@example.com"
        ... )
        >>> cred.is_expired()
        False
    """

    access_token: str
    refresh_token: str | None = None
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    scopes: list[str] | None = None
    provider: str | None = None
    user_email: str | None = None
    client_id: str | None = None
    token_uri: str | None = None
    metadata: dict[str, Any] | None = None

    def is_expired(self) -> bool:
        """Check if the access token is expired.

        Returns:
            True if token is expired or expires within 60 seconds
        """
        if self.expires_at is None:
            return False

        # Consider expired if expires within 60 seconds (safety margin)
        return datetime.now(UTC) >= (self.expires_at - timedelta(seconds=60))

    def needs_refresh(self) -> bool:
        """Check if the token needs refresh.

        Returns:
            True if token is expired AND refresh_token is available
        """
        return self.is_expired() and self.refresh_token is not None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for storage.

        Returns:
            Dictionary representation suitable for JSON serialization
        """
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "scopes": self.scopes,
            "provider": self.provider,
            "user_email": self.user_email,
            "client_id": self.client_id,
            "token_uri": self.token_uri,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OAuthCredential":
        """Create from dictionary.

        Args:
            data: Dictionary representation (from to_dict or database)

        Returns:
            OAuthCredential instance
        """
        # Parse expires_at if present
        expires_at = None
        if data.get("expires_at"):
            if isinstance(data["expires_at"], str):
                expires_at = datetime.fromisoformat(data["expires_at"])
            elif isinstance(data["expires_at"], datetime):
                expires_at = data["expires_at"]

        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_type=data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=data.get("scopes"),
            provider=data.get("provider"),
            user_email=data.get("user_email"),
            client_id=data.get("client_id"),
            token_uri=data.get("token_uri"),
            metadata=data.get("metadata"),
        )


class OAuthProvider(ABC):
    """Abstract base class for OAuth 2.0 providers.

    Each provider (Google, Microsoft, Dropbox, etc.) implements this interface
    to handle provider-specific OAuth flows and token refresh logic.

    Implementation pattern follows MindsDB's provider-specific managers:
    - GoogleOAuth2Manager -> GoogleOAuthProvider
    - MicrosoftOAuth2Manager -> MicrosoftOAuthProvider

    Key responsibilities:
    1. Generate authorization URLs for OAuth flow
    2. Exchange authorization codes for tokens
    3. Refresh expired access tokens
    4. Revoke tokens when requested
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
    ):
        """Initialize OAuth provider.

        Args:
            client_id: OAuth client ID from provider console
            client_secret: OAuth client secret from provider console
            redirect_uri: OAuth redirect URI (required for web OAuth flows)
            scopes: List of OAuth scopes to request (required, at least one scope needed)
            provider_name: Provider name from config (e.g., "google-drive", "microsoft", "x") - required

        Raises:
            OAuthError: If redirect_uri is empty, scopes is empty, or provider_name is empty
        """
        if not redirect_uri:
            raise OAuthError("redirect_uri is required for OAuth provider")
        if not scopes:
            raise OAuthError("At least one scope is required for OAuth provider")
        if not provider_name:
            raise OAuthError("provider_name is required for OAuth provider")

        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.provider_name = provider_name

    @abstractmethod
    def get_authorization_url(self, state: str | None = None) -> str:
        """Generate OAuth authorization URL for user consent.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL for user to visit

        Example:
            >>> provider = GoogleOAuthProvider(...)
            >>> url = provider.get_authorization_url(state="random_state")
            >>> print(f"Visit: {url}")
        """
        pass

    @abstractmethod
    async def exchange_code(self, code: str) -> OAuthCredential:
        """Exchange authorization code for access/refresh tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            OAuthCredential with tokens

        Raises:
            OAuthError: If code exchange fails

        Example:
            >>> provider = GoogleOAuthProvider(...)
            >>> cred = await provider.exchange_code("4/0AY0e...")
        """
        pass

    @abstractmethod
    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired access token.

        Args:
            credential: Existing credential with refresh_token

        Returns:
            New credential with refreshed access_token

        Raises:
            OAuthError: If refresh fails (e.g., refresh_token revoked)

        Example (MindsDB pattern):
            >>> if creds.expired and creds.refresh_token:
            ...     creds = await provider.refresh_token(creds)
        """
        pass

    @abstractmethod
    async def revoke_token(self, credential: OAuthCredential) -> bool:
        """Revoke an OAuth token.

        Args:
            credential: Credential to revoke

        Returns:
            True if revocation succeeded

        Example:
            >>> await provider.revoke_token(credential)
        """
        pass

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool:
        """Validate an access token without refreshing.

        Args:
            access_token: Access token to validate

        Returns:
            True if token is valid

        Example:
            >>> is_valid = await provider.validate_token(token)
        """
        pass


class OAuthError(Exception):
    """OAuth operation failed."""

    pass
