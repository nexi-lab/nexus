"""Google OAuth 2.0 provider implementation.

Implements OAuth flow for all Google services (Drive, Gmail, Calendar, Cloud Storage, etc.).
This provider can be used for any Google service by specifying the appropriate scopes.

Different Google services are configured via the OAuth config system with different
default scopes. See config.demo.yaml for examples.

Example:
    >>> provider = GoogleOAuthProvider(
    ...     client_id="123.apps.googleusercontent.com",
    ...     client_secret="secret",
    ...     redirect_uri="http://localhost:8080/oauth/callback",
    ...     scopes=["https://www.googleapis.com/auth/drive"],
    ...     provider_name="google-drive"
    ... )
    >>> auth_url = provider.get_authorization_url()
    >>> # User visits auth_url, grants permission, gets redirected with code
    >>> credential = await provider.exchange_code(code)
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from .oauth_provider import OAuthCredential, OAuthError, OAuthProvider


class GoogleOAuthProvider(OAuthProvider):
    """Google OAuth 2.0 provider for all Google services.

    This provider can be used for any Google service (Drive, Gmail, Calendar,
    Cloud Storage, etc.) by specifying the appropriate scopes.

    Supports OAuth flows for all Google services:
    - Google Drive: https://www.googleapis.com/auth/drive
    - Gmail: https://www.googleapis.com/auth/gmail.readonly
    - Calendar: https://www.googleapis.com/auth/calendar
    - Cloud Storage: https://www.googleapis.com/auth/devstorage.read_write
    - etc.

    Different Google services are configured via the OAuth config system with
    different default scopes. See config.demo.yaml for service-specific
    configurations.

    OAuth endpoints (shared across all Google services):
    - Authorization: https://accounts.google.com/o/oauth2/v2/auth
    - Token: https://oauth2.googleapis.com/token
    - Revoke: https://oauth2.googleapis.com/revoke
    - Token info: https://oauth2.googleapis.com/tokeninfo

    Note: Scopes should be provided explicitly or from OAuth config.
    For service-specific scopes, configure providers in the OAuth config.

    Example:
        >>> provider = GoogleOAuthProvider(
        ...     client_id="123.apps.googleusercontent.com",
        ...     client_secret="GOCSPX-...",
        ...     redirect_uri="http://localhost:8080/oauth/callback",
        ...     scopes=["https://www.googleapis.com/auth/drive"],
        ...     provider_name="google-drive"
        ... )
    """

    # Google OAuth endpoints (shared across all services)
    AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
    TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
    ):
        """Initialize Google OAuth provider.

        Args:
            client_id: Google OAuth client ID (from Google Cloud Console)
            client_secret: Google OAuth client secret
            redirect_uri: OAuth redirect URI (must match console config)
            scopes: List of Google OAuth scopes to request (required)
            provider_name: Provider name from config (e.g., "google-drive", "gmail")
        """
        super().__init__(client_id, client_secret, redirect_uri, scopes, provider_name)

    def get_authorization_url(self, state: str | None = None) -> str:
        """Generate Google OAuth authorization URL.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL for user to visit

        Raises:
            OAuthError: If redirect_uri is None or scopes is empty

        Example:
            >>> provider = GoogleOAuthProvider(...)
            >>> url = provider.get_authorization_url(state="random_state")
            >>> print(f"Visit: {url}")
            Visit: https://accounts.google.com/o/oauth2/v2/auth?client_id=...
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",  # Request refresh token
            "prompt": "consent",  # Force consent to get refresh token
        }

        if state:
            params["state"] = state

        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthCredential:
        """Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            OAuthCredential with access_token, refresh_token, etc.

        Raises:
            OAuthError: If code exchange fails or redirect_uri is None

        Example:
            >>> provider = GoogleOAuthProvider(...)
            >>> cred = await provider.exchange_code("4/0AY0e...")
            >>> print(cred.access_token)
        """
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.TOKEN_ENDPOINT, data=data)
                response.raise_for_status()
                token_data = response.json()
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to exchange code: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to exchange code: {e}") from e

        # Parse token response
        return self._parse_token_response(token_data)

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired access token.

        Args:
            credential: Existing credential with refresh_token

        Returns:
            New credential with refreshed access_token

        Raises:
            OAuthError: If refresh fails (e.g., refresh_token revoked)

        Example (MindsDB pattern):
            >>> if creds.is_expired() and creds.refresh_token:
            ...     creds = await provider.refresh_token(creds)
        """
        if not credential.refresh_token:
            raise OAuthError("No refresh_token available")

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": credential.refresh_token,
            "grant_type": "refresh_token",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.TOKEN_ENDPOINT, data=data)
                response.raise_for_status()
                token_data = response.json()
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to refresh token: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to refresh token: {e}") from e

        # Parse token response and preserve refresh_token
        new_cred = self._parse_token_response(token_data)

        # Google doesn't always return a new refresh_token, so preserve the old one
        if not new_cred.refresh_token:
            new_cred.refresh_token = credential.refresh_token

        # Preserve other metadata
        new_cred.provider = self.provider_name
        new_cred.user_email = credential.user_email
        new_cred.scopes = credential.scopes or new_cred.scopes

        return new_cred

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        """Revoke a Google OAuth token.

        Args:
            credential: Credential to revoke

        Returns:
            True if revocation succeeded

        Example:
            >>> success = await provider.revoke_token(credential)
        """
        # Google allows revoking either access_token or refresh_token
        token = credential.refresh_token or credential.access_token

        if not token:
            return False

        params = {"token": token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.REVOKE_ENDPOINT, params=params)
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                # Token might already be revoked or invalid
                return False
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        """Validate a Google access token.

        Args:
            access_token: Access token to validate

        Returns:
            True if token is valid

        Example:
            >>> is_valid = await provider.validate_token(token)
        """
        params = {"access_token": access_token}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(self.TOKENINFO_ENDPOINT, params=params)
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        """Parse Google token response into OAuthCredential.

        Args:
            token_data: Token response from Google

        Returns:
            OAuthCredential

        Example token_data:
            {
                "access_token": "ya29.a0ARrdaM...",
                "expires_in": 3599,
                "refresh_token": "1//0e...",
                "scope": "https://www.googleapis.com/auth/drive",
                "token_type": "Bearer"
            }
        """
        # Calculate expires_at from expires_in
        expires_at = None
        if "expires_in" in token_data:
            expires_in = int(token_data["expires_in"])
            expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        # Parse scopes
        scopes = None
        if "scope" in token_data:
            scopes = token_data["scope"].split(" ")

        return OAuthCredential(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
            provider=self.provider_name,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
        )
