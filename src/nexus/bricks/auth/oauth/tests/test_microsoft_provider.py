"""Tests for Microsoft OAuth provider."""

import httpx
import pytest

from nexus.bricks.auth.oauth.protocol import OAuthProviderProtocol
from nexus.bricks.auth.oauth.providers.microsoft import MicrosoftOAuthProvider
from nexus.bricks.auth.oauth.types import OAuthCredential


def _make_provider(transport: httpx.MockTransport | None = None) -> MicrosoftOAuthProvider:
    client = httpx.AsyncClient(transport=transport) if transport else None
    return MicrosoftOAuthProvider(
        client_id="test-ms-client-id",
        client_secret="test-ms-secret",
        redirect_uri="http://localhost:2026/oauth/callback",
        scopes=["Files.ReadWrite.All"],
        provider_name="microsoft",
        http_client=client,
    )


class TestMicrosoftProtocolConformance:
    def test_satisfies_protocol(self) -> None:
        assert isinstance(_make_provider(), OAuthProviderProtocol)


class TestMicrosoftAuthorizationUrl:
    def test_generates_url_with_params(self) -> None:
        url = _make_provider().get_authorization_url(state="test")
        assert "login.microsoftonline.com" in url
        assert "client_id=test-ms-client-id" in url
        assert "state=test" in url
        assert "offline_access" in url  # auto-added

    def test_offline_access_not_duplicated(self) -> None:
        provider = MicrosoftOAuthProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["Files.ReadWrite.All", "offline_access"],
            provider_name="ms",
        )
        url = provider.get_authorization_url()
        assert url.count("offline_access") == 1


class TestMicrosoftExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_code_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "access_token": "eyJ0test",
                    "refresh_token": "M.R3test",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "Files.ReadWrite.All offline_access",
                },
            )

        provider = _make_provider(httpx.MockTransport(handler))
        cred = await provider.exchange_code("auth_code")
        assert cred.access_token == "eyJ0test"
        assert cred.provider == "microsoft"


class TestMicrosoftRevokeToken:
    @pytest.mark.asyncio
    async def test_revoke_always_true(self) -> None:
        """Microsoft has no revocation API — always returns True."""
        provider = _make_provider()
        cred = OAuthCredential(access_token="test")
        assert await provider.revoke_token(cred) is True


class TestMicrosoftValidateToken:
    @pytest.mark.asyncio
    async def test_validate_success(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"displayName": "Test User"})

        provider = _make_provider(httpx.MockTransport(handler))
        assert await provider.validate_token("valid_token") is True

    @pytest.mark.asyncio
    async def test_validate_failure(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_token"})

        provider = _make_provider(httpx.MockTransport(handler))
        assert await provider.validate_token("invalid") is False
