"""Tests for Google OAuth provider."""

import httpx
import pytest

from nexus.auth.oauth.protocol import OAuthProviderProtocol
from nexus.auth.oauth.providers.google import GoogleOAuthProvider
from nexus.auth.oauth.types import OAuthCredential, OAuthError


def _google_token_response() -> dict:
    return {
        "access_token": "ya29.a0test",
        "refresh_token": "1//0etest",
        "token_type": "Bearer",
        "expires_in": 3599,
        "scope": "https://www.googleapis.com/auth/drive",
    }


def _make_provider(transport: httpx.MockTransport | None = None) -> GoogleOAuthProvider:
    client = httpx.AsyncClient(transport=transport) if transport else None
    return GoogleOAuthProvider(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:2026/oauth/callback",
        scopes=["https://www.googleapis.com/auth/drive"],
        provider_name="google-drive",
        http_client=client,
    )


class TestGoogleProtocolConformance:
    def test_satisfies_protocol(self) -> None:
        provider = _make_provider()
        assert isinstance(provider, OAuthProviderProtocol)


class TestGoogleAuthorizationUrl:
    def test_generates_url_with_params(self) -> None:
        provider = _make_provider()
        url = provider.get_authorization_url(state="test_state")
        assert "accounts.google.com" in url
        assert "client_id=test-client-id" in url
        assert "state=test_state" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_override_redirect_uri(self) -> None:
        provider = _make_provider()
        url = provider.get_authorization_url(redirect_uri="http://other/callback")
        assert "http%3A%2F%2Fother%2Fcallback" in url or "http://other/callback" in url


class TestGoogleExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_code_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_google_token_response())

        provider = _make_provider(httpx.MockTransport(handler))
        cred = await provider.exchange_code("auth_code_123")
        assert cred.access_token == "ya29.a0test"
        assert cred.refresh_token == "1//0etest"
        assert cred.provider == "google-drive"
        assert isinstance(cred.scopes, tuple)

    @pytest.mark.asyncio
    async def test_exchange_code_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        provider = _make_provider(httpx.MockTransport(handler))
        with pytest.raises(OAuthError, match="Failed to exchange code"):
            await provider.exchange_code("bad_code")


class TestGoogleRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_preserves_old_refresh_token(self) -> None:
        resp_data = {
            "access_token": "ya29.refreshed",
            "token_type": "Bearer",
            "expires_in": 3599,
        }

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=resp_data)

        provider = _make_provider(httpx.MockTransport(handler))
        old_cred = OAuthCredential(
            access_token="ya29.old",
            refresh_token="1//original",
            scopes=("drive",),
            user_email="a@b.com",
        )
        new_cred = await provider.refresh_token(old_cred)
        assert new_cred.access_token == "ya29.refreshed"
        assert new_cred.refresh_token == "1//original"  # preserved
        assert new_cred.user_email == "a@b.com"

    @pytest.mark.asyncio
    async def test_refresh_no_refresh_token_raises(self) -> None:
        provider = _make_provider()
        cred = OAuthCredential(access_token="ya29.test")
        with pytest.raises(OAuthError, match="No refresh_token"):
            await provider.refresh_token(cred)


class TestGoogleValidateToken:
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"aud": "test-client-id"})

        provider = _make_provider(httpx.MockTransport(handler))
        assert await provider.validate_token("ya29.valid") is True

    @pytest.mark.asyncio
    async def test_validate_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        provider = _make_provider(httpx.MockTransport(handler))
        assert await provider.validate_token("ya29.invalid") is False


class TestGoogleRevokeToken:
    @pytest.mark.asyncio
    async def test_revoke_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        provider = _make_provider(httpx.MockTransport(handler))
        cred = OAuthCredential(access_token="ya29.test", refresh_token="rt")
        assert await provider.revoke_token(cred) is True

    @pytest.mark.asyncio
    async def test_revoke_no_token(self) -> None:
        provider = _make_provider()
        cred = OAuthCredential(access_token="")
        assert await provider.revoke_token(cred) is False
