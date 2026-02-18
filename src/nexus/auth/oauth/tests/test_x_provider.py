"""Tests for X (Twitter) OAuth PKCE provider."""

from __future__ import annotations

import httpx
import pytest

from nexus.auth.oauth.protocol import OAuthProviderProtocol
from nexus.auth.oauth.providers.x import XOAuthProvider
from nexus.auth.oauth.types import OAuthCredential, OAuthError


def _x_token_response() -> dict:
    return {
        "token_type": "bearer",
        "expires_in": 7200,
        "access_token": "VGhpcyBpcytest",
        "refresh_token": "bWlzUyBpcytest",
        "scope": "tweet.read users.read offline.access",
    }


def _make_provider(transport: httpx.MockTransport | None = None) -> XOAuthProvider:
    client = httpx.AsyncClient(transport=transport) if transport else None
    return XOAuthProvider(
        client_id="test-x-client-id",
        redirect_uri="http://localhost:5173/auth/callback",
        scopes=["tweet.read", "users.read", "offline.access"],
        provider_name="x",
        client_secret="test-x-secret",
        http_client=client,
    )


class TestXProtocolConformance:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(_make_provider(), OAuthProviderProtocol)


class TestXAuthorizationUrl:
    def test_generates_url_with_pkce(self) -> None:
        provider = _make_provider()
        url, pkce_data = provider.get_authorization_url_with_pkce(state="test")
        assert "twitter.com/i/oauth2/authorize" in url
        assert "code_challenge=" in url
        assert "code_challenge_method=S256" in url
        assert "code_verifier" in pkce_data
        assert "code_challenge" in pkce_data

    def test_get_authorization_url_compat(self) -> None:
        """get_authorization_url() works (delegates to PKCE version)."""
        provider = _make_provider()
        url = provider.get_authorization_url(state="test")
        assert "twitter.com" in url

    def test_state_generated_when_not_provided(self) -> None:
        provider = _make_provider()
        _, pkce_data = provider.get_authorization_url_with_pkce()
        assert "state" in pkce_data
        assert len(pkce_data["state"]) > 0


class TestXExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_code_raises_requires_pkce(self) -> None:
        provider = _make_provider()
        with pytest.raises(OAuthError, match="PKCE"):
            await provider.exchange_code("code")

    @pytest.mark.asyncio
    async def test_exchange_code_pkce_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_x_token_response())

        provider = _make_provider(httpx.MockTransport(handler))
        cred = await provider.exchange_code_pkce("code", "verifier123")
        assert cred.access_token == "VGhpcyBpcytest"
        assert cred.token_type == "Bearer"  # capitalized
        assert cred.provider == "x"
        assert isinstance(cred.scopes, tuple)

    @pytest.mark.asyncio
    async def test_exchange_code_pkce_with_basic_auth(self) -> None:
        """Confidential clients should use Basic auth."""
        auth_header_seen = None

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal auth_header_seen
            auth_header_seen = _request.headers.get("Authorization")
            return httpx.Response(200, json=_x_token_response())

        provider = _make_provider(httpx.MockTransport(handler))
        await provider.exchange_code_pkce("code", "verifier")
        assert auth_header_seen is not None
        assert auth_header_seen.startswith("Basic ")


class TestXRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_preserves_metadata(self) -> None:
        resp_data = {
            "access_token": "refreshed",
            "token_type": "bearer",
            "expires_in": 7200,
        }

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=resp_data)

        provider = _make_provider(httpx.MockTransport(handler))
        old_cred = OAuthCredential(
            access_token="old",
            refresh_token="old_rt",
            user_email="user@x.com",
            metadata={"x_username": "testuser"},
        )
        new_cred = await provider.refresh_token(old_cred)
        assert new_cred.access_token == "refreshed"
        assert new_cred.refresh_token == "old_rt"
        assert new_cred.user_email == "user@x.com"
        assert new_cred.metadata == {"x_username": "testuser"}


class TestXRevokeToken:
    @pytest.mark.asyncio
    async def test_revoke_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        provider = _make_provider(httpx.MockTransport(handler))
        cred = OAuthCredential(access_token="test_token")
        assert await provider.revoke_token(cred) is True

    @pytest.mark.asyncio
    async def test_revoke_no_token(self) -> None:
        provider = _make_provider()
        cred = OAuthCredential(access_token="")
        assert await provider.revoke_token(cred) is False
