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
