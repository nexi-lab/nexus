"""Slack OAuth v2 provider (bot-token flow)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from nexus.lib.oauth.types import OAuthCredential, OAuthError
from nexus.lib.oauth.universal import UniversalOAuthProvider


class SlackOAuthProvider(UniversalOAuthProvider):
    """Slack OAuth v2 (bot token).

    Slack deviates from RFC 6749 in two ways that matter here:

    - Scopes are comma-separated on the authorize URL (RFC 6749 uses space).
    - The token response is wrapped in a ``{"ok": bool, ...}`` envelope and
      does not follow the ``access_token`` / ``expires_in`` convention for bot
      tokens (bot tokens do not expire). User-scope refresh tokens exist but
      are not handled here — add a user-token subclass if/when needed.
    """

    SLACK_REVOKE_ENDPOINT = "https://slack.com/api/auth.revoke"
    AUTH_TEST_ENDPOINT = "https://slack.com/api/auth.test"

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: list[str],
        provider_name: str = "slack",
        *,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
            provider_name=provider_name,
            authorization_endpoint="https://slack.com/oauth/v2/authorize",
            token_endpoint="https://slack.com/api/oauth.v2.access",
            revocation_endpoint=self.SLACK_REVOKE_ENDPOINT,
            scope_format="comma",
            scope_on_refresh=False,
            requires_pkce=False,
            http_client=http_client,
        )

    def _parse_token_response(self, token_data: dict[str, Any]) -> OAuthCredential:
        if not token_data.get("ok", False):
            raise OAuthError(f"Slack OAuth error: {token_data.get('error', 'unknown')}")

        scope_str: str = token_data.get("scope", "")
        scopes_tuple: tuple[str, ...] | None = tuple(s for s in scope_str.split(",") if s) or None

        expires_at = None
        if "expires_in" in token_data:
            expires_at = datetime.now(UTC) + timedelta(seconds=int(token_data["expires_in"]))

        metadata: dict[str, Any] = {}
        team = token_data.get("team") or {}
        if team.get("id"):
            metadata["team_id"] = team["id"]
        if team.get("name"):
            metadata["team_name"] = team["name"]
        authed_user = token_data.get("authed_user") or {}
        if authed_user.get("id"):
            metadata["authed_user_id"] = authed_user["id"]

        return OAuthCredential(
            access_token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_type=token_data.get("token_type", "bot"),
            expires_at=expires_at,
            scopes=scopes_tuple,
            provider=self.provider_name,
            client_id=self.client_id,
            token_uri=self.TOKEN_ENDPOINT,
            metadata=metadata or None,
        )

    async def revoke_token(self, credential: OAuthCredential) -> bool:
        token = credential.access_token
        if not token:
            return False
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.SLACK_REVOKE_ENDPOINT,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()
                return bool(data.get("ok"))
            except Exception:
                return False

    async def validate_token(self, access_token: str) -> bool:
        async with self._get_client() as client:
            try:
                response = await client.post(
                    self.AUTH_TEST_ENDPOINT,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                return bool(response.json().get("ok"))
            except Exception:
                return False
