"""Route-level tests for OAuth CSRF state + browser binding (Issue P2.5).

Covers:
  - /oauth/google/authorize issues a signed state AND sets the binding cookie
  - /oauth/check and /oauth/callback reject callbacks when:
      * state is missing/unknown
      * binding cookie is missing
      * binding cookie is mismatched (login-fixation attack)
  - /oauth/check and /oauth/callback accept callbacks with matching
    state + cookie, and clear the cookie on success.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, Response

from nexus.server.auth import auth_routes, oauth_state_store
from nexus.server.auth.oauth_state_store import (
    OAuthStateService,
    initialize_oauth_state_service,
)


@pytest.fixture(autouse=True)
def reset_state_service(monkeypatch: pytest.MonkeyPatch) -> OAuthStateService:
    """Initialize a fresh state service per test so no state can leak across."""
    svc = initialize_oauth_state_service("test-secret")
    yield svc
    monkeypatch.setattr(oauth_state_store, "_state_service", None, raising=False)


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
    """Install a mock OAuthUserAuth and return the mock.

    Importantly, ``get_google_auth_url`` echoes back whatever ``state`` it is
    given — the real provider does the same, so tests verify that the route
    passes its *signed* state into the provider rather than letting the
    provider mint a random one.
    """
    provider = MagicMock()

    def _build_auth_url(redirect_uri: str | None = None, state: str | None = None):
        assert state is not None, "route must supply signed state"
        return ("https://example/auth", state)

    provider.get_google_auth_url = MagicMock(side_effect=_build_auth_url)
    provider.handle_google_callback = AsyncMock(return_value=(_make_user(), "JWT"))
    monkeypatch.setattr(auth_routes, "_oauth_provider", provider)
    return provider


def _issue_state_for(binding: str) -> str:
    return oauth_state_store.get_oauth_state_service().issue(binding)


async def test_authorize_issues_signed_state_and_sets_binding_cookie(
    mock_oauth_provider: MagicMock, reset_state_service: OAuthStateService
) -> None:
    response = Response()
    resp = await auth_routes.get_google_oauth_url(response=response, redirect_uri=None)

    set_cookie = response.headers.get("set-cookie", "")
    assert auth_routes.OAUTH_BINDING_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "samesite=lax" in set_cookie.lower()

    # The returned state must verify against the cookie nonce — i.e. the
    # route wired the signed state + cookie correctly.
    nonce = _extract_cookie_value(set_cookie, auth_routes.OAUTH_BINDING_COOKIE)
    assert reset_state_service.verify(resp.state, nonce) is True


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
    mock_oauth_provider: MagicMock,
) -> None:
    """State is valid but the caller has no binding cookie — login-fixation attempt."""
    state = _issue_state_for("the-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state=state)
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding=None)
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_rejects_mismatched_binding_cookie(
    mock_oauth_provider: MagicMock,
) -> None:
    """Attacker forwarded (code, state) to victim; victim's cookie doesn't match."""
    state = _issue_state_for("attacker-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state=state)
    response = Response()
    with pytest.raises(HTTPException) as exc:
        await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="victim-nonce")
    assert exc.value.status_code == 400
    mock_oauth_provider.handle_google_callback.assert_not_awaited()


async def test_oauth_callback_accepts_matching_binding(
    mock_oauth_provider: MagicMock,
) -> None:
    state = _issue_state_for("the-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="code-x", state=state)
    response = Response()
    resp = await auth_routes.oauth_callback(req, response=response, nexus_oauth_binding="the-nonce")
    assert resp.token == "JWT"
    mock_oauth_provider.handle_google_callback.assert_awaited_once()
    # Cookie cleared on success
    assert auth_routes.OAUTH_BINDING_COOKIE in response.headers.get("set-cookie", "")


async def test_oauth_callback_preserves_cookie_on_transient_failure(
    mock_oauth_provider: MagicMock,
) -> None:
    """If the token exchange / DB step fails AFTER state verification, the
    binding cookie must remain so the caller can retry without having to
    restart the whole /authorize flow.
    """
    mock_oauth_provider.handle_google_callback = AsyncMock(
        side_effect=RuntimeError("transient network blip")
    )
    state = _issue_state_for("survives-nonce")
    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state=state)
    response = Response()
    with pytest.raises(HTTPException):
        await auth_routes.oauth_callback(
            req, response=response, nexus_oauth_binding="survives-nonce"
        )
    # delete_cookie sets a Set-Cookie header; ensure one was NOT emitted.
    assert auth_routes.OAUTH_BINDING_COOKIE not in response.headers.get("set-cookie", "")


async def test_oauth_callback_verifies_across_workers(mock_oauth_provider: MagicMock) -> None:
    """State issued by 'worker A' must verify at 'worker B' with the same
    signing secret — simulates a load-balanced deployment where authorize
    and callback land on different Python processes.
    """
    # Worker A issues the state
    worker_a = OAuthStateService(signing_secret="test-secret")
    state = worker_a.issue("multi-worker-nonce")

    # Worker B serves the callback (fresh service with same secret)
    initialize_oauth_state_service("test-secret")

    req = auth_routes.OAuthCallbackRequest(provider="google", code="c", state=state)
    resp = await auth_routes.oauth_callback(
        req, response=Response(), nexus_oauth_binding="multi-worker-nonce"
    )
    assert resp.token == "JWT"


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
    mock_oauth_provider: MagicMock,
) -> None:
    state = _issue_state_for("real-nonce")
    mock_oauth_provider._get_provider = MagicMock(
        side_effect=AssertionError("should not be called")
    )
    req = auth_routes.OAuthCheckRequest(provider="google", code="code-x", state=state)
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
