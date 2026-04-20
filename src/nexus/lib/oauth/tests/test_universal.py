"""Tests for :class:`UniversalOAuthProvider`."""

from __future__ import annotations

import httpx
import pytest

from nexus.lib.oauth.discovery import DiscoveryMetadata
from nexus.lib.oauth.types import OAuthCredential
from nexus.lib.oauth.universal import UniversalOAuthProvider


def _meta() -> DiscoveryMetadata:
    return DiscoveryMetadata(
        issuer="https://issuer.example",
        authorization_endpoint="https://issuer.example/authorize",
        token_endpoint="https://issuer.example/token",
        revocation_endpoint="https://issuer.example/revoke",
        scopes_supported=("read", "write"),
        code_challenge_methods_supported=("S256",),
    )


def test_endpoints_from_discovery_metadata() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="generic",
        discovery_metadata=_meta(),
    )
    assert provider.TOKEN_ENDPOINT == "https://issuer.example/token"
    assert provider.AUTHORIZATION_ENDPOINT == "https://issuer.example/authorize"
    assert provider.REVOKE_ENDPOINT == "https://issuer.example/revoke"


def test_endpoints_from_explicit_kwargs() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="generic",
        authorization_endpoint="https://a.example/auth",
        token_endpoint="https://a.example/token",
    )
    assert provider.TOKEN_ENDPOINT == "https://a.example/token"


def test_scope_format_space_default() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["a", "b"],
        provider_name="generic",
        discovery_metadata=_meta(),
    )
    assert provider._scope_string() == "a b"


def test_scope_format_comma() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="secret",
        redirect_uri="http://localhost/callback",
        scopes=["a", "b"],
        provider_name="generic",
        discovery_metadata=_meta(),
        scope_format="comma",
    )
    assert provider._scope_string() == "a,b"


@pytest.mark.asyncio
async def test_exchange_code_posts_standard_params() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        captured.append(body)
        return httpx.Response(
            200,
            json={
                "access_token": "at",
                "refresh_token": "rt",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = UniversalOAuthProvider(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            scopes=["read"],
            provider_name="generic",
            discovery_metadata=_meta(),
            http_client=client,
        )
        cred = await provider.exchange_code("code123")
    assert isinstance(cred, OAuthCredential)
    assert cred.access_token == "at"
    body = captured[0]
    assert body["grant_type"] == "authorization_code"
    assert body["code"] == "code123"
    assert body["client_id"] == "cid"
    assert body["client_secret"] == "secret"


@pytest.mark.asyncio
async def test_refresh_includes_scope_when_scope_on_refresh_true() -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        captured.append(body)
        return httpx.Response(
            200,
            json={"access_token": "at2", "token_type": "Bearer", "expires_in": 3600},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        provider = UniversalOAuthProvider(
            client_id="cid",
            client_secret="secret",
            redirect_uri="http://localhost/cb",
            scopes=["read", "write"],
            provider_name="generic",
            discovery_metadata=_meta(),
            scope_on_refresh=True,
            http_client=client,
        )
        old = OAuthCredential(
            access_token="old",
            refresh_token="rtok",
            provider="generic",
            scopes=("read", "write"),
        )
        await provider.refresh_token(old)

    assert "scope" in captured[0]
    # httpx form-encoding uses + for space, %20 also acceptable;
    # raw unencoded space only if nothing URL-encoded the body.
    assert captured[0]["scope"] in ("read+write", "read%20write", "read write")


def test_requires_pkce_can_be_set_via_ctor() -> None:
    provider = UniversalOAuthProvider(
        client_id="cid",
        client_secret="",
        redirect_uri="http://localhost/cb",
        scopes=["read"],
        provider_name="generic",
        discovery_metadata=_meta(),
        requires_pkce=True,
    )
    assert provider.requires_pkce is True
