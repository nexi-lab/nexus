"""Integration tests for A2A authentication enforcement.

Tests that the A2A endpoint requires authentication when the server has
auth configured, following real-world A2A implementations (ServiceNow,
LangSmith, etc.) which require auth for all operational endpoints.

Reference: https://a2a-protocol.org/latest/topics/enterprise-ready/
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from nexus.a2a.router import build_router


# ======================================================================
# Fixtures
# ======================================================================


@pytest.fixture
def app_with_auth() -> FastAPI:
    """Create a FastAPI app with auth provider configured."""
    app = FastAPI()
    router = build_router(base_url="http://testserver")
    app.include_router(router)

    # Mock _app_state to simulate auth being enabled
    from nexus.server import fastapi_server

    # Store original values
    original_api_key = getattr(fastapi_server._app_state, "api_key", None)
    original_auth_provider = getattr(
        fastapi_server._app_state, "auth_provider", None
    )

    # Set mock auth provider to enable auth enforcement
    mock_auth_provider = AsyncMock()
    fastapi_server._app_state.auth_provider = mock_auth_provider

    yield app

    # Restore original values
    fastapi_server._app_state.api_key = original_api_key
    fastapi_server._app_state.auth_provider = original_auth_provider


@pytest.fixture
def app_no_auth() -> FastAPI:
    """Create a FastAPI app without auth (open mode)."""
    app = FastAPI()
    router = build_router(base_url="http://testserver")
    app.include_router(router)

    # Ensure _app_state has no auth
    from nexus.server import fastapi_server

    original_api_key = getattr(fastapi_server._app_state, "api_key", None)
    original_auth_provider = getattr(
        fastapi_server._app_state, "auth_provider", None
    )

    fastapi_server._app_state.api_key = None
    fastapi_server._app_state.auth_provider = None

    yield app

    # Restore
    fastapi_server._app_state.api_key = original_api_key
    fastapi_server._app_state.auth_provider = original_auth_provider


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

        body = {
            "jsonrpc": "2.0",
            "method": "a2a.tasks.send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
            "id": "test-1",
        }

        # No Authorization header
        response = client.post("/a2a", json=body)

        # Should return 401 Unauthorized
        assert response.status_code == 401
        data = response.json()
        assert data["error"] == "Unauthorized"
        assert "Authorization header" in data["message"]
        assert response.headers.get("WWW-Authenticate") == 'Bearer realm="A2A"'

    def test_a2a_works_without_auth_in_open_mode(
        self, app_no_auth: FastAPI
    ) -> None:
        """POST /a2a works without auth when server has no auth configured."""
        client = TestClient(app_no_auth)

        body = {
            "jsonrpc": "2.0",
            "method": "a2a.tasks.send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "hello"}],
                }
            },
            "id": "test-2",
        }

        # No Authorization header, but should work in open mode
        response = client.post("/a2a", json=body)

        # Should succeed (200) with task result
        assert response.status_code == 200
        data = response.json()
        assert data["jsonrpc"] == "2.0"
        assert "result" in data
        assert data["result"]["id"]  # Task was created

    def test_streaming_requires_auth_when_enabled(
        self, app_with_auth: FastAPI
    ) -> None:
        """Streaming methods require auth when server has auth enabled."""
        client = TestClient(app_with_auth)

        body = {
            "jsonrpc": "2.0",
            "method": "a2a.tasks.sendStreamingMessage",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "stream"}],
                }
            },
            "id": "test-3",
        }

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
            body = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
                "id": f"test-{method}",
            }

            response = client.post("/a2a", json=body)

            # All should require auth
            assert response.status_code == 401, f"Method {method} should require auth"
            data = response.json()
            assert data["error"] == "Unauthorized"


class TestAuthBestPractices:
    def test_www_authenticate_header_present(self, app_with_auth: FastAPI) -> None:
        """401 responses include WWW-Authenticate header per OAuth 2.0 spec."""
        client = TestClient(app_with_auth)

        body = {
            "jsonrpc": "2.0",
            "method": "a2a.tasks.send",
            "params": {"message": {"role": "user", "parts": []}},
            "id": "test-www-auth",
        }

        response = client.post("/a2a", json=body)

        assert response.status_code == 401
        # Per RFC 6750 (OAuth 2.0 Bearer Token Usage)
        assert "WWW-Authenticate" in response.headers
        assert "Bearer" in response.headers["WWW-Authenticate"]

    def test_error_message_helpful(self, app_with_auth: FastAPI) -> None:
        """401 error includes helpful message for debugging."""
        client = TestClient(app_with_auth)

        body = {
            "jsonrpc": "2.0",
            "method": "a2a.tasks.send",
            "params": {"message": {"role": "user", "parts": []}},
            "id": "test-msg",
        }

        response = client.post("/a2a", json=body)

        assert response.status_code == 401
        data = response.json()
        # Should explain what's needed
        assert "Authorization" in data["message"]
        assert "header" in data["message"].lower()
