"""Google OAuth 2.0 provider.

Thin subclass of :class:`UniversalOAuthProvider` with Google-specific quirks:

- ``access_type=offline`` + ``prompt=consent`` on authorization (required to
  receive refresh tokens).
- Non-standard ``tokeninfo`` validation endpoint (Google doesn't expose RFC 7662
  introspection for consumer OAuth).
- Silent-accept on ``revoke`` failure — Google returns 200 on already-revoked
  tokens but historical code only flipped the success bit on 2xx.
"""

from __future__ import annotations

from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


class GoogleOAuthProvider(UniversalOAuthProvider):
    """Google OAuth 2.0 (Drive / Gmail / Calendar / Cloud Storage)."""

    TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"

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
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
            token_endpoint="https://oauth2.googleapis.com/token",
            revocation_endpoint="https://oauth2.googleapis.com/revoke",
            scope_format="space",
            scope_on_refresh=False,
            requires_pkce=False,
            http_client=http_client,
        )

    def get_authorization_url(
        self,
        state: str | None = None,
        redirect_uri: str | None = None,
        **kwargs: Any,
    ) -> str:
        # Per-call redirect_uri is forwarded as a kwarg so the parent resolves
        # it into a local variable. The GoogleOAuthProvider instance is
        # shared across concurrent requests (one provider per app); mutating
        # ``self.redirect_uri`` here would race against any other authorize
        # request building its URL at the same time.
        extras = {"access_type": "offline", "prompt": "consent"}
        return super().get_authorization_url(
            state=state, redirect_uri=redirect_uri, extra_params=extras, **kwargs
        )

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.get(
                    self.TOKENINFO_ENDPOINT, params={"access_token": access_token}
                )
                response.raise_for_status()
                return True
            except Exception:
                return False

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
