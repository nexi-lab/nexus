"""Route-level tests for OAuth CSRF state validation (Issue P2.5).

Covers the three touchpoints:
  - /oauth/google/authorize registers state before returning it
  - /oauth/check rejects callbacks whose state is missing/unknown/replayed
  - /oauth/callback rejects callbacks whose state is missing/unknown/replayed
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from nexus.server.auth import auth_routes
from nexus.server.auth.oauth_state_store import OAuthStateStore


@pytest.fixture(autouse=True)
def reset_state_store(monkeypatch: pytest.MonkeyPatch) -> OAuthStateStore:
    """Give each test a fresh state store so state from earlier tests can't leak in."""
    store = OAuthStateStore()
    monkeypatch.setattr(
        "nexus.server.auth.oauth_state_store._state_store",
        store,
        raising=False,
    )
    return store


def _make_user(**overrides: Any) -> MagicMock:
    defaults: dict[str, Any] = {
        "user_id": "user-1",
        "email": "u@x.co",
        "username": "u",
        "display_name": "User",
        "avatar_url": None,
        "is_global_admin": 0,
    }
    defaults.update(overrides)
    user = MagicMock()
    for k, v in defaults.items():
        setattr(user, k, v)
    return user


@pytest.fixture
def mock_oauth_provider(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a mock OAuthUserAuth and return the mock."""
    provider = MagicMock()
    provider.get_google_auth_url = MagicMock(return_value=("https://example/auth", "STATE-123"))
    provider.handle_google_callback = AsyncMock(return_value=(_make_user(), "JWT"))
    monkeypatch.setattr(auth_routes, "_oauth_provider", provider)
    return provider


async def test_authorize_registers_state_in_store(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    response = await auth_routes.get_google_oauth_url(redirect_uri=None)
    assert response.state == "STATE-123"
    # consume succeeds once -> it was registered
    assert reset_state_store.consume("STATE-123") is True


async def test_oauth_callback_rejects_missing_state(mock_oauth_provider: MagicMock) -> None:
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state=None)
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req)
    assert exc.value.status_code == 400
    assert "CSRF" in exc.value.detail
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_rejects_unknown_state(mock_oauth_provider: MagicMock) -> None:
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state="forged")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req)
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_accepts_registered_state(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    reset_state_store.register("valid-state")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state="valid-state")
    resp = await auth_routes.oauth_callback(req)
    assert resp.token == "JWT"
    mock_oauth_provider.handle_google_callback.assert_awaited_once()


async def test_oauth_callback_rejects_replayed_state(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    reset_state_store.register("one-shot")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="one-shot")
    await auth_routes.oauth_callback(req)  # first use consumes it

    # replay
    req2 = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="one-shot")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req2)
    assert exc.value.status_code == 400


async def test_oauth_check_rejects_missing_state(
    mock_oauth_provider: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # oauth_check reaches for ._get_provider and ._extract_user_info — if the
    # state gate fails first, these should never be called.
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    mock_oauth_provider._extract_user_info = AsyncMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state=None)
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_check(req)
    assert exc.value.status_code == 400
    assert "CSRF" in exc.value.detail


async def test_oauth_check_rejects_unknown_state(mock_oauth_provider: MagicMock) -> None:
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state="forged")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_check(req)
    assert exc.value.status_code == 400
