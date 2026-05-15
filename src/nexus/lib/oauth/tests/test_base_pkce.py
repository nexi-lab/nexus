"""Tests for optional PKCE support in :class:`BaseOAuthProvider`."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from nexus.lib.oauth.base import BaseOAuthProvider
from nexus.lib.oauth.types import OAuthCredential, OAuthError


class _DummyProvider(BaseOAuthProvider):
    TOKEN_ENDPOINT = "https://example.com/token"
    AUTHORIZATION_ENDPOINT = "https://example.com/auth"
    requires_pkce = True

    def get_authorization_url(self, state: str | None = None, **_kwargs: Any) -> str:
        url, _ = self.get_authorization_url_with_pkce(state=state)
        return url

    def _build_exchange_params(self, code: str, **kwargs: Any) -> dict[str, str]:
        params = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
        }
        code_verifier = kwargs.get("code_verifier")
        if code_verifier:
            params["code_verifier"] = code_verifier
        return params

    def _build_refresh_params(self, credential: OAuthCredential) -> dict[str, str]:
        return {
            "grant_type": "refresh_token",
            "refresh_token": credential.refresh_token or "",
            "client_id": self.client_id,
        }

    async def revoke_token(self, credential: OAuthCredential) -> bool:  # noqa: ARG002
        return True

    async def validate_token(self, access_token: str) -> bool:  # noqa: ARG002
        return True


def _make_provider() -> _DummyProvider:
    return _DummyProvider(
        client_id="cid",
        client_secret="",
        redirect_uri="http://localhost/callback",
        scopes=["read"],
        provider_name="dummy",
    )


def test_pkce_url_contains_code_challenge_s256() -> None:
    provider = _make_provider()
    url, pkce = provider.get_authorization_url_with_pkce()
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert qs["code_challenge_method"] == ["S256"]
    assert qs["code_challenge"][0] == pkce["code_challenge"]
    assert len(pkce["code_verifier"]) >= 43


def test_pkce_requires_verifier_on_exchange() -> None:
    provider = _make_provider()
    with pytest.raises(OAuthError) as exc_info:
        asyncio.run(provider.exchange_code("abc"))
    assert "pkce" in str(exc_info.value).lower() or "code_verifier" in str(exc_info.value).lower()
