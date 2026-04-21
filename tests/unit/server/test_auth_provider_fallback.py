"""Tests for the split auth DI in auth_routes.

``get_auth_provider`` is strict — it raises 503 when no DatabaseLocalAuth is
injected, even if an OAuth provider is configured. It guards email/password
endpoints (register, login, change-password, verify-email, setup-zone) so
that OAuth-only deployments don't silently gain password flows.

``get_token_verifier`` has the OAuth fallback — when no DatabaseLocalAuth is
injected but an OAuth provider is, it synthesizes a DatabaseLocalAuth from
the OAuth provider's session_factory + jwt_secret so ``/auth/me`` works for
OAuth-logged-in users.
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


# -----------------------------------------------------------------------------
# get_auth_provider — strict, no OAuth fallback
# -----------------------------------------------------------------------------


def test_strict_raises_when_nothing_configured() -> None:
    with pytest.raises(HTTPException) as exc:
        auth_routes.get_auth_provider()
    assert exc.value.status_code == 503


def test_strict_raises_in_oauth_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth-only mode must NOT implicitly enable password flows.

    This is the security-critical property of the split: ``get_auth_provider``
    is used by register/login/change-password; returning a synthesized
    provider here would silently allow password auth on an OAuth-only deploy.
    """
    oauth = _fake_oauth_provider()
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)
    with pytest.raises(HTTPException) as exc:
        auth_routes.get_auth_provider()
    assert exc.value.status_code == 503


def test_strict_returns_injected_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    injected = MagicMock(spec=DatabaseLocalAuth)
    monkeypatch.setattr(auth_routes, "_auth_provider", injected)
    assert auth_routes.get_auth_provider() is injected


# -----------------------------------------------------------------------------
# get_token_verifier — JWT-only, OAuth fallback allowed
# -----------------------------------------------------------------------------


def test_verifier_raises_when_nothing_configured() -> None:
    with pytest.raises(HTTPException) as exc:
        auth_routes.get_token_verifier()
    assert exc.value.status_code == 503


def test_verifier_returns_injected_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    injected = MagicMock(spec=DatabaseLocalAuth)
    monkeypatch.setattr(auth_routes, "_auth_provider", injected)
    assert auth_routes.get_token_verifier() is injected


def test_verifier_falls_back_to_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    provider = auth_routes.get_token_verifier()
    assert isinstance(provider, DatabaseLocalAuth)
    # Shared session_factory — so the synthesized provider reads the same DB
    assert provider.session_factory is oauth.session_factory
    # Shared jwt_secret — so JWTs issued by OAuth verify here
    assert provider.jwt_secret == "test-secret"
    assert provider.token_expiry == 1234


def test_verifier_fallback_is_cached_across_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    first = auth_routes.get_token_verifier()
    second = auth_routes.get_token_verifier()
    assert first is second, "synthesized provider should be cached, not rebuilt"


def test_verifier_injected_wins_over_oauth_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    oauth = _fake_oauth_provider()
    injected = MagicMock(spec=DatabaseLocalAuth)
    monkeypatch.setattr(auth_routes, "_auth_provider", injected)
    monkeypatch.setattr(auth_routes, "_oauth_provider", oauth)

    assert auth_routes.get_token_verifier() is injected
