"""X (Twitter) OAuth 2.0 PKCE provider (Template Method subclass).

Uses PKCE (Proof Key for Code Exchange) for enhanced security.
``client_secret`` is optional for public clients.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import os
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from nexus.auth.oauth.base_provider import BaseOAuthProvider
from nexus.auth.oauth.types import OAuthCredential, OAuthError


class XOAuthProvider(BaseOAuthProvider):
    """X (Twitter) OAuth 2.0 provider with PKCE support."""

    AUTHORIZATION_ENDPOINT = "https://twitter.com/i/oauth2/authorize"
    TOKEN_ENDPOINT = "https://api.twitter.com/2/oauth2/token"
    REVOKE_ENDPOINT = "https://api.twitter.com/2/oauth2/revoke"
    USERS_ME_ENDPOINT = "https://api.twitter.com/2/users/me"

    DEFAULT_SCOPES = [
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

    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str,
        client_secret: str | None = None,
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret or "",
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            http_client=http_client,
        )

    # ── Authorization URL ──────────────────────────────────────

    def get_authorization_url(self, state: str | None = None, **_kwargs: Any) -> str:
        url, _ = self.get_authorization_url_with_pkce(state)
        return url

    def get_authorization_url_with_pkce(
        self, state: str | None = None
    ) -> tuple[str, dict[str, str]]:
        code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode("utf-8").rstrip("=")
        challenge_bytes = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode("utf-8").rstrip("=")

        params: dict[str, str] = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        params["state"] = state or secrets.token_urlsafe(32)

        auth_url = f"{self.AUTHORIZATION_ENDPOINT}?{urlencode(params)}"
        pkce_data = {
            "code_verifier": code_verifier,
            "code_challenge": code_challenge,
            "state": params["state"],
        }
        return auth_url, pkce_data

    # ── Code exchange ──────────────────────────────────────────

    async def exchange_code(self, _code: str, **_kwargs: Any) -> OAuthCredential:
        raise OAuthError("X OAuth requires PKCE. Use exchange_code_pkce() instead.")

    async def exchange_code_pkce(self, code: str, code_verifier: str) -> OAuthCredential:
        data: dict[str, str] = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": code_verifier,
            "client_id": self.client_id,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret

        headers = self._build_exchange_headers() or {}
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        token_data = await self._post_token_request(data, headers=headers, action="exchange code")
        cred = self._parse_token_response(token_data)
        # Capitalize bearer -> Bearer for consistency
        if cred.token_type.lower() == "bearer":
            cred = dataclasses.replace(cred, token_type="Bearer")
        return cred

    def _build_exchange_params(self, _code: str, **_kwargs: Any) -> dict[str, str]:
        # Not used directly — exchange_code raises. Required by ABC.
        return {}

    def _build_exchange_headers(self) -> dict[str, str] | None:
        if self.client_secret:
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {"Authorization": f"Basic {encoded}"}
        return None

    # ── Refresh ────────────────────────────────────────────────

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token or "",
            "client_id": self.client_id,
        }

    def _build_refresh_headers(self) -> dict[str, str] | None:
        if self.client_secret:
            credentials = f"{self.client_id}:{self.client_secret}"
            encoded = base64.b64encode(credentials.encode()).decode()
            return {
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {encoded}",
            }
        return {"Content-Type": "application/x-www-form-urlencoded"}

    async def refresh_token(self, credential: OAuthCredential) -> OAuthCredential:
        new_cred = await super().refresh_token(credential)
        # X: preserve metadata from old credential
        if credential.metadata:
            new_cred = dataclasses.replace(new_cred, metadata=credential.metadata)
        # Capitalize bearer -> Bearer
        if new_cred.token_type.lower() == "bearer":
            new_cred = dataclasses.replace(new_cred, token_type="Bearer")
        return new_cred

    # ── Revoke ─────────────────────────────────────────────────

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
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False

    # ── Validate ───────────────────────────────────────────────

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.USERS_ME_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return True
            except httpx.HTTPStatusError:
                return False
            except Exception:
                return False
