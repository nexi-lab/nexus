"""Template Method base class for OAuth providers.

Shared logic for token exchange, refresh, response parsing, and error handling.
Providers only override ``_build_exchange_params()``, ``_build_refresh_params()``,
``get_authorization_url()``, ``revoke_token()``, and ``validate_token()``.
"""

from __future__ import annotations

import dataclasses
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nexus.auth.oauth.types import OAuthCredential, OAuthError

logger = logging.getLogger(__name__)


class BaseOAuthProvider(ABC):
    """Template Method base for all OAuth providers.

    Shared behavior:
    - ``exchange_code()`` — POST to TOKEN_ENDPOINT with provider-specific params
    - ``refresh_token()`` — POST to TOKEN_ENDPOINT for refresh, preserve old refresh_token
    - ``_parse_token_response()`` — standard parsing of token JSON
    - ``_post_token_request()`` — shared httpx POST with error handling

    Subclasses must define:
    - ``TOKEN_ENDPOINT`` class attribute
    - ``get_authorization_url()``
    - ``_build_exchange_params()``
    - ``_build_refresh_params()``
    - ``revoke_token()``
    - ``validate_token()``
    """

    TOKEN_ENDPOINT: str  # Subclass must set

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
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
        self._http_client = http_client

    @asynccontextmanager
    async def _get_client(self) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the shared client or a temporary one."""
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient() as client:
                yield client

    # ── Template Method: exchange_code ──────────────────────────

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthCredential:
        """Exchange authorization code for tokens (Template Method)."""
        params = self._build_exchange_params(code, **kwargs)
        headers = self._build_exchange_headers()
        token_data = await self._post_token_request(params, headers=headers, action="exchange code")
        return self._parse_token_response(token_data)

    @abstractmethod
    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        """Build POST body for token exchange. Provider-specific."""
        ...

    def _build_exchange_headers(self) -> dict[str, str] | None:
        """Optional headers for token exchange (e.g. Basic auth for X)."""
        return None

    # ── Template Method: refresh_token ──────────────────────────

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        """Refresh an expired access token (Template Method)."""
        if not credential.refresh_token:
            raise OAuthError("No refresh_token available")

        params = self._build_refresh_params(credential)
        headers = self._build_refresh_headers()
        token_data = await self._post_token_request(
            params, headers=headers, action="refresh token"
        )
        new_cred = self._parse_token_response(token_data)

        # Preserve old refresh_token if server didn't return a new one
        refresh = new_cred.refresh_token or credential.refresh_token
        return dataclasses.replace(
            new_cred,
            refresh_token=refresh,
            provider=self.provider_name,
            user_email=credential.user_email,
            scopes=credential.scopes or new_cred.scopes,
        )

    @abstractmethod
    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        """Build POST body for token refresh. Provider-specific."""
        ...

    def _build_refresh_headers(self) -> dict[str, str] | None:
        """Optional headers for token refresh."""
        return None

    # ── Abstract methods: provider-specific ─────────────────────

    @abstractmethod
    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str: ...

    @abstractmethod
    async def revoke_token(self, credential: OAuthCredential) -> bool: ...

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool: ...

    # ── Shared infrastructure ──────────────────────────────────

    async def _post_token_request(
        self,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        action: str = "token request",
    ) -> dict[str, Any]:
        """POST to TOKEN_ENDPOINT with unified error handling."""
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.TOKEN_ENDPOINT,
                    data=data,
                    headers=headers,
                )
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as e:
                error_detail = e.response.text
                raise OAuthError(f"Failed to {action}: {error_detail}") from e
            except Exception as e:
                raise OAuthError(f"Failed to {action}: {e}") from e

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        """Parse standard OAuth token response into frozen OAuthCredential."""
        expires_at = None
        if "expires_in" in token_data:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(token_data["expires_in"]))

        scopes = None
        if "scope" in token_data:
            scopes = tuple(token_data["scope"].split(" "))

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
