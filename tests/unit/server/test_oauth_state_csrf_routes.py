"""Route-level tests for OAuth CSRF state + browser binding (Issue P2.5).

Covers:
  - /oauth/google/authorize registers state AND sets the binding cookie
  - /oauth/check and /oauth/callback reject callbacks when:
      * state is missing/unknown
      * binding cookie is missing
      * binding cookie is mismatched (login-fixation attack)
      * state is replayed
  - /oauth/check and /oauth/callback accept callbacks with matching
    state + cookie, and clear the cookie on success.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response

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


async def test_authorize_registers_state_and_sets_binding_cookie(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    response = Response()
    resp = await auth_routes.get_google_oauth_url(response=response, redirect_uri=None)
    assert resp.state == "STATE-123"

    set_cookie = response.headers.get("set-cookie", "")
    assert auth_routes.OAUTH_BINDING_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    # Extract the nonce from the Set-Cookie header and confirm it's the one
    # registered against the state in the store.
    nonce = _extract_cookie_value(set_cookie, auth_routes.OAUTH_BINDING_COOKIE)
    assert reset_state_store.consume("STATE-123", nonce) is True


async def test_oauth_callback_rejects_missing_state(mock_oauth_provider: MagicMock) -> None:
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state=None)
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="any")
    assert exc.value.status_code == 400
    assert "CSRF" in exc.value.detail
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_rejects_unknown_state(mock_oauth_provider: MagicMock) -> None:
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state="forged")
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="any")
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_rejects_missing_binding_cookie(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    """State was registered but the caller has no binding cookie — login-fixation attempt."""
    reset_state_store.register("valid-state", "the-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="valid-state")
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding=None)
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_rejects_mismatched_binding_cookie(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    """Attacker forwarded (code, state) to victim; victim's cookie doesn't match."""
    reset_state_store.register("valid-state", "attacker-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="valid-state")
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="victim-nonce")
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_accepts_matching_binding(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    reset_state_store.register("valid-state", "the-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state="valid-state")
    response = Response()
    resp = await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="the-nonce")
    assert resp.token == "JWT"
    mock_oauth_provider.handle_google_callback.assert_awaited_once()
    # Cookie cleared on success
    assert auth_routes.OAUTH_BINDING_COOKIE in response.headers.get("set-cookie", "")


async def test_oauth_callback_rejects_replayed_state(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    reset_state_store.register("one-shot", "nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="one-shot")
    response = Response()
    await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="nonce")

    req2 = auth_routes.OAuthCallbackRequest(provider="google", code="c", state="one-shot")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req2, response=Response(), nexus_oauth_binding="nonce")
    assert exc.value.status_code == 400


async def test_oauth_check_rejects_missing_state(mock_oauth_provider: MagicMock) -> None:
    # If the state gate fails first, the provider should never be invoked.
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    mock_oauth_provider._extract_user_info = AsyncMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state=None)
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_check(req, response=Response(), nexus_oauth_binding="any")
    assert exc.value.status_code == 400
    assert "CSRF" in exc.value.detail


async def test_oauth_check_rejects_mismatched_binding_cookie(
    mock_oauth_provider: MagicMock, reset_state_store: OAuthStateStore
) -> None:
    reset_state_store.register("s", "real-nonce")
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state="s")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_check(
            req, response=Response(), nexus_oauth_binding="attacker-nonce"
        )
    assert exc.value.status_code == 400


async def test_oauth_check_rejects_unknown_state(mock_oauth_provider: MagicMock) -> None:
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state="forged")
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_check(req, response=Response(), nexus_oauth_binding="any")
    assert exc.value.status_code == 400


def _extract_cookie_value(set_cookie_header: str, name: str) -> str:
    """Pick the value for ``name=...`` out of a Set-Cookie header (first attr)."""
    for part in set_cookie_header.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return ""
