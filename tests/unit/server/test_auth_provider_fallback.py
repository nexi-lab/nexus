"""Tests for the auth-provider DI fallback in auth_routes.

In static-auth mode no DatabaseLocalAuth is injected explicitly, but OAuth
flows still want ``/auth/me`` to work for OAuth-logged-in users. The fallback
builds a DatabaseLocalAuth from the OAuth provider's state so the two share
the same JWT secret and session factory.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
from nexus.server.auth import auth_routes


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_routes, "_auth_provider", None, raising=False)
    monkeypatch.setattr(auth_routes, "_oauth_provider", None, raising=False)
    monkeypatch.setattr(auth_routes, "_synthesized_auth_provider", None, raising=False)


def _fake_oauth_provider() -> MagicMock:
    oauth = MagicMock()
    oauth.session_factory = MagicMock(name="session_factory")
    oauth.local_auth = MagicMock()
    oauth.local_auth.jwt_secret = "test-secret"
    oauth.local_auth.token_expiry = 1234
    return oauth


def test_get_auth_provider_raises_when_nothing_configured() -> None:
    with pytest.raises(HTTPException) as exc:
        auth_routes.get_auth_provider()
    assert exc.value.status_code == 503


def test_get_auth_provider_returns_injected_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    injected = MagicMock(spec=DatabaseLocalAuth)
    monkeypatch.setattr(auth_routes, "_auth_provider", injected)
    assert auth_routes.get_auth_provider() is injected


def test_get_auth_provider_falls_back_to_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    provider = auth_routes.get_auth_provider()
    assert isinstance(provider, DatabaseLocalAuth)
    # Shared session_factory — so the synthesized provider reads the same DB
    assert provider.session_factory is oauth.session_factory
    # Shared jwt_secret — so JWTs issued by OAuth verify here
    assert provider.jwt_secret == "test-secret"
    assert provider.token_expiry == 1234


def test_fallback_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    first = auth_routes.get_auth_provider()
    second = auth_routes.get_auth_provider()
    assert first is second, "synthesized provider should be cached, not rebuilt"


def test_injected_provider_wins_over_oauth_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    injected = MagicMock(spec=DatabaseLocalAuth)
    monkeypatch.setattr(auth_routes, "_auth_provider", injected)
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    assert auth_routes.get_auth_provider() is injected
