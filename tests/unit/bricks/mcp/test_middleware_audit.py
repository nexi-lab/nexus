"""Tests for MCPAuditLogMiddleware (#3779)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from nexus.bricks.mcp import middleware_audit
from nexus.bricks.mcp.middleware_audit import MCPAuditLogMiddleware


async def _echo(request: Request) -> JSONResponse:
    body = await request.body()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    return JSONResponse({"echoed": payload})


@pytest.fixture
def captured_records(monkeypatch) -> list[dict]:
    records: list[dict] = []
    monkeypatch.setattr(middleware_audit, "_emit_stdout_record", lambda r: records.append(r))
    monkeypatch.setattr(
        middleware_audit,
        "_publish_record",
        AsyncMock(return_value=None),
    )
    return records


@pytest.fixture
def app() -> Starlette:
    application = Starlette(routes=[Route("/mcp", _echo, methods=["POST"])])
    application.add_middleware(MCPAuditLogMiddleware)
    return application


def test_records_emitted_for_json_rpc_request(app: Starlette, captured_records: list[dict]) -> None:
    client = TestClient(app)
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "nexus_grep", "arguments": {}},
        },
        headers={"Authorization": "Bearer sk-z_u_id_abc"},
    )
    assert resp.status_code == 200
    assert len(captured_records) == 1
    rec = captured_records[0]
    assert rec["event"] == "mcp.request"
    assert rec["rpc_method"] == "tools/call"
    assert rec["tool_name"] == "nexus_grep"
    assert rec["status_code"] == 200
    assert rec["latency_ms"] >= 0
    assert rec["token_hash"] is not None
    assert "ts" in rec


def test_body_preserved_for_downstream(app: Starlette, captured_records: list[dict]) -> None:
    client = TestClient(app)
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    )
    assert resp.json()["echoed"]["method"] == "initialize"


def test_non_json_body_still_logged(app: Starlette, captured_records: list[dict]) -> None:
    client = TestClient(app)
    client.post("/mcp", content=b"not-json", headers={"Content-Type": "text/plain"})
    assert len(captured_records) == 1
    rec = captured_records[0]
    assert rec["rpc_method"] is None
    assert rec["tool_name"] is None


def test_publish_failure_does_not_break_request(app: Starlette, monkeypatch) -> None:
    async def _boom(_record: dict) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(middleware_audit, "_publish_record", _boom)
    captured: list[dict] = []
    monkeypatch.setattr(middleware_audit, "_emit_stdout_record", lambda r: captured.append(r))
    client = TestClient(app)
    resp = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp.status_code == 200
    assert len(captured) == 1


def test_bearer_case_insensitive(app: Starlette, captured_records: list[dict]) -> None:
    client = TestClient(app)
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Authorization": "bearer sk-abc"},  # lowercase bearer
    )
    assert len(captured_records) == 1
    assert captured_records[0]["token_hash"] is not None


def test_x_nexus_api_key_header_hashed(app: Starlette, captured_records: list[dict]) -> None:
    """Regression: X-Nexus-API-Key clients must produce a token_hash so
    active-client metrics don't collapse distinct tokens to 'anonymous' (#3784)."""
    client = TestClient(app)
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"X-Nexus-API-Key": "sk-x-nexus-one"},
    )
    assert len(captured_records) == 1
    assert captured_records[0]["token_hash"] is not None


def test_distinct_x_nexus_api_keys_produce_distinct_hashes(
    app: Starlette, captured_records: list[dict]
) -> None:
    """Two X-Nexus-API-Key clients must hash to two different token_hashes."""
    client = TestClient(app)
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"X-Nexus-API-Key": "sk-one"},
    )
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize"},
        headers={"X-Nexus-API-Key": "sk-two"},
    )
    hashes = {r["token_hash"] for r in captured_records}
    assert None not in hashes
    assert len(hashes) == 2


def test_bearer_and_x_nexus_api_key_hash_same_token_identically(
    app: Starlette, captured_records: list[dict]
) -> None:
    """Same raw token via either header must produce the same hash."""
    client = TestClient(app)
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        headers={"Authorization": "Bearer sk-same"},
    )
    client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "initialize"},
        headers={"X-Nexus-API-Key": "sk-same"},
    )
    assert captured_records[0]["token_hash"] == captured_records[1]["token_hash"]
