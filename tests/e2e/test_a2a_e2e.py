"""E2E tests for A2A protocol endpoint.

Tests the full A2A lifecycle against a real ``nexus serve`` process:
- Agent Card discovery
- Task creation via JSON-RPC
- Task retrieval and history
- Task cancellation
- Error handling
- SSE streaming endpoint

These are TRUE e2e tests — they start the actual server subprocess.

Note: Auth enforcement is tested in integration tests
(tests/integration/a2a/test_a2a_auth.py). The E2E server runs
in open-access mode per the shared conftest fixture.
"""

from __future__ import annotations

from typing import Any

import httpx


def _rpc_body(
    method: str,
    params: dict[str, Any] | None = None,
    request_id: str = "e2e-1",
) -> dict[str, Any]:
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


class TestAgentCardE2E:
    """Agent Card discovery via /.well-known/agent.json."""

    def test_agent_card_returns_200(self, test_app: httpx.Client) -> None:
        response = test_app.get("/.well-known/agent.json")
        assert response.status_code == 200

    def test_agent_card_is_valid_json(self, test_app: httpx.Client) -> None:
        response = test_app.get("/.well-known/agent.json")
        data = response.json()
        assert "name" in data
        assert "url" in data
        assert "version" in data

    def test_agent_card_capabilities(self, test_app: httpx.Client) -> None:
        """Agent Card includes A2A capabilities."""
        response = test_app.get("/.well-known/agent.json")
        data = response.json()
        assert data.get("capabilities", {}).get("streaming") is True
        assert data.get("capabilities", {}).get("pushNotifications") is False


# ======================================================================
# Task Lifecycle
# ======================================================================


class TestTaskLifecycleE2E:
    """Full task lifecycle: create -> get -> cancel."""

    def test_create_task(self, test_app: httpx.Client) -> None:
        """Create a task via a2a.tasks.send."""
        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "e2e test"}]}},
            request_id="create-1",
        )
        resp = test_app.post("/a2a", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == "create-1"
        assert "result" in data
        assert data["result"]["id"]  # UUID assigned
        assert data["result"]["status"]["state"] == "submitted"

    def test_create_and_get_task(self, test_app: httpx.Client) -> None:
        """Create a task, then retrieve it by ID."""
        # Create
        create_body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "roundtrip"}]}},
            request_id="create-2",
        )
        create_resp = test_app.post("/a2a", json=create_body)
        task_id = create_resp.json()["result"]["id"]

        # Get
        get_body = _rpc_body("a2a.tasks.get", {"taskId": task_id}, request_id="get-1")
        get_resp = test_app.post("/a2a", json=get_body)
        assert get_resp.status_code == 200
        get_data = get_resp.json()
        assert get_data["result"]["id"] == task_id
        assert get_data["result"]["status"]["state"] == "submitted"
        # History includes the original message
        assert len(get_data["result"]["history"]) >= 1

    def test_cancel_task(self, test_app: httpx.Client) -> None:
        """Create then cancel a task."""
        # Create
        create_body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "cancel me"}]}},
            request_id="create-cancel",
        )
        task_id = test_app.post("/a2a", json=create_body).json()["result"]["id"]

        # Cancel
        cancel_body = _rpc_body("a2a.tasks.cancel", {"taskId": task_id}, request_id="cancel-1")
        cancel_resp = test_app.post("/a2a", json=cancel_body)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["result"]["status"]["state"] == "canceled"

        # Verify the state persists
        get_body = _rpc_body("a2a.tasks.get", {"taskId": task_id}, request_id="verify-cancel")
        get_resp = test_app.post("/a2a", json=get_body)
        assert get_resp.json()["result"]["status"]["state"] == "canceled"

    def test_get_nonexistent_task(self, test_app: httpx.Client) -> None:
        """Getting a nonexistent task returns JSON-RPC error."""
        body = _rpc_body("a2a.tasks.get", {"taskId": "nonexistent-id"}, request_id="get-missing")
        resp = test_app.post("/a2a", json=body)
        assert resp.status_code == 200  # JSON-RPC errors are 200 with error payload
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32001  # TaskNotFoundError

    def test_cancel_terminal_task_fails(self, test_app: httpx.Client) -> None:
        """Cannot cancel a task that is already in a terminal state."""
        # Create + cancel
        create_body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "done"}]}},
            request_id="create-term",
        )
        task_id = test_app.post("/a2a", json=create_body).json()["result"]["id"]
        cancel_body = _rpc_body("a2a.tasks.cancel", {"taskId": task_id}, request_id="cancel-term")
        test_app.post("/a2a", json=cancel_body)

        # Try to cancel again
        cancel2_body = _rpc_body(
            "a2a.tasks.cancel", {"taskId": task_id}, request_id="cancel-term-2"
        )
        resp = test_app.post("/a2a", json=cancel2_body)
        data = resp.json()
        assert "error" in data  # TaskNotCancelableError


# ======================================================================
# Error Handling
# ======================================================================


class TestErrorHandlingE2E:
    def test_unknown_method(self, test_app: httpx.Client) -> None:
        body = _rpc_body("a2a.tasks.unknown", {}, request_id="err-1")
        resp = test_app.post("/a2a", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == -32601  # Method not found

    def test_invalid_json(self, test_app: httpx.Client) -> None:
        resp = test_app.post(
            "/a2a",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data

    def test_push_notification_not_supported(self, test_app: httpx.Client) -> None:
        body = _rpc_body("a2a.tasks.createPushNotificationConfig", {}, request_id="push-1")
        resp = test_app.post("/a2a", json=body)
        data = resp.json()
        assert "error" in data

    def test_response_echoes_request_id(self, test_app: httpx.Client) -> None:
        body = _rpc_body("a2a.tasks.get", {"taskId": "x"}, request_id="echo-id-42")
        resp = test_app.post("/a2a", json=body)
        assert resp.json()["id"] == "echo-id-42"


# ======================================================================
# Streaming
# ======================================================================


class TestStreamingE2E:
    def test_send_streaming_returns_sse(self, test_app: httpx.Client) -> None:
        """sendStreamingMessage returns SSE content type with task data."""
        body = _rpc_body(
            "a2a.tasks.sendStreamingMessage",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "stream test"}]}},
            request_id="stream-1",
        )
        with test_app.stream("POST", "/a2a", json=body) as resp:
            assert resp.status_code == 200
            assert "text/event-stream" in resp.headers.get("content-type", "")
            # Read first SSE event
            first_chunk = b""
            for chunk in resp.iter_bytes():
                first_chunk += chunk
                if b"\n\n" in first_chunk:
                    break
            assert b"data:" in first_chunk

    def test_extended_agent_card(self, test_app: httpx.Client) -> None:
        """a2a.agent.getExtendedAgentCard returns agent card data."""
        body = _rpc_body("a2a.agent.getExtendedAgentCard", {}, request_id="card-1")
        resp = test_app.post("/a2a", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert "result" in data
        assert "name" in data["result"]
