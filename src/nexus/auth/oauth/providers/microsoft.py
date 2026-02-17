"""Microsoft OAuth 2.0 provider (Template Method subclass).

Supports Microsoft Graph services (OneDrive, Outlook, SharePoint, etc.).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.auth.oauth.base_provider import BaseOAuthProvider
from nexus.auth.oauth.types import OAuthCredential


class MicrosoftOAuthProvider(BaseOAuthProvider):
    """Microsoft OAuth 2.0 provider (Microsoft Identity Platform)."""

    TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    AUTHORIZATION_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
    GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"

    def _scopes_with_offline(self, scopes: list[str] | None = None) -> list[str]:
        s = list(scopes or self.scopes)
        if "offline_access" not in s:
            s.append("offline_access")
        return s

    def get_authorization_url(self, state: str | None = None, **_kwargs: Any) -> str:
        scopes = self._scopes_with_offline()
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "response_mode": "query",
        }
        if state:
            params["state"] = state
        return f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def _build_exchange_params(self, code: str, **_kwargs: Any) -> dict[str, str]:
        scopes = self._scopes_with_offline()
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(scopes),
        }

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        scopes = self._scopes_with_offline(
            list(credential.scopes) if credential.scopes else None
        )
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": credential.refresh_token or "",
            "grant_type": "refresh_token",
            "scope": " ".join(scopes),
        }

    async def revoke_token(self, _credential: OAuthCredential) -> bool:
        # Microsoft has no standard revocation API
        return True

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    f"{self.GRAPH_ENDPOINT}/me",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False
