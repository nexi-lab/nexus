"""Tests for MCPRateLimitMiddleware (#3779)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp.middleware_ratelimit import install_rate_limit


def _ok(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


@pytest.fixture
def app(monkeypatch) -> Starlette:
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", "3/minute")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", "5/minute")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_PREMIUM", "10/minute")
    monkeypatch.setenv("NEXUS_REDIS_URL", "memory://")
    routes = [Route("/mcp", _ok, methods=["POST"])]
    application = Starlette(routes=routes)
    install_rate_limit(application)
    return application


def test_anonymous_requests_rate_limited(app: Starlette) -> None:
    client = TestClient(app)
    statuses = [client.post("/mcp").status_code for _ in range(5)]
    assert statuses.count(200) == 3
    assert statuses.count(429) == 2


def test_429_response_shape(app: Starlette) -> None:
    client = TestClient(app)
    for _ in range(3):
        client.post("/mcp")
    resp = client.post("/mcp")
    assert resp.status_code == 429
    assert resp.headers.get("Retry-After") is not None
    body = resp.json()
    assert body["error"] == "Rate limit exceeded"
    assert "retry_after" in body


def test_different_tokens_limited_independently(app: Starlette) -> None:
    client = TestClient(app)
    for _ in range(5):
        r = client.post("/mcp", headers={"Authorization": "Bearer sk-z_u1_k_a"})
        assert r.status_code == 200
    for _ in range(5):
        r = client.post("/mcp", headers={"Authorization": "Bearer sk-z_u2_k_b"})
        assert r.status_code == 200


def test_disabled_when_env_false(monkeypatch) -> None:
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("NEXUS_REDIS_URL", "memory://")
    routes = [Route("/mcp", _ok, methods=["POST"])]
    application = Starlette(routes=routes)
    install_rate_limit(application)
    client = TestClient(application)
    for _ in range(20):
        assert client.post("/mcp").status_code == 200
