"""Tests for BaseOAuthProvider Template Method (exchange, refresh, revoke, validate)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nexus.auth.oauth.base_provider import BaseOAuthProvider
from nexus.auth.oauth.types import OAuthCredential, OAuthError


class ConcreteProvider(BaseOAuthProvider):
    """Minimal concrete provider for testing Template Method."""

    TOKEN_ENDPOINT = "https://example.com/token"

    def get_authorization_url(self, state: str | None = None, **kwargs: Any) -> str:  # noqa: ARG002
        return f"https://example.com/auth?state={state}"

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:  # noqa: ARG002
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": credential.refresh_token or "",
            "grant_type": "refresh_token",
        }

    async def revoke_token(self, _credential: OAuthCredential) -> bool:
        return True

    async def validate_token(self, _access_token: str) -> bool:
        return True


def _make_transport(status: int = 200, json_data: dict | None = None) -> httpx.MockTransport:
    """Create a mock transport that returns fixed JSON."""
    data = json_data or {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "token_type": "Bearer",
        "expires_in": 3600,
        "scope": "openid email",
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=data)

    return httpx.MockTransport(handler)


class TestBaseExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_code_success(self) -> None:
        client = httpx.AsyncClient(transport=_make_transport())
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
            http_client=client,
        )
        cred = await provider.exchange_code("auth_code")
        assert cred.access_token == "new_access"
        assert cred.refresh_token == "new_refresh"
        assert cred.provider == "test"
        assert isinstance(cred.scopes, tuple)

    @pytest.mark.asyncio
    async def test_exchange_code_http_error(self) -> None:
        client = httpx.AsyncClient(
            transport=_make_transport(status=400, json_data={"error": "invalid_grant"})
        )
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
            http_client=client,
        )
        with pytest.raises(OAuthError, match="Failed to exchange code"):
            await provider.exchange_code("bad_code")


class TestBaseRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_preserves_refresh_token(self) -> None:
        """If server doesn't return new refresh_token, old one is preserved."""
        data = {
            "access_token": "refreshed_access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid",
        }
        client = httpx.AsyncClient(transport=_make_transport(json_data=data))
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
            http_client=client,
        )
        old_cred = OAuthCredential(
            access_token="old",
            refresh_token="old_refresh",
            scopes=("openid",),
            user_email="a@b.com",
        )
        new_cred = await provider.refresh_token(old_cred)
        assert new_cred.access_token == "refreshed_access"
        assert new_cred.refresh_token == "old_refresh"
        assert new_cred.user_email == "a@b.com"

    @pytest.mark.asyncio
    async def test_refresh_no_refresh_token_raises(self) -> None:
        client = httpx.AsyncClient(transport=_make_transport())
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
            http_client=client,
        )
        cred = OAuthCredential(access_token="old")
        with pytest.raises(OAuthError, match="No refresh_token"):
            await provider.refresh_token(cred)


class TestParseTokenResponse:
    def test_parses_standard_response(self) -> None:
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
        )
        data = {
            "access_token": "at",
            "refresh_token": "rt",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": "openid email",
        }
        cred = provider._parse_token_response(data)
        assert cred.access_token == "at"
        assert cred.refresh_token == "rt"
        assert cred.scopes == ("openid", "email")
        assert cred.provider == "test"
        assert cred.expires_at is not None


class TestSharedHttpClient:
    @pytest.mark.asyncio
    async def test_uses_injected_client(self) -> None:
        """When http_client is injected, it's reused (not created per call)."""
        call_count = 0

        def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(
                200,
                json={
                    "access_token": "at",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
            http_client=client,
        )
        await provider.exchange_code("c1")
        await provider.exchange_code("c2")
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_creates_client_when_none(self) -> None:
        """When no http_client injected, creates per-call (backward compat)."""
        provider = ConcreteProvider(
            client_id="cid",
            client_secret="cs",
            redirect_uri="http://localhost/callback",
            scopes=["openid"],
            provider_name="test",
        )
        # Can't easily test this without a real server, but ensure no crash
        assert provider._http_client is None
