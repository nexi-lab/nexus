"""Unit tests for the expand= request param on GET /api/v2/search/query (#4398 Task 9).

Covers:
(a) expand=macro is accepted (not a 400) and daemon.search_with_error is called with expand="macro"
(b) expand=bogus returns 400 with the validation detail message
(c) default (expand omitted) passes expand="none" to daemon.search_with_error
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False

pytestmark = pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi test client unavailable")


# ---------------------------------------------------------------------------
# Shared harness
# ---------------------------------------------------------------------------

_AUTH = {
    "authenticated": True,
    "subject_type": "user",
    "subject_id": "alice",
    "zone_id": "eng",
    "zone_perms": [["eng", "r"]],
    "is_admin": False,
}


class _RecordingRunner:
    async def call(self, work: Any) -> Any:
        return await work()


class _RecordingRegistry:
    def __init__(self) -> None:
        self.zones: list[str] = []

    def runner_for(self, zone_id: str) -> _RecordingRunner:
        self.zones.append(zone_id)
        return _RecordingRunner()


def _build_app(daemon: Any) -> "FastAPI":
    """Minimal FastAPI app with search router and a controllable daemon."""
    from nexus.server.api.v2.routers.search import router
    from nexus.server.dependencies import require_auth

    app = FastAPI()
    app.state.search_daemon = daemon
    app.state.permission_enforcer = None
    app.state.zone_registry = _RecordingRegistry()
    app.dependency_overrides[require_auth] = lambda: _AUTH
    app.include_router(router)
    return app


def _make_daemon() -> MagicMock:
    """Return a daemon mock that records .search() calls."""
    daemon = MagicMock()
    daemon.is_initialized = True
    daemon.config = MagicMock()

    async def fake_search(*args: Any, **kwargs: Any) -> tuple[list[Any], str | None]:
        return ([], None)

    daemon.search_with_error = AsyncMock(side_effect=fake_search)
    return daemon


# ---------------------------------------------------------------------------
# (b) expand=bogus -> 400
# ---------------------------------------------------------------------------


def test_expand_bogus_returns_400() -> None:
    """expand=bogus must be rejected with HTTP 400 before touching the daemon."""
    daemon = _make_daemon()
    app = _build_app(daemon)

    with TestClient(app) as client:
        response = client.get("/api/v2/search/query", params={"q": "hello", "expand": "bogus"})

    assert response.status_code == 400, response.text
    body = response.json()
    detail = body.get("detail", "")
    assert "expand" in detail.lower(), f"Expected 'expand' in detail, got: {detail!r}"
    # daemon.search_with_error must NOT have been called
    daemon.search_with_error.assert_not_called()


# ---------------------------------------------------------------------------
# (a) expand=macro is accepted; daemon receives expand="macro"
# ---------------------------------------------------------------------------


def test_expand_macro_accepted_and_threaded() -> None:
    """expand=macro must reach daemon.search_with_error as expand='macro'."""
    daemon = _make_daemon()
    app = _build_app(daemon)

    with TestClient(app) as client:
        response = client.get("/api/v2/search/query", params={"q": "hello", "expand": "macro"})

    # Must NOT be a 400/422
    assert response.status_code != 400, response.text
    assert response.status_code != 422, response.text

    # daemon.search_with_error must have been called with a SearchRequest carrying expand="macro"
    daemon.search_with_error.assert_called_once()
    req = daemon.search_with_error.call_args.args[0]
    assert req.expand == "macro", f"Expected expand='macro' on SearchRequest, got: {req!r}"


# ---------------------------------------------------------------------------
# (c) expand omitted -> daemon receives expand="none"
# ---------------------------------------------------------------------------


def test_expand_default_none_threaded() -> None:
    """When expand is omitted, daemon.search_with_error must be called with expand='none'."""
    daemon = _make_daemon()
    app = _build_app(daemon)

    with TestClient(app) as client:
        response = client.get("/api/v2/search/query", params={"q": "hello"})

    # Response must not be a validation error
    assert response.status_code not in (400, 422), response.text

    daemon.search_with_error.assert_called_once()
    req = daemon.search_with_error.call_args.args[0]
    assert req.expand == "none", f"Expected expand='none' on SearchRequest, got: {req!r}"
