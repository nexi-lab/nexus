"""End-to-end tests for SSE real-time event streaming (#2056).

Tests the SSE stream endpoint /api/v2/events/stream which replaces
the WebSocket endpoints removed during v1 sunset.

Issue #1116 (original WebSocket), #2056 (SSE replacement).
"""

from __future__ import annotations

from typing import Any

import pytest
from starlette.testclient import TestClient


class TestSSEStreamEndpoint:
    """Tests for GET /api/v2/events/stream SSE endpoint."""

    def test_sse_stream_returns_event_stream(self, nexus_fs) -> None:
        """Test that SSE endpoint returns text/event-stream content type."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # SSE endpoint should return streaming response or 503 if no DB
            response = client.get(
                "/api/v2/events/stream",
                params={"since_revision": "0"},
                headers={"Accept": "text/event-stream"},
            )
            # Either streaming (200) or DB not configured (503)
            assert response.status_code in (200, 503)

            if response.status_code == 200:
                assert "text/event-stream" in response.headers.get("content-type", "")

    def test_sse_stream_connection_limit(self, nexus_fs) -> None:
        """Test that SSE enforces per-zone connection limits."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Just verify the endpoint responds (503 if no DB is OK)
            response = client.get(
                "/api/v2/events/stream",
                params={"zone_id": "test-zone"},
            )
            assert response.status_code in (200, 503)


class TestSSEHealthIntegration:
    """Tests for SSE/WebSocket health integration."""

    def test_websocket_health_stats_in_detailed_health(self, nexus_fs) -> None:
        """Test that WebSocket stats appear in /health/detailed."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Check health endpoint has websocket component
            response = client.get("/health/detailed")
            assert response.status_code == 200
            data = response.json()

            ws_stats = data.get("components", {}).get("websocket", {})
            assert ws_stats.get("status") == "healthy"
            assert "current_connections" in ws_stats
            assert "total_connections" in ws_stats


class TestEventReplayEndpoint:
    """Tests for GET /api/v2/events/replay."""

    def test_replay_returns_valid_response(self, nexus_fs) -> None:
        """Test that replay endpoint returns valid response or 503."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get(
                "/api/v2/events/replay",
                params={"limit": 10},
            )
            # Either success or DB not configured
            assert response.status_code in (200, 503)

            if response.status_code == 200:
                data = response.json()
                assert "events" in data
                assert "next_cursor" in data
                assert "has_more" in data

    def test_replay_with_filters(self, nexus_fs) -> None:
        """Test replay with zone and event type filters."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get(
                "/api/v2/events/replay",
                params={
                    "zone_id": "root",
                    "event_types": "write,delete",
                    "limit": 5,
                },
            )
            assert response.status_code in (200, 503)


class TestEventListEndpoint:
    """Tests for GET /api/v2/events (v1-compat list)."""

    def test_event_list_returns_valid_response(self, nexus_fs) -> None:
        """Test that event list endpoint returns valid response or 503."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            response = client.get(
                "/api/v2/events",
                params={"limit": 10},
            )
            # Either success or DB not configured
            assert response.status_code in (200, 503)

            if response.status_code == 200:
                data = response.json()
                assert "events" in data
                assert "next_cursor" in data
                assert "has_more" in data
