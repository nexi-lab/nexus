"""Integration tests for A2A protocol endpoints.

Tests the full HTTP request/response cycle using FastAPI's TestClient.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nexus.a2a.models import TaskState
from nexus.a2a.router import build_router
from nexus.a2a.task_manager import TaskManager


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def app() -> FastAPI:
    """Create a minimal FastAPI app with the A2A router."""
    app = FastAPI()
    router = build_router(base_url="http://testserver")
    app.include_router(router)
    return app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _make_rpc(
    method: str, params: dict[str, Any] | None = None, request_id: str | int = "req-1"
) -> dict[str, Any]:
    """Helper to create a JSON-RPC request body."""
    body: dict[str, Any] = {
        "jsonrpc": "2.0",
        "method": method,
        "id": request_id,
    }
    if params is not None:
        body["params"] = params
    return body


# ======================================================================
# Agent Card Discovery
# ======================================================================


class TestAgentCardEndpoint:
    def test_returns_200(self, client: TestClient) -> None:
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200

    def test_returns_json_content_type(self, client: TestClient) -> None:
        resp = client.get("/.well-known/agent.json")
        assert resp.headers["content-type"] == "application/json"

    def test_contains_required_fields(self, client: TestClient) -> None:
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        assert "name" in data
        assert "description" in data
        assert "url" in data
        assert "version" in data
        assert "capabilities" in data
        assert "skills" in data

    def test_url_points_to_a2a_endpoint(self, client: TestClient) -> None:
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        assert data["url"].endswith("/a2a")

    def test_capabilities_streaming_true(self, client: TestClient) -> None:
        resp = client.get("/.well-known/agent.json")
        data = resp.json()
        assert data["capabilities"]["streaming"] is True

    def test_no_auth_required(self, client: TestClient) -> None:
        """Agent Card discovery should work without authentication."""
        resp = client.get("/.well-known/agent.json")
        assert resp.status_code == 200


# ======================================================================
# JSON-RPC: tasks.send
# ======================================================================


class TestTasksSend:
    def test_create_task(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
        )
        resp = client.post("/a2a", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == "req-1"
        assert "result" in data
        assert data["result"]["status"]["state"] == "submitted"

    def test_task_has_id(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
        )
        resp = client.post("/a2a", json=body)
        result = resp.json()["result"]
        assert "id" in result
        assert len(result["id"]) > 0

    def test_invalid_params(self, client: TestClient) -> None:
        body = _make_rpc("a2a.tasks.send", {"bad": "params"})
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32602  # Invalid params


# ======================================================================
# JSON-RPC: tasks.get
# ======================================================================


class TestTasksGet:
    def test_get_task(self, client: TestClient) -> None:
        # Create
        create_body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "test"}],
                }
            },
            request_id="c1",
        )
        create_resp = client.post("/a2a", json=create_body)
        task_id = create_resp.json()["result"]["id"]

        # Get
        get_body = _make_rpc(
            "a2a.tasks.get", {"taskId": task_id}, request_id="g1"
        )
        get_resp = client.post("/a2a", json=get_body)
        data = get_resp.json()
        assert data["result"]["id"] == task_id

    def test_get_nonexistent_task(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.get", {"taskId": "nonexistent"}, request_id="g2"
        )
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32001  # Task not found


# ======================================================================
# JSON-RPC: tasks.cancel
# ======================================================================


class TestTasksCancel:
    def test_cancel_task(self, client: TestClient) -> None:
        # Create
        create_body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "cancel me"}],
                }
            },
        )
        create_resp = client.post("/a2a", json=create_body)
        task_id = create_resp.json()["result"]["id"]

        # Cancel
        cancel_body = _make_rpc(
            "a2a.tasks.cancel", {"taskId": task_id}, request_id="cancel-1"
        )
        cancel_resp = client.post("/a2a", json=cancel_body)
        data = cancel_resp.json()
        assert data["result"]["status"]["state"] == "canceled"

    def test_cancel_nonexistent(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.cancel", {"taskId": "nope"}, request_id="cancel-2"
        )
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert data["error"]["code"] == -32001


# ======================================================================
# JSON-RPC: Error handling
# ======================================================================


class TestErrorHandling:
    def test_unknown_method(self, client: TestClient) -> None:
        body = _make_rpc("a2a.nonexistent.method")
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert data["error"]["code"] == -32601  # Method not found

    def test_push_notification_not_supported(self, client: TestClient) -> None:
        body = _make_rpc("a2a.tasks.createPushNotificationConfig")
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert data["error"]["code"] == -32006

    def test_invalid_json_body(self, client: TestClient) -> None:
        resp = client.post(
            "/a2a",
            content=b"not json",
            headers={"content-type": "application/json"},
        )
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32700  # Parse error

    def test_invalid_rpc_structure(self, client: TestClient) -> None:
        resp = client.post("/a2a", json={"not": "rpc"})
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32600  # Invalid request

    def test_response_always_has_jsonrpc_field(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
        )
        resp = client.post("/a2a", json=body)
        assert resp.json()["jsonrpc"] == "2.0"

    def test_response_echoes_request_id(self, client: TestClient) -> None:
        body = _make_rpc(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
            request_id=42,
        )
        resp = client.post("/a2a", json=body)
        assert resp.json()["id"] == 42


# ======================================================================
# Extended Agent Card
# ======================================================================


class TestExtendedAgentCard:
    def test_returns_card(self, client: TestClient) -> None:
        body = _make_rpc("a2a.agent.getExtendedAgentCard")
        resp = client.post("/a2a", json=body)
        data = resp.json()
        assert "result" in data
        assert "name" in data["result"]
        assert "capabilities" in data["result"]
