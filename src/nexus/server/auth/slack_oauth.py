"""Slack OAuth 2.0 provider implementation.

Implements OAuth flow for Slack workspace integrations.
Uses Slack's OAuth v2 flow for modern scopes and bot/user token support.

References:
- https://api.slack.com/authentication/oauth-v2
- https://api.slack.com/scopes

Example:
    >>> provider = SlackOAuthProvider(
    ...     client_id="123.456",
    ...     client_secret="secret",
    ...     redirect_uri="http://localhost:2026/oauth/callback",
    ...     scopes=["channels:read", "channels:history", "chat:write"],
    ...     provider_name="slack"
    ... )
    >>> auth_url = provider.get_authorization_url()
    >>> # User visits auth_url, grants permission, gets redirected with code
    >>> credential = await provider.exchange_code(code)
"""

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from .oauth_provider import OAuthCredential, OAuthError, OAuthProvider


class SlackOAuthProvider(OAuthProvider):
    """Slack OAuth 2.0 provider for Slack workspace integrations.

    This provider implements Slack's OAuth v2 flow, which supports:
    - User tokens for user-level operations
    - Bot tokens for bot operations
    - Granular scopes (channels:read, chat:write, etc.)
    - Workspace-level permissions

    OAuth endpoints:
    - Authorization: https://slack.com/oauth/v2/authorize
    - Token: https://slack.com/api/oauth.v2.access
    - Revoke: https://slack.com/api/auth.revoke

    Common scopes:
    - channels:read - View basic channel information
    - channels:history - View messages in public channels
    - chat:write - Post messages to channels
    - users:read - View users in workspace
    - im:read - View direct messages
    - im:history - View direct message history

    Example:
        >>> provider = SlackOAuthProvider(
        ...     client_id="123.456",
        ...     client_secret="secret",
        ...     redirect_uri="http://localhost:2026/oauth/callback",
        ...     scopes=["channels:read", "channels:history", "chat:write"],
        ...     provider_name="slack"
        ... )
    """

    # Slack OAuth v2 endpoints
    AUTHORIZATION_ENDPOINT = "https://slack.com/oauth/v2/authorize"
    TOKEN_ENDPOINT = "https://slack.com/api/oauth.v2.access"
    REVOKE_ENDPOINT = "https://slack.com/api/auth.revoke"
    TEST_ENDPOINT = "https://slack.com/api/auth.test"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
    ):
        """Initialize Slack OAuth provider.

        Args:
            client_id: Slack app client ID (from app settings)
            client_secret: Slack app client secret
            redirect_uri: OAuth redirect URI (must match app config)
            scopes: List of Slack OAuth scopes to request (required)
            provider_name: Provider name from config (e.g., "slack")
        """
        super().__init__(client_id, client_secret, redirect_uri, scopes, provider_name)

    def get_authorization_url(
        self, state: str | None = None, redirect_uri: str | None = None
    ) -> str:
        """Generate Slack OAuth authorization URL.

        Args:
            state: Optional state parameter for CSRF protection
            redirect_uri: Optional redirect URI to override the default one.
                         If not provided, uses self.redirect_uri

        Returns:
            Authorization URL for user to visit

        Raises:
            OAuthError: If redirect_uri is None or scopes is empty

        Example:
            >>> provider = SlackOAuthProvider(...)
            >>> url = provider.get_authorization_url(state="random_state")
            >>> print(f"Visit: {url}")
            Visit: https://slack.com/oauth/v2/authorize?client_id=...
        """
        # Use provided redirect_uri or fall back to instance redirect_uri
        uri_to_use = redirect_uri if redirect_uri is not None else self.redirect_uri

        params = {
            "client_id": self.client_id,
            "redirect_uri": uri_to_use,
            "scope": ",".join(self.scopes),  # Slack uses comma-separated scopes
            "user_scope": "",  # Optional: user-level scopes (we use user scopes by default)
        }

        if state:
            params["state"] = state

        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str | None = None) -> OAuthCredential:
        """Exchange authorization code for tokens.

        Args:
            code: Authorization code from OAuth callback
            redirect_uri: Optional redirect URI to use for token exchange.
                         Must match the redirect_uri used in authorization URL.
                         If not provided, uses self.redirect_uri

        Returns:
            OAuthCredential with access_token, user info, etc.

        Raises:
            OAuthError: If code exchange fails or redirect_uri is None

        Example:
            >>> provider = SlackOAuthProvider(...)
            >>> cred = await provider.exchange_code("1234567890.1234567890")
            >>> print(cred.access_token)
        """
        # Use provided redirect_uri or fall back to instance redirect_uri
        uri_to_use = redirect_uri if redirect_uri is not None else self.redirect_uri

        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": uri_to_use,
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

        # Check for Slack API error response
        if not token_data.get("ok"):
            error = token_data.get("error", "unknown_error")
            raise OAuthError(f"Slack API error: {error}")

        # Parse token response
        credential = self._parse_token_response(token_data)

        # Fetch user email from Slack API if not already set
        if not credential.user_email and credential.access_token:
            credential = await self._fetch_user_email(credential)

        return credential

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired access token.

        Note: Slack does not support token refresh for OAuth v2.
        Tokens do not expire unless explicitly revoked.
        This method will raise an error if called.

        Args:
            credential: Existing credential

        Raises:
            OAuthError: Always raised (Slack doesn't support token refresh)

        Reference:
            https://api.slack.com/authentication/rotation
        """
        _ = credential  # Keep parameter for interface compatibility
        raise OAuthError(
            "Slack OAuth v2 tokens do not expire and cannot be refreshed. "
            "If access is lost, the user must re-authorize the app."
        )

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        """Revoke a Slack OAuth token.

        Args:
            credential: Credential to revoke

        Returns:
            True if revocation succeeded

        Example:
            >>> success = await provider.revoke_token(credential)
        """
        if not credential.access_token:
            return False

        headers = {"Authorization": f"Bearer {credential.access_token}"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.REVOKE_ENDPOINT, headers=headers)
                response.raise_for_status()
                result = response.json()
                return result.get("ok", False)
            except httpx.HTTPStatusError:
                # Token might already be revoked or invalid
                return False
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        """Validate a Slack access token.

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
                response = await client.get(self.TEST_ENDPOINT, headers=headers)
                response.raise_for_status()
                result = response.json()
                return result.get("ok", False)
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        """Parse Slack token response into OAuthCredential.

        Args:
            token_data: Token response from Slack

        Returns:
            OAuthCredential

        Example token_data:
            {
                "ok": true,
                "access_token": "xoxp-...",
                "token_type": "Bearer",
                "scope": "channels:read,channels:history,chat:write",
                "bot_user_id": "U0KRQLJ9H",
                "app_id": "A0KRD7HC3",
                "team": {
                    "name": "Slack Softball Team",
                    "id": "T9TK3CUKW"
                },
                "enterprise": {...},
                "authed_user": {
                    "id": "U1234567890",
                    "scope": "channels:read,channels:history",
                    "access_token": "xoxp-...",
                    "token_type": "Bearer"
                }
            }
        """
        # Slack tokens don't expire (unless revoked), so no expires_at
        # For now we set a very far future date
        expires_at = datetime(2099, 12, 31, tzinfo=UTC)

        # Parse scopes (Slack returns comma-separated string)
        scopes = None
        if "scope" in token_data:
            scopes = token_data["scope"].split(",")

        # Extract user information
        user_email = None
        user_id = None
        team_id = None

        if "authed_user" in token_data:
            user_id = token_data["authed_user"].get("id")

        if "team" in token_data:
            team_id = token_data["team"].get("id")

        # Use authed_user token if available (user token), otherwise use app-level token
        access_token = token_data.get("access_token")
        if "authed_user" in token_data and "access_token" in token_data["authed_user"]:
            access_token = token_data["authed_user"]["access_token"]
            if "scope" in token_data["authed_user"]:
                scopes = token_data["authed_user"]["scope"].split(",")

        return OAuthCredential(
            access_token=access_token,
            refresh_token=None,  # Slack doesn't use refresh tokens
            token_type=token_data.get("token_type", "Bearer"),
            expires_at=expires_at,
            scopes=scopes,
            provider=self.provider_name,
            user_email=user_email,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
            # Store additional Slack-specific metadata
            metadata={
                "team_id": team_id,
                "user_id": user_id,
                "bot_user_id": token_data.get("bot_user_id"),
                "app_id": token_data.get("app_id"),
            },
        )

    async def _fetch_user_email(self, credential: OAuthCredential) -> OAuthCredential:
        """Fetch user email from Slack API.

        Slack's OAuth token response doesn't include email directly.
        We need to call users.info or auth.test to get the user's email.

        Args:
            credential: OAuth credential with access token

        Returns:
            Updated credential with user_email populated

        Raises:
            OAuthError: If unable to fetch user email
        """
        if not credential.access_token:
            return credential

        # First try auth.test to get the current user info
        headers = {"Authorization": f"Bearer {credential.access_token}"}

        async with httpx.AsyncClient() as client:
            try:
                # Call auth.test to get current user info
                response = await client.get(self.TEST_ENDPOINT, headers=headers)
                response.raise_for_status()
                auth_data = response.json()

                if not auth_data.get("ok"):
                    raise OAuthError(f"Slack API error: {auth_data.get('error', 'unknown')}")

                user_id = auth_data.get("user_id")
                if not user_id:
                    raise OAuthError("Could not get user_id from auth.test")

                # Now call users.info to get user email
                users_info_url = "https://slack.com/api/users.info"
                response = await client.get(
                    users_info_url, headers=headers, params={"user": user_id}
                )
                response.raise_for_status()
                user_data = response.json()

                if not user_data.get("ok"):
                    raise OAuthError(f"Slack API error: {user_data.get('error', 'unknown')}")

                user_email = user_data.get("user", {}).get("profile", {}).get("email")
                if not user_email:
                    # Fallback: use user_id@slack if email not available
                    # This can happen if the app doesn't have users:read.email scope
                    user_email = f"{user_id}@slack.local"

                # Update credential with email
                credential.user_email = user_email
                return credential

            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to fetch user email: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to fetch user email: {e}") from e
