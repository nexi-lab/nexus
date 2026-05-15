"""Tests for Slack OAuth v2 provider."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from nexus.lib.oauth.providers.slack import SlackOAuthProvider
from nexus.lib.oauth.types import OAuthCredential, OAuthError


def _provider(
    scopes: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> SlackOAuthProvider:
    return SlackOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=scopes or ["channels:read", "chat:write"],
        provider_name="slack",
        http_client=http_client,
    )


def test_authorize_url_uses_comma_scopes() -> None:
    url = _provider().get_authorization_url(state="abc")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == ["channels:read,chat:write"]
    assert q["state"] == ["abc"]
    assert "slack.com/oauth/v2/authorize" in url


@pytest.mark.asyncio
async def test_exchange_code_parses_v2_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/oauth.v2.access"
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        assert body["code"] == "code123"
        assert body["client_id"] == "cid"
        assert body["client_secret"] == "secret"
        return httpx.Response(
            200,
            json={
                "ok": True,
                "access_token": "xoxb-bot-token",
                "token_type": "bot",
                "scope": "channels:read,chat:write",
                "team": {"id": "T1", "name": "Acme"},
                "authed_user": {"id": "U1"},
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cred = await _provider(http_client=client).exchange_code("code123")

    assert cred.access_token == "xoxb-bot-token"
    assert cred.token_type == "bot"
    assert cred.scopes == ("channels:read", "chat:write")
    assert cred.metadata is not None
    assert cred.metadata["team_id"] == "T1"
    assert cred.metadata["team_name"] == "Acme"
    assert cred.metadata["authed_user_id"] == "U1"


@pytest.mark.asyncio
async def test_exchange_code_raises_on_ok_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(OAuthError) as exc_info:
            await _provider(http_client=client).exchange_code("bad")
    assert "invalid_code" in str(exc_info.value)


@pytest.mark.asyncio
async def test_revoke_token_posts_to_auth_revoke() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth.revoke"
        assert request.headers.get("Authorization") == "Bearer xoxb-token"
        return httpx.Response(200, json={"ok": True, "revoked": True})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = _provider(http_client=client)
        cred = OAuthCredential(access_token="xoxb-token", provider="slack")
        assert await provider.revoke_token(cred) is True
