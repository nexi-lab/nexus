"""X (Twitter) OAuth 2.0 provider with mandatory PKCE."""

from __future__ import annotations

import base64
import dataclasses
from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider

_DEFAULT_SCOPES = [
    "tweet.read",
    "tweet.write",
    "tweet.moderate.write",
    "users.read",
    "follows.read",
    "offline.access",
    "bookmark.read",
    "bookmark.write",
    "list.read",
    "like.read",
    "like.write",
]


class XOAuthProvider(UniversalOAuthProvider):
    """X (Twitter) OAuth 2.0 with PKCE.

    ``client_secret`` is optional for public clients; when set, Basic Auth is
    used on the token endpoint.
    """

    USERS_ME_ENDPOINT = "https://api.twitter.com/2/users/me"

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str] | None = None,
        provider_name: str = "x",
        client_secret: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret or "",
            redirect_uri=redirect_uri,
            scopes=scopes or _DEFAULT_SCOPES,
            provider_name=provider_name,
            authorization_endpoint="https://twitter.com/i/oauth2/authorize",
            token_endpoint="https://api.twitter.com/2/oauth2/token",
            revocation_endpoint="https://api.twitter.com/2/oauth2/revoke",
            scope_format="space",
            scope_on_refresh=False,
            requires_pkce=True,
            http_client=http_client,
        )

    # The base class ``get_authorization_url_with_pkce`` already generates the
    # challenge/verifier. X's historical ``get_authorization_url()`` delegates
    # to the PKCE variant so CLI callers that want a URL without handling PKCE
    # still get one (the caller discards pkce_data if it doesn't plan to use
    # exchange_code_pkce — that's an incorrect usage, but we preserve the
    # surface to avoid breaking existing callers).
    def get_authorization_url(self, state: str | None = None, **_kwargs: Any) -> str:
        url, _ = self.get_authorization_url_with_pkce(state=state)
        return url

    def _basic_auth_header(self) -> dict[str, str] | None:
        if not self.client_secret:
            return None
        cred = f"{self.client_id}:{self.client_secret}".encode()
        encoded = base64.b64encode(cred).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    def _build_exchange_headers(self) -> dict[str, str] | None:
        headers = self._basic_auth_header() or {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _build_refresh_headers(self) -> dict[str, str] | None:
        headers = self._basic_auth_header() or {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        return headers

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        cred = super()._parse_token_response(token_data)
        if cred.token_type.lower() == "bearer":
            cred = dataclasses.replace(cred, token_type="Bearer")
        return cred

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        new_cred = await super().refresh_token(credential)
        # Preserve metadata across refresh (X-specific).
        if credential.metadata:
            new_cred = dataclasses.replace(new_cred, metadata=credential.metadata)
        if new_cred.token_type.lower() == "bearer":
            new_cred = dataclasses.replace(new_cred, token_type="Bearer")
        return new_cred

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        token = credential.access_token
        if not token:
            return False
        data: dict[str, str] = {
            "token": token,
            "token_type_hint": "access_token",
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.REVOKE_ENDPOINT,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.USERS_ME_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return True
            except Exception:
                return False
