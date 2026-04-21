"""Microsoft OAuth 2.0 provider (Microsoft Identity Platform / Graph)."""

from __future__ import annotations

from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


class MicrosoftOAuthProvider(UniversalOAuthProvider):
    """Microsoft Identity Platform (``common`` tenant).

    Quirks:

    - Scope list auto-includes ``offline_access`` (required for refresh tokens).
    - Authorize URL adds ``response_mode=query``.
    - Refresh requires resending the scope (``scope_on_refresh=True``).
    - No standard revocation endpoint; ``revoke_token`` is a no-op.
    - Token validation uses Graph ``/me``.
    """

    GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"

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
        scopes_with_offline = list(scopes)
        if "offline_access" not in scopes_with_offline:
            scopes_with_offline.append("offline_access")
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes_with_offline,
            provider_name=provider_name,
            authorization_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
            token_endpoint="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            scope_format="space",
            scope_on_refresh=True,
            requires_pkce=False,
            http_client=http_client,
        )

    def get_authorization_url(self, state: str | None = None, **_kwargs: Any) -> str:
        return super().get_authorization_url(state=state, extra_params={"response_mode": "query"})

    async def revoke_token(self, _credential: OAuthCredential) -> bool:
        # Microsoft has no standard revocation API; treat as success.
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
            except Exception:
                return False
