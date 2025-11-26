"""X (Twitter) OAuth 2.0 PKCE provider implementation.

Implements OAuth 2.0 Authorization Code Flow with PKCE for X API v2.
PKCE (Proof Key for Code Exchange) provides enhanced security without requiring
client secrets, making it suitable for public clients.

Example:
    >>> provider = XOAuthProvider(
    ...     client_id="your-client-id",
    ...     redirect_uri="http://localhost:5173/auth/callback",
    ...     scopes=["tweet.read", "tweet.write", "users.read"]
    ... )
    >>> auth_url, pkce_data = provider.get_authorization_url_with_pkce()
    >>> # User visits auth_url, grants permission, gets redirected with code
    >>> credential = await provider.exchange_code_pkce(code, pkce_data)
"""

import base64
import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from .oauth_provider import OAuthCredential, OAuthError, OAuthProvider


class XOAuthProvider(OAuthProvider):
    """X (Twitter) OAuth 2.0 provider with PKCE support.

    Supports OAuth flows for X API v2:
    - Tweets: tweet.read, tweet.write, tweet.moderate.write
    - Users: users.read, follows.read, follows.write
    - Bookmarks: bookmark.read, bookmark.write
    - Lists: list.read, list.write
    - Spaces: space.read
    - Offline access: offline.access (for refresh tokens)

    OAuth endpoints:
    - Authorization: https://twitter.com/i/oauth2/authorize
    - Token: https://api.twitter.com/2/oauth2/token
    - Revoke: https://api.twitter.com/2/oauth2/revoke

    Note: X API v2 uses PKCE (Proof Key for Code Exchange), which means
    client_secret is optional for public clients.
    """

    # X OAuth endpoints
    AUTHORIZATION_ENDPOINT = "https://twitter.com/i/oauth2/authorize"
    TOKEN_ENDPOINT = "https://api.twitter.com/2/oauth2/token"
    REVOKE_ENDPOINT = "https://api.twitter.com/2/oauth2/revoke"

    # Default scopes for X connector
    DEFAULT_SCOPES = [
        "tweet.read",  # Read tweets
        "tweet.write",  # Post tweets
        "tweet.moderate.write",  # Delete tweets
        "users.read",  # Read user profiles
        "follows.read",  # Read followers/following
        "offline.access",  # Refresh tokens
        "bookmark.read",  # Read bookmarks
        "bookmark.write",  # Add/remove bookmarks
        "list.read",  # Read lists
        "like.read",  # Read likes
        "like.write",  # Like/unlike tweets
    ]

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        client_secret: str | None = None,  # Optional for PKCE
    ):
        """Initialize X OAuth provider with PKCE support.

        Args:
            client_id: X OAuth client ID (from X Developer Portal)
            redirect_uri: OAuth redirect URI (must match app config)
            scopes: List of X OAuth scopes to request (required)
            provider_name: Provider name from config (e.g., "x", "twitter")
            client_secret: X OAuth client secret (optional for PKCE)

        Example:
            >>> provider = XOAuthProvider(
            ...     client_id="your-client-id",
            ...     redirect_uri="http://localhost:5173/auth/callback",
            ...     scopes=["tweet.read", "tweet.write", "users.read", "offline.access"],
            ...     provider_name="x"
            ... )
        """
        super().__init__(
            provider_name=provider_name,
            client_id=client_id,
            client_secret=client_secret or "",  # Empty string if not provided
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

    def get_authorization_url(self, state: str | None = None) -> str:
        """Generate X OAuth authorization URL (without PKCE).

        Note: For X API v2, use get_authorization_url_with_pkce() instead.
        This method is provided for interface compatibility.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL for user to visit
        """
        # For PKCE, we need to return both URL and PKCE data
        # This method returns only the URL, so generate PKCE internally
        url, _ = self.get_authorization_url_with_pkce(state)
        return url

    def get_authorization_url_with_pkce(
        self, state: str | None = None
    ) -> tuple[str, dict[str, str]]:
        """Generate X OAuth authorization URL with PKCE challenge.

        This is the recommended method for X OAuth as it provides
        enhanced security through PKCE.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Tuple of (auth_url, pkce_data)
            - auth_url: URL for user to visit
            - pkce_data: PKCE verifier to store (needed for token exchange)

        Example:
            >>> provider = XOAuthProvider(...)
            >>> auth_url, pkce_data = provider.get_authorization_url_with_pkce()
            >>> print(f"Visit: {auth_url}")
            >>> # Store pkce_data["code_verifier"] for later use
        """
        # Generate PKCE code verifier (43-128 characters, URL-safe)
        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")

        # Generate PKCE code challenge (SHA256 hash of verifier)
        challenge_bytes = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode("utf-8").rstrip("=")

        # Build authorization URL
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        # Add state if provided
        if state:
            params["state"] = state
        else:
            # Generate random state for CSRF protection
            params["state"] = secrets.token_urlsafe(32)

        auth_url = f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

        # Return URL and PKCE verifier (to be stored temporarily)
        state_value: str = params["state"] or ""
        pkce_data: dict[str, str] = {
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
            "state": state_value,
        }

        return auth_url, pkce_data

    async def exchange_code(self, code: str) -> OAuthCredential:  # noqa: ARG002
        """Exchange authorization code for tokens (without PKCE).

        Note: For X API v2, use exchange_code_pkce() instead.
        This method is provided for interface compatibility.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            OAuthCredential with access_token, refresh_token, etc.

        Raises:
            OAuthError: PKCE is required for X OAuth
        """
        raise OAuthError("X OAuth requires PKCE. Use exchange_code_pkce() instead.")

    async def exchange_code_pkce(self, code: str, code_verifier: str) -> OAuthCredential:
        """Exchange authorization code for tokens using PKCE verifier.

        Args:
            code: Authorization code from OAuth callback
            code_verifier: PKCE code verifier (from get_authorization_url_with_pkce)

        Returns:
            OAuthCredential with access_token, refresh_token, etc.

        Raises:
            OAuthError: If code exchange fails

        Example:
            >>> provider = XOAuthProvider(...)
            >>> # After user authorizes and returns with code
            >>> cred = await provider.exchange_code_pkce(code, pkce_data["code_verifier"])
            >>> print(cred.access_token)
        """
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }

        # Add client_secret if provided (for confidential clients)
        if self.client_secret:
            data["client_secret"] = self.client_secret

        # Build headers - X API requires Basic auth for confidential clients
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # Use Basic authentication if client_secret is provided
        if self.client_secret:
            import base64

            credentials = f"{self.client_id}:{self.client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded_credentials}"
            # Remove client_id/secret from body when using Basic auth
            data.pop("client_secret", None)

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.TOKEN_ENDPOINT,
                    data=data,
                    headers=headers,
                )
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

        Example:
            >>> if creds.is_expired() and creds.refresh_token:
            ...     creds = await provider.refresh_token(creds)
        """
        if not credential.refresh_token:
            raise OAuthError("No refresh_token available")

        data = {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token,
            "client_id": self.client_id,
        }

        # Build headers - X API requires Basic auth for confidential clients
        headers = {"Content-Type": "application/x-www-form-urlencoded"}

        # Use Basic authentication if client_secret is provided
        if self.client_secret:
            import base64

            credentials = f"{self.client_id}:{self.client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded_credentials}"

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.TOKEN_ENDPOINT,
                    data=data,
                    headers=headers,
                )
                response.raise_for_status()
                token_data = response.json()
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to refresh token: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to refresh token: {e}") from e

        # Parse token response
        new_cred = self._parse_token_response(token_data)

        # X doesn't always return a new refresh_token, so preserve the old one
        if not new_cred.refresh_token:
            new_cred.refresh_token = credential.refresh_token

        # Preserve other metadata
        new_cred.provider = self.provider_name
        new_cred.user_email = credential.user_email
        new_cred.scopes = credential.scopes or new_cred.scopes
        new_cred.metadata = credential.metadata

        return new_cred

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        """Revoke an X OAuth token.

        Args:
            credential: Credential to revoke

        Returns:
            True if revocation succeeded

        Example:
            >>> success = await provider.revoke_token(credential)
        """
        # X allows revoking access_token or refresh_token
        token = credential.access_token
        token_type = "access_token"

        if not token:
            return False

        data = {
            "token": token,
            "token_type_hint": token_type,
            "client_id": self.client_id,
        }

        # Add client_secret if provided
        if self.client_secret:
            data["client_secret"] = self.client_secret

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.REVOKE_ENDPOINT,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                # Token might already be revoked or invalid
                return False
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        """Validate an X access token.

        X doesn't provide a tokeninfo endpoint, so we validate by
        making a simple API call (e.g., get authenticated user).

        Args:
            access_token: Access token to validate

        Returns:
            True if token is valid

        Example:
            >>> is_valid = await provider.validate_token(token)
        """
        # Use X API v2 to get authenticated user (requires users.read scope)
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    "https://api.twitter.com/2/users/me",
                    headers=headers,
                )
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        """Parse X token response into OAuthCredential.

        Args:
            token_data: Token response from X API

        Returns:
            OAuthCredential

        Example token_data:
            {
                "token_type": "bearer",
                "expires_in": 7200,
                "access_token": "VGhpcyBpcyBhbiBlmFtcGxlIGFjY2VzcyB0b2tlbg",
                "refresh_token": "bWlzUyBpcyBhbiBlmFtcGxlIHJlZnJlc2ggdG9rZW4K",
                "scope": "tweet.read users.read tweet.write offline.access"
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
            token_type=token_data.get("token_type", "bearer").capitalize(),
            expires_at=expires_at,
            scopes=scopes,
            provider=self.provider_name,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
        )
