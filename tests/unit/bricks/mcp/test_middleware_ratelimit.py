"""Tests for MCPRateLimitMiddleware (#3779)."""

from __future__ import annotations

import asyncio

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp import middleware_ratelimit
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


def test_429_records_rate_limit_hit_tier(monkeypatch, app: Starlette) -> None:
    hits: list[str] = []

    async def fake_record(tier: str) -> None:
        hits.append(tier)

    monkeypatch.setattr(
        "nexus.bricks.mcp.middleware_ratelimit._record_rate_limit_hit",
        fake_record,
        raising=False,
    )

    client = TestClient(app)
    for _ in range(3):
        assert client.post("/mcp").status_code == 200
    assert client.post("/mcp").status_code == 429
    assert hits == ["anonymous"]


def test_record_rate_limit_hit_swallows_redis_client_creation_errors(monkeypatch) -> None:
    redis = pytest.importorskip("redis.asyncio")
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379/0")

    def raise_from_url(_url: str):
        raise RuntimeError("redis client creation failed")

    monkeypatch.setattr(redis, "from_url", raise_from_url)

    asyncio.run(middleware_ratelimit._record_rate_limit_hit("anonymous"))


def test_record_rate_limit_hit_swallows_redis_close_errors(monkeypatch) -> None:
    redis = pytest.importorskip("redis.asyncio")
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://localhost:6379/0")

    class CloseFailingClient:
        async def incr(self, _key: str) -> None:
            return None

        async def expire(self, _key: str, _ttl: int) -> None:
            return None

        async def close(self) -> None:
            raise RuntimeError("redis close failed")

    monkeypatch.setattr(redis, "from_url", lambda _url: CloseFailingClient())

    asyncio.run(middleware_ratelimit._record_rate_limit_hit("anonymous"))


def test_different_tokens_limited_independently(app: Starlette) -> None:
    # sk-z_u1_k_a → zone="z", user="u1" → bucket "user:z:u1"
    # sk-z_u2_k_b → zone="z", user="u2" → bucket "user:z:u2"
    # Premium limit is 10/minute so both users can make 5 requests each.
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


def test_redis_unreachable_falls_back_to_memory(monkeypatch) -> None:
    """install_rate_limit does not raise on bad Redis URI; runtime hits fail-open.

    ``storage_from_string`` lazy-connects, so construction succeeds even for an
    unreachable Redis host.  When ``strategy.hit()`` raises at request time the
    middleware logs a warning and fails open (allows the request) per spec.
    """
    monkeypatch.setenv("MCP_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", "2/minute")
    monkeypatch.setenv("NEXUS_REDIS_URL", "redis://nonexistent.invalid:9999")
    routes = [Route("/mcp", _ok, methods=["POST"])]
    application = Starlette(routes=routes)
    install_rate_limit(application)  # must not raise
    client = TestClient(application)
    statuses = [client.post("/mcp").status_code for _ in range(4)]
    # Redis is unreachable at hit() time → fail-open: all requests pass through.
    assert all(s == 200 for s in statuses)
