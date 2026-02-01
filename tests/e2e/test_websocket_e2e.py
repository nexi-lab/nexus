"""End-to-end tests for WebSocket real-time events.

Tests the WebSocket endpoint with Starlette TestClient.
Issue #1116: Add WebSocket Connection Manager for Real-Time Events

Note: Uses Starlette TestClient for WebSocket testing as the websockets
library has compatibility issues with Python 3.14.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest
from starlette.testclient import TestClient


class TestWebSocketHealthIntegration:
    """Tests for WebSocket integration with health endpoint."""

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

    def test_websocket_connect_and_receive_welcome(self, nexus_fs) -> None:
        """Test basic WebSocket connection and welcome message."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client, client.websocket_connect("/ws/events/all") as ws:
            # Should receive welcome message
            data = ws.receive_json()

            assert data["type"] == "connected"
            assert "connection_id" in data
            assert data["tenant_id"] == "default"

    def test_websocket_ping_pong(self, nexus_fs) -> None:
        """Test ping/pong heartbeat mechanism."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client, client.websocket_connect("/ws/events/all") as ws:
            # Consume welcome message
            ws.receive_json()

            # Send ping
            ws.send_json({"type": "ping"})

            # Should receive pong
            data = ws.receive_json()
            assert data["type"] == "pong"

    def test_websocket_dynamic_subscribe(self, nexus_fs) -> None:
        """Test dynamic subscription via WebSocket message."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client, client.websocket_connect("/ws/events/all") as ws:
            # Consume welcome message
            ws.receive_json()

            # Send subscribe message
            ws.send_json(
                {
                    "type": "subscribe",
                    "patterns": ["/workspace/**/*.py"],
                    "event_types": ["file_write"],
                }
            )

            # Should receive subscribed confirmation
            data = ws.receive_json()
            assert data["type"] == "subscribed"
            assert data["patterns"] == ["/workspace/**/*.py"]

    def test_websocket_with_subscription_id(self, nexus_fs) -> None:
        """Test WebSocket connection with subscription ID."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client, client.websocket_connect("/ws/events/test-sub-123") as ws:
            data = ws.receive_json()
            assert data["type"] == "connected"
            # Connection ID should contain the subscription ID
            assert "test-sub-123" in data["connection_id"]

    def test_websocket_connection_updates_stats(self, nexus_fs) -> None:
        """Test that WebSocket connections update health stats."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client:
            # Get initial stats
            response = client.get("/health/detailed")
            initial = response.json()["components"]["websocket"]["current_connections"]

            # Connect WebSocket
            with client.websocket_connect("/ws/events/all") as ws:
                ws.receive_json()  # welcome

                # Check stats updated
                response = client.get("/health/detailed")
                during = response.json()["components"]["websocket"]["current_connections"]
                assert during == initial + 1


class TestWebSocketPatternFiltering:
    """Tests for WebSocket pattern filtering."""

    def test_pattern_filtering_via_subscribe(self, nexus_fs) -> None:
        """Test that pattern filtering works via subscribe message."""
        from nexus.server.fastapi_server import create_app

        app = create_app(nexus_fs)

        with TestClient(app) as client, client.websocket_connect("/ws/events/all") as ws:
            ws.receive_json()  # welcome

            # Subscribe to Python files only
            ws.send_json(
                {
                    "type": "subscribe",
                    "patterns": ["/src/**/*.py"],
                    "event_types": ["file_write"],
                }
            )

            response = ws.receive_json()
            assert response["type"] == "subscribed"
            assert response["patterns"] == ["/src/**/*.py"]


@pytest.mark.skipif(
    sys.version_info >= (3, 14),
    reason="websockets library has compatibility issues with Python 3.14",
)
class TestWebSocketE2EWithRealServer:
    """E2E tests with real server process (requires websockets library to work)."""

    @pytest.mark.asyncio
    async def test_websocket_with_real_server(self, nexus_server: dict[str, Any]) -> None:
        """Test WebSocket with real server (skipped on Python 3.14+)."""
        import websockets

        base_url = nexus_server["base_url"]
        ws_url = base_url.replace("http://", "ws://") + "/ws/events/all"

        async with websockets.connect(ws_url) as ws:
            msg = await ws.recv()
            import json

            data = json.loads(msg)
            assert data["type"] == "connected"
