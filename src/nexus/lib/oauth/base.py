"""Template Method base for OAuth providers (RFC 6749 + optional RFC 7636 PKCE).

Shared behavior:
- Token exchange and refresh HTTP POSTs with unified error handling.
- Standard ``access_token`` / ``refresh_token`` / ``expires_in`` response parsing.
- Optional PKCE: set ``requires_pkce = True`` on the subclass to require a
  ``code_verifier`` in :meth:`BaseOAuthProvider.exchange_code`.

Subclasses must define:
- ``TOKEN_ENDPOINT`` — token exchange / refresh URL.
- ``AUTHORIZATION_ENDPOINT`` — user-consent URL (may be empty for
  client-credentials grants).
- :meth:`BaseOAuthProvider.get_authorization_url` — builds the redirect URL.
- :meth:`BaseOAuthProvider._build_exchange_params` — POST body for
  :meth:`BaseOAuthProvider.exchange_code`.
- :meth:`BaseOAuthProvider._build_refresh_params` — POST body for
  :meth:`BaseOAuthProvider.refresh_token`.
- :meth:`BaseOAuthProvider.revoke_token` /
  :meth:`BaseOAuthProvider.validate_token` — vendor endpoints.
"""

from __future__ import annotations

import dataclasses
import logging
import secrets
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.lib.oauth.pkce import generate_pkce_pair
from nexus.lib.oauth.types import OAuthCredential, OAuthError

logger = logging.getLogger(__name__)


class BaseOAuthProvider(ABC):
    """Template Method base for all OAuth providers."""

    TOKEN_ENDPOINT: str = ""
    AUTHORIZATION_ENDPOINT: str = ""

    # RFC 7636: subclasses that MUST use PKCE flip this to True. Providers that
    # CAN use PKCE but don't require it can still call
    # :meth:`get_authorization_url_with_pkce` /
    # :meth:`exchange_code_pkce` directly.
    requires_pkce: bool = False

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
        if self._http_client is not None:
            yield self._http_client
        else:
            async with httpx.AsyncClient() as client:
                yield client

    # ── PKCE helpers (optional) ─────────────────────────────────

    def get_authorization_url_with_pkce(
        self,
        state: str | None = None,
        *,
        extra_params: dict[str, str] | None = None,
    ) -> tuple[str, dict[str, str]]:
        """Build an authorization URL with a PKCE ``code_challenge``.

        Returns ``(url, pkce_data)`` where ``pkce_data`` has ``code_verifier``,
        ``code_challenge``, and ``state``. Callers MUST persist
        ``code_verifier`` and pass it back to :meth:`exchange_code_pkce`.
        """
        if not self.AUTHORIZATION_ENDPOINT:
            raise OAuthError(f"{type(self).__name__} has no AUTHORIZATION_ENDPOINT set")

        verifier, challenge = generate_pkce_pair()
        state = state or secrets.token_urlsafe(32)
        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self._scope_string(),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        if extra_params:
            params.update(extra_params)
        url = f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"
        return url, {
            "code_verifier": verifier,
            "code_challenge": challenge,
            "state": state,
        }

    async def exchange_code_pkce(
        self, code: str, code_verifier: str, **kwargs: Any
    ) -> OAuthCredential:
        """PKCE-aware wrapper around :meth:`exchange_code`."""
        return await self.exchange_code(code, code_verifier=code_verifier, **kwargs)

    # ── Template Method: exchange_code ──────────────────────────

    async def exchange_code(self, code: str, **kwargs: Any) -> OAuthCredential:
        if self.requires_pkce and "code_verifier" not in kwargs:
            raise OAuthError(
                f"{self.provider_name} requires PKCE; call exchange_code_pkce or "
                f"pass code_verifier kwarg."
            )
        params = self._build_exchange_params(code, **kwargs)
        headers = self._build_exchange_headers()
        token_data = await self._post_token_request(params, headers=headers, action="exchange code")
        return self._parse_token_response(token_data)

    @abstractmethod
    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]: ...

    def _build_exchange_headers(self) -> dict[str, str] | None:
        return None

    # ── Template Method: refresh_token ──────────────────────────

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        if not credential.refresh_token:
            raise OAuthError("No refresh_token available")

        params = self._build_refresh_params(credential)
        headers = self._build_refresh_headers()
        token_data = await self._post_token_request(params, headers=headers, action="refresh token")
        new_cred = self._parse_token_response(token_data)
        refresh = new_cred.refresh_token or credential.refresh_token
        return dataclasses.replace(
            new_cred,
            refresh_token=refresh,
            provider=self.provider_name,
            user_email=credential.user_email,
            scopes=credential.scopes or new_cred.scopes,
        )

    @abstractmethod
    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]: ...

    def _build_refresh_headers(self) -> dict[str, str] | None:
        return None

    # ── Abstract methods: provider-specific ─────────────────────

    @abstractmethod
    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str: ...

    @abstractmethod
    async def revoke_token(self, credential: OAuthCredential) -> bool: ...

    @abstractmethod
    async def validate_token(self, access_token: str) -> bool: ...

    # ── Shared infrastructure ──────────────────────────────────

    def _scope_string(self) -> str:
        """Serialize ``self.scopes`` for authorization/token URLs.

        Default is space-separated (RFC 6749). Subclasses that need a different
        separator (Slack uses comma) override this.
        """
        return " ".join(self.scopes)

    async def _post_token_request(
        self,
        data: dict[str, str],
        *,
        headers: dict[str, str] | None = None,
        action: str = "token request",
    ) -> dict[str, Any]:
        async with self._get_client() as client:
            try:
                response = await client.post(self.TOKEN_ENDPOINT, data=data, headers=headers)
                response.raise_for_status()
                result: dict[str, Any] = response.json()
                return result
            except httpx.HTTPStatusError as e:
                raise OAuthError(f"Failed to {action}: {e.response.text}") from e
            except Exception as e:
                raise OAuthError(f"Failed to {action}: {e}") from e

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
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
