"""Regression tests for the hub-mode MCP HTTP auth gate (#3784).

Pre-fix: ``nexus mcp serve --transport http`` with ``NEXUS_DATABASE_URL``
set (hub mode) would extract the bearer token into ``_request_api_key``
but never validate it. Local mode's ``_get_nexus_instance`` always
returned the default (unauthenticated) NexusFS, so any request on the
public MCP port got full filesystem access regardless of the token.

These tests verify the fail-closed gate:
- missing/empty bearer → 401
- bogus bearer → 401
- valid bearer → passes through (200)
- /health passes through unauthenticated
- auth provider raising → 503 (not silently passing)
- builder correctly gates on NEXUS_DATABASE_URL and absence of NEXUS_URL.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.cli.commands.mcp import _build_hub_auth_provider, _HubAuthGateMiddleware


class _FakeAuth:
    def __init__(self, valid_tokens: set[str], raise_on: set[str] | None = None) -> None:
        self._valid = valid_tokens
        self._raise = raise_on or set()

    async def authenticate(self, token: str) -> Any:
        if token in self._raise:
            raise RuntimeError("db down")
        return SimpleNamespace(authenticated=token in self._valid)


async def _ok(_request: Any) -> Any:
    return PlainTextResponse("ok")


async def _health(_request: Any) -> Any:
    return PlainTextResponse("healthy")


def _make_client(auth: Any) -> TestClient:
    app = Starlette(
        routes=[Route("/mcp", _ok, methods=["GET", "POST"]), Route("/health", _health)],
    )
    app.add_middleware(_HubAuthGateMiddleware, auth_provider=auth)
    return TestClient(app)


class TestHubAuthGate:
    def test_missing_token_returns_401(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.post("/mcp")
        assert resp.status_code == 401
        assert "missing_bearer_token" in resp.text
        assert resp.headers["WWW-Authenticate"].startswith("Bearer")

    def test_bogus_bearer_returns_401(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.post("/mcp", headers={"Authorization": "Bearer sk-bogus"})
        assert resp.status_code == 401
        assert "invalid_or_revoked_token" in resp.text

    def test_empty_bearer_returns_401(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.post("/mcp", headers={"Authorization": "Bearer "})
        assert resp.status_code == 401

    def test_valid_bearer_passes_through(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.post("/mcp", headers={"Authorization": "Bearer sk-good"})
        assert resp.status_code == 200
        assert resp.text == "ok"

    def test_x_nexus_api_key_header_accepted(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.post("/mcp", headers={"X-Nexus-API-Key": "sk-good"})
        assert resp.status_code == 200

    def test_health_bypass_without_auth(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}))
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.text == "healthy"

    def test_auth_provider_raises_returns_503(self) -> None:
        client = _make_client(_FakeAuth({"sk-good"}, raise_on={"sk-boom"}))
        resp = client.post("/mcp", headers={"Authorization": "Bearer sk-boom"})
        assert resp.status_code == 503


class TestBuildHubAuthProvider:
    def test_no_database_url_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NEXUS_DATABASE_URL", raising=False)
        monkeypatch.delenv("NEXUS_URL", raising=False)
        assert _build_hub_auth_provider() is None

    def test_remote_mode_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NEXUS_URL set means remote mode — per-request keys flow to the
        remote server's auth layer, so the gate should not activate."""
        monkeypatch.setenv("NEXUS_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.setenv("NEXUS_URL", "http://remote:2026")
        assert _build_hub_auth_provider() is None

    def test_hub_mode_constructs_provider(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NEXUS_DATABASE_URL", "sqlite:///:memory:")
        monkeypatch.delenv("NEXUS_URL", raising=False)
        provider = _build_hub_auth_provider()
        assert provider is not None
        from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth

        assert isinstance(provider, DatabaseAPIKeyAuth)
