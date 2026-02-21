"""Google OAuth 2.0 provider (Template Method subclass).

Supports all Google services (Drive, Gmail, Calendar, etc.) via scope selection.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.bricks.auth.oauth.base_provider import BaseOAuthProvider
from nexus.bricks.auth.oauth.types import OAuthCredential


class GoogleOAuthProvider(BaseOAuthProvider):
    """Google OAuth 2.0 provider for all Google services."""

    AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
    TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
    REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
    TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

    def get_authorization_url(
        self, state: str | None = None, redirect_uri: str | None = None, **_kwargs: Any
    ) -> str:
        uri = redirect_uri if redirect_uri is not None else self.redirect_uri
        params = {
            "client_id": self.client_id,
            "redirect_uri": uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "prompt": "consent",
        }
        if state:
            params["state"] = state
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        redirect_uri = kwargs.get("redirect_uri") or self.redirect_uri
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": credential.refresh_token or "",
            "grant_type": "refresh_token",
        }

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        token = credential.refresh_token or credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(self.REVOKE_ENDPOINT, params={"token": token})
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.TOKENINFO_ENDPOINT, params={"access_token": access_token}
                )
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False
