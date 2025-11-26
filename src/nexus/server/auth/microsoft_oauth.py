"""Microsoft OAuth 2.0 provider implementation.

Implements OAuth flow for Microsoft services (OneDrive, Outlook, SharePoint, etc.).
Based on MindsDB's MicrosoftOAuth2Manager pattern.

Example:
    >>> provider = MicrosoftOAuthProvider(
    ...     client_id="12345678-1234-1234-1234-123456789012",
    ...     client_secret="secret",
    ...     tenant_id="common",  # or specific tenant ID
    ...     redirect_uri="http://localhost:8080/oauth/callback",
    ...     scopes=["Files.ReadWrite.All", "offline_access"]
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


class MicrosoftOAuthProvider(OAuthProvider):
    """Microsoft OAuth 2.0 provider (Microsoft Identity Platform).

    Supports OAuth flows for Microsoft services:
    - OneDrive: Files.ReadWrite.All
    - Outlook: Mail.Read, Mail.Send
    - SharePoint: Sites.Read.All
    - Microsoft Graph: User.Read

    OAuth endpoints (v2.0):
    - Authorization: https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
    - Token: https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token
    - Token info: https://graph.microsoft.com/v1.0/me

    Note: Microsoft uses tenant-specific endpoints. Use "common" for multi-tenant apps.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
    ):
        """Initialize Microsoft OAuth provider.

        Args:
            client_id: Microsoft app (client) ID from Azure Portal
            client_secret: Microsoft client secret from Azure Portal
            redirect_uri: OAuth redirect URI (must match Azure config)
            scopes: List of Microsoft Graph scopes to request (required)
            provider_name: Provider name from config (e.g., "microsoft", "microsoft-onedrive")

        Example:
            >>> provider = MicrosoftOAuthProvider(
            ...     client_id="12345678-1234-1234-1234-123456789012",
            ...     client_secret="secret~...",
            ...     redirect_uri="http://localhost:8080/oauth/callback",
            ...     scopes=["Files.ReadWrite.All", "offline_access"],
            ...     provider_name="microsoft-onedrive"
            ... )
        """
        super().__init__(client_id, client_secret, redirect_uri, scopes, provider_name)

        # Microsoft OAuth endpoints (using "common" for multi-tenant support)
        self.tenant_id = "common"
        self.authorization_endpoint = (
            "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
        )
        self.token_endpoint = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        self.graph_endpoint = "https://graph.microsoft.com/v1.0"

    def get_authorization_url(self, state: str | None = None) -> str:
        """Generate Microsoft OAuth authorization URL.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL for user to visit

        Raises:
            OAuthError: If redirect_uri is None or scopes is empty

        Example:
            >>> provider = MicrosoftOAuthProvider(...)
            >>> url = provider.get_authorization_url(state="random_state")
            >>> print(f"Visit: {url}")
            Visit: https://login.microsoftonline.com/common/oauth2/v2.0/authorize?client_id=...
        """
        # Microsoft requires offline_access scope for refresh tokens
        scopes = self.scopes.copy()
        if "offline_access" not in scopes:
            scopes.append("offline_access")

        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "response_mode": "query",
        }

        if state:
            params["state"] = state

        return f"{self.authorization_endpoint}?{urlencode(params)}"

    async def exchange_code(self, code: str) -> OAuthCredential:
        """Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            OAuthCredential with access_token, refresh_token, etc.

        Raises:
            OAuthError: If code exchange fails or redirect_uri is None

        Example:
            >>> provider = MicrosoftOAuthProvider(...)
            >>> cred = await provider.exchange_code("M.R3_BAY...")
            >>> print(cred.access_token)
        """
        scopes = self.scopes.copy()
        if "offline_access" not in scopes:
            scopes.append("offline_access")

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.token_endpoint, data=data)
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

        scopes = credential.scopes or self.scopes or []
        if "offline_access" not in scopes:
            scopes.append("offline_access")

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": credential.refresh_token,
            "grant_type": "refresh_token",
            "scope": " ".join(scopes),
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.token_endpoint, data=data)
                response.raise_for_status()
                token_data = response.json()
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to refresh token: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to refresh token: {e}") from e

        # Parse token response and preserve refresh_token
        new_cred = self._parse_token_response(token_data)

        # Microsoft may return a new refresh_token, but if not, preserve the old one
        if not new_cred.refresh_token:
            new_cred.refresh_token = credential.refresh_token

        # Preserve other metadata
        new_cred.provider = self.provider_name
        new_cred.user_email = credential.user_email
        new_cred.scopes = credential.scopes or new_cred.scopes

        return new_cred

    async def revoke_token(self, _credential: OAuthCredential) -> bool:
        """Revoke a Microsoft OAuth token.

        Note: Microsoft Graph API doesn't have a standard token revocation endpoint.
        To revoke access, users must remove app permissions from their account settings.

        Args:
            _credential: Credential to revoke (unused - Microsoft has no revocation API)

        Returns:
            True (always, since there's no API to call)

        Example:
            >>> success = await provider.revoke_token(credential)
        """
        # Microsoft doesn't provide a public revocation endpoint
        # Users must manually revoke from: https://account.live.com/consent/Manage
        # or https://myapps.microsoft.com
        return True

    async def validate_token(self, access_token: str) -> bool:
        """Validate a Microsoft access token.

        Args:
            access_token: Access token to validate

        Returns:
            True if token is valid

        Example:
            >>> is_valid = await provider.validate_token(token)
        """
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(f"{self.graph_endpoint}/me", headers=headers)
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        """Parse Microsoft token response into OAuthCredential.

        Args:
            token_data: Token response from Microsoft

        Returns:
            OAuthCredential

        Example token_data:
            {
                "access_token": "eyJ0eXAi...",
                "token_type": "Bearer",
                "expires_in": 3600,
                "refresh_token": "M.R3_BAY...",
                "scope": "Files.ReadWrite.All offline_access"
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
            token_uri=self.token_endpoint,
        )
