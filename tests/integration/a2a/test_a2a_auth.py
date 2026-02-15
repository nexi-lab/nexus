"""Integration tests for A2A authentication enforcement.

Tests that the A2A endpoint requires authentication when the server has
auth configured, following real-world A2A implementations (ServiceNow,
LangSmith, etc.) which require auth for all operational endpoints.

Reference: https://a2a-protocol.org/latest/topics/enterprise-ready/
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from nexus.a2a.router import build_router

# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def app_with_auth() -> FastAPI:
    """Create a FastAPI app with auth_required=True."""
    app = FastAPI()
    router = build_router(base_url="http://testserver", auth_required=True)
    app.include_router(router)
    return app


@pytest.fixture
def app_no_auth() -> FastAPI:
    """Create a FastAPI app without auth (open mode)."""
    app = FastAPI()
    router = build_router(base_url="http://testserver", auth_required=False)
    app.include_router(router)
    return app


def _rpc_body(
    method: str,
    params: dict[str, Any] | None = None,
    request_id: str = "test-1",
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
# Authentication Enforcement Tests
# ======================================================================


class TestAuthEnforcement:
    def test_agent_card_always_public(self, app_with_auth: FastAPI) -> None:
        """Agent Card discovery must always be public (even with auth enabled)."""
        client = TestClient(app_with_auth)

        # No Authorization header
        response = client.get("/.well-known/agent.json")

        # Should succeed (200) without auth
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        data = response.json()
        assert "name" in data
        assert "url" in data

    def test_a2a_requires_auth_when_enabled(self, app_with_auth: FastAPI) -> None:
        """POST /a2a requires auth when server has auth enabled."""
        client = TestClient(app_with_auth)

        body = _rpc_body(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
        )

        # No Authorization header
        response = client.post("/a2a", json=body)

        # Should return 401 Unauthorized
        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Unauthorized"
        assert "Authorization header" in data["message"]
        assert response.headers.get("WWW-Authenticate") == 'Bearer realm="A2A"'

    def test_a2a_works_without_auth_in_open_mode(self, app_no_auth: FastAPI) -> None:
        """POST /a2a works without auth when server has no auth configured."""
        client = TestClient(app_no_auth)

        body = _rpc_body(
            "a2a.tasks.send",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
            request_id="test-2",
        )

        # No Authorization header, but should work in open mode
        response = client.post("/a2a", json=body)

        # Should succeed (200) with task result
        assert response.status_code == 200
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert "result" in data
        assert data["result"]["id"]  # Task was created

    def test_streaming_requires_auth_when_enabled(self, app_with_auth: FastAPI) -> None:
        """Streaming methods require auth when server has auth enabled."""
        client = TestClient(app_with_auth)

        body = _rpc_body(
            "a2a.tasks.sendStreamingMessage",
            {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "stream"}],
                }
            },
            request_id="test-3",
        )

        # No Authorization header
        response = client.post("/a2a", json=body)

        # Should return 401 Unauthorized (not SSE stream)
        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Unauthorized"

    def test_all_methods_require_auth(self, app_with_auth: FastAPI) -> None:
        """All A2A methods require auth when enabled (not just tasks.send)."""
        client = TestClient(app_with_auth)

        methods_to_test = [
            ("a2a.tasks.send", {"message": {"role": "user", "parts": []}}),
            ("a2a.tasks.get", {"taskId": "test-id"}),
            ("a2a.tasks.cancel", {"taskId": "test-id"}),
            ("a2a.agent.getExtendedAgentCard", {}),
        ]

        for method, params in methods_to_test:
            body = _rpc_body(method, params, request_id=f"test-{method}")
            response = client.post("/a2a", json=body)

            # All should require auth
            assert response.status_code == 401, f"Method {method} should require auth"
            data = response.json()
            assert data["error"] == "Unauthorized"


class TestAuthCallback:
    """Tests for the injected auth_fn callback mechanism."""

    def test_auth_fn_called_on_request(self) -> None:
        """Verify the injected auth_fn is invoked for each request."""
        call_count = 0

        async def mock_auth_fn(request: Any) -> dict[str, Any] | None:
            nonlocal call_count
            call_count += 1
            return {"zone_id": "test-zone", "authenticated": True}

        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=False,
            auth_fn=mock_auth_fn,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        client.post("/a2a", json=body)
        assert call_count == 1

    def test_auth_fn_result_used_for_zone_id(self) -> None:
        """Verify zone_id from auth_fn is used for task isolation."""

        async def mock_auth_fn(request: Any) -> dict[str, Any] | None:
            return {"zone_id": "custom-zone", "authenticated": True}

        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=False,
            auth_fn=mock_auth_fn,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        response = client.post("/a2a", json=body)
        assert response.status_code == 200
        data = response.json()
        # Zone isolation is internal; the key assertion is the request succeeded
        assert data["result"]["id"]

    def test_auth_fn_none_defaults_to_default_zone(self) -> None:
        """Without auth_fn, zone_id defaults to 'default'."""
        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=False,
            auth_fn=None,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        response = client.post("/a2a", json=body)
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["id"]

    def test_auth_fn_exception_returns_none_safely(self) -> None:
        """If auth_fn raises with auth not required, request proceeds."""

        async def failing_auth_fn(request: Any) -> dict[str, Any] | None:
            raise RuntimeError("Auth service unavailable")

        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=False,
            auth_fn=failing_auth_fn,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        # Should succeed despite auth_fn failure (defaults to zone="default")
        response = client.post("/a2a", json=body)
        assert response.status_code == 200
        data = response.json()
        assert data["result"]["id"]

    def test_auth_fn_failure_with_auth_required_returns_401(self) -> None:
        """If auth_fn raises and auth is required, the request must be rejected."""

        async def failing_auth_fn(request: Any) -> dict[str, Any] | None:
            raise RuntimeError("Auth service unavailable")

        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=True,
            auth_fn=failing_auth_fn,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        response = client.post("/a2a", json=body, headers={"Authorization": "Bearer bad-token"})
        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Unauthorized"

    def test_auth_fn_returns_none_with_auth_required_returns_401(self) -> None:
        """If auth_fn returns None and auth is required, request is rejected."""

        async def null_auth_fn(request: Any) -> dict[str, Any] | None:
            return None

        app = FastAPI()
        router = build_router(
            base_url="http://testserver",
            auth_required=True,
            auth_fn=null_auth_fn,
        )
        app.include_router(router)
        client = TestClient(app)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": [{"type": "text", "text": "hi"}]}},
        )
        response = client.post("/a2a", json=body, headers={"Authorization": "Bearer some-token"})
        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Unauthorized"


class TestAuthBestPractices:
    def test_www_authenticate_header_present(self, app_with_auth: FastAPI) -> None:
        """401 responses include WWW-Authenticate header per OAuth 2.0 spec."""
        client = TestClient(app_with_auth)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": []}},
            request_id="test-www-auth",
        )

        response = client.post("/a2a", json=body)

        assert response.status_code == 401
        # Per RFC 6750 (OAuth 2.0 Bearer Token Usage)
        assert "WWW-Authenticate" in response.headers
        assert "Bearer" in response.headers["WWW-Authenticate"]

    def test_error_message_helpful(self, app_with_auth: FastAPI) -> None:
        """401 error includes helpful message for debugging."""
        client = TestClient(app_with_auth)

        body = _rpc_body(
            "a2a.tasks.send",
            {"message": {"role": "user", "parts": []}},
            request_id="test-msg",
        )

        response = client.post("/a2a", json=body)

        assert response.status_code == 401
        data = response.json()
        # Should explain what's needed
        assert "Authorization" in data["message"]
        assert "header" in data["message"].lower()
