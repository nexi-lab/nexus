"""Unit tests for WebSocket manager.

Tests the WebSocketManager class for real-time event streaming.
Issue #1116: Add WebSocket Connection Manager for Real-Time Events
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.server.websocket.manager import (
    ConnectionInfo,
    WebSocketManager,
)


class MockWebSocket:
    """Mock WebSocket for testing."""

    def __init__(self) -> None:
        self.accepted = False
        self.closed = False
        self.close_code: int | None = None
        self.close_reason: str | None = None
        self.sent_messages: list[dict[str, Any]] = []
        self.receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_json(self, data: dict[str, Any]) -> None:
        if self.closed:
            raise RuntimeError("WebSocket closed")
        self.sent_messages.append(data)

    async def receive_json(self) -> dict[str, Any]:
        return await self.receive_queue.get()

    def add_message(self, data: dict[str, Any]) -> None:
        """Add a message to the receive queue."""
        self.receive_queue.put_nowait(data)


class TestWebSocketManager:
    """Tests for WebSocketManager."""

    @pytest.fixture
    def manager(self) -> WebSocketManager:
        """Create a WebSocket manager for testing."""
        return WebSocketManager()

    @pytest.fixture
    def mock_websocket(self) -> MockWebSocket:
        """Create a mock WebSocket."""
        return MockWebSocket()

    @pytest.mark.asyncio
    async def test_start_stop(self, manager: WebSocketManager) -> None:
        """Test manager start and stop."""
        assert not manager._started

        await manager.start()
        assert manager._started
        assert manager._heartbeat_task is not None

        await manager.stop()
        assert not manager._started
        assert manager._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_connect_disconnect(
        self, manager: WebSocketManager, mock_websocket: MockWebSocket
    ) -> None:
        """Test connection and disconnection."""
        await manager.start()

        conn_info = await manager.connect(
            websocket=mock_websocket,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
            user_id="user1",
        )

        assert mock_websocket.accepted
        assert conn_info.tenant_id == "tenant1"
        assert conn_info.user_id == "user1"
        assert manager.get_connection_count() == 1

        await manager.disconnect("conn1")
        assert manager.get_connection_count() == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_connect_with_patterns(
        self, manager: WebSocketManager, mock_websocket: MockWebSocket
    ) -> None:
        """Test connection with pattern filters."""
        await manager.start()

        conn_info = await manager.connect(
            websocket=mock_websocket,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
            patterns=["/workspace/**/*.py"],
            event_types=["file_write", "file_delete"],
        )

        assert conn_info.patterns == ["/workspace/**/*.py"]
        assert conn_info.event_types == ["file_write", "file_delete"]

        await manager.stop()

    @pytest.mark.asyncio
    async def test_handle_pong(
        self, manager: WebSocketManager, mock_websocket: MockWebSocket
    ) -> None:
        """Test handling pong messages."""
        await manager.start()

        await manager.connect(
            websocket=mock_websocket,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )

        # Simulate pong message
        await manager._handle_client_message("conn1", {"type": "pong"})

        conn_info = manager.get_connection_info("conn1")
        assert conn_info is not None
        assert conn_info.messages_received == 1

        await manager.stop()

    @pytest.mark.asyncio
    async def test_handle_subscribe(
        self, manager: WebSocketManager, mock_websocket: MockWebSocket
    ) -> None:
        """Test dynamic subscription via message."""
        await manager.start()

        await manager.connect(
            websocket=mock_websocket,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )

        # Send subscribe message
        await manager._handle_client_message(
            "conn1",
            {
                "type": "subscribe",
                "patterns": ["/inbox/**/*"],
                "event_types": ["file_write"],
            },
        )

        conn_info = manager.get_connection_info("conn1")
        assert conn_info is not None
        assert conn_info.patterns == ["/inbox/**/*"]
        assert conn_info.event_types == ["file_write"]

        # Check that subscribed confirmation was sent
        assert len(mock_websocket.sent_messages) == 1
        assert mock_websocket.sent_messages[0]["type"] == "subscribed"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_broadcast_to_tenant(
        self, manager: WebSocketManager
    ) -> None:
        """Test broadcasting events to tenant connections."""
        await manager.start()

        # Create mock event
        mock_event = MagicMock()
        mock_event.type = "file_write"
        mock_event.path = "/workspace/main.py"
        mock_event.to_dict.return_value = {
            "type": "file_write",
            "path": "/workspace/main.py",
        }

        # Connect two clients
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn2",
        )

        # Broadcast event
        sent = await manager.broadcast_to_tenant("tenant1", mock_event)

        assert sent == 2
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1
        assert ws1.sent_messages[0]["type"] == "event"
        assert ws2.sent_messages[0]["type"] == "event"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_broadcast_with_pattern_filter(
        self, manager: WebSocketManager
    ) -> None:
        """Test that pattern filters are applied during broadcast."""
        await manager.start()

        # Create mock event for Python file
        mock_event = MagicMock()
        mock_event.type = "file_write"
        mock_event.path = "/workspace/main.py"
        mock_event.to_dict.return_value = {
            "type": "file_write",
            "path": "/workspace/main.py",
        }

        # Connect two clients with different patterns
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
            patterns=["/workspace/**/*.py"],  # Matches
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn2",
            patterns=["/inbox/**/*"],  # Doesn't match
        )

        # Broadcast event
        sent = await manager.broadcast_to_tenant("tenant1", mock_event)

        assert sent == 1  # Only one client matches
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_broadcast_with_event_type_filter(
        self, manager: WebSocketManager
    ) -> None:
        """Test that event type filters are applied during broadcast."""
        await manager.start()

        # Create mock event
        mock_event = MagicMock()
        mock_event.type = "file_delete"
        mock_event.path = "/workspace/main.py"
        mock_event.to_dict.return_value = {
            "type": "file_delete",
            "path": "/workspace/main.py",
        }

        # Connect two clients with different event type filters
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
            event_types=["file_write", "file_delete"],  # Matches
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn2",
            event_types=["file_write"],  # Doesn't match
        )

        # Broadcast event
        sent = await manager.broadcast_to_tenant("tenant1", mock_event)

        assert sent == 1  # Only one client matches
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_get_stats(
        self, manager: WebSocketManager, mock_websocket: MockWebSocket
    ) -> None:
        """Test getting manager statistics."""
        await manager.start()

        await manager.connect(
            websocket=mock_websocket,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )

        stats = manager.get_stats()

        assert stats["total_connections"] == 1
        assert stats["current_connections"] == 1
        assert stats["connections_by_tenant"] == {"tenant1": 1}

        await manager.disconnect("conn1")

        stats = manager.get_stats()
        assert stats["total_connections"] == 1
        assert stats["total_disconnections"] == 1
        assert stats["current_connections"] == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_tenant_isolation(
        self, manager: WebSocketManager
    ) -> None:
        """Test that events are isolated by tenant."""
        await manager.start()

        # Create mock event for tenant1
        mock_event = MagicMock()
        mock_event.type = "file_write"
        mock_event.path = "/workspace/main.py"
        mock_event.to_dict.return_value = {"type": "file_write", "path": "/workspace/main.py"}

        # Connect clients to different tenants
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            tenant_id="tenant2",
            connection_id="conn2",
        )

        # Broadcast to tenant1 only
        sent = await manager.broadcast_to_tenant("tenant1", mock_event)

        assert sent == 1
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0  # tenant2 should not receive

        await manager.stop()

    @pytest.mark.asyncio
    async def test_graceful_shutdown(
        self, manager: WebSocketManager
    ) -> None:
        """Test that shutdown closes all connections gracefully."""
        await manager.start()

        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            tenant_id="tenant1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            tenant_id="tenant2",
            connection_id="conn2",
        )

        await manager.stop()

        assert ws1.closed
        assert ws2.closed
        assert ws1.close_code == 1001  # Going away
        assert ws2.close_code == 1001


class TestConnectionInfo:
    """Tests for ConnectionInfo dataclass."""

    def test_default_values(self) -> None:
        """Test default values are set correctly."""
        ws = MockWebSocket()
        conn = ConnectionInfo(
            websocket=ws,  # type: ignore
            tenant_id="tenant1",
            subscription_id=None,
            user_id=None,
        )

        assert conn.tenant_id == "tenant1"
        assert conn.subscription_id is None
        assert conn.user_id is None
        assert conn.patterns == []
        assert conn.event_types == []
        assert conn.messages_sent == 0
        assert conn.messages_received == 0
        assert conn.connected_at > 0
        assert conn.last_pong > 0


class TestPatternMatching:
    """Tests for pattern matching in the manager."""

    @pytest.fixture
    def manager(self) -> WebSocketManager:
        return WebSocketManager()

    def test_glob_star_pattern(self, manager: WebSocketManager) -> None:
        """Test glob * pattern matching."""
        from nexus.core.event_bus import FileEvent

        event = FileEvent(
            type="file_write",
            path="/workspace/main.py",
            tenant_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            tenant_id="test",
            subscription_id=None,
            user_id=None,
            patterns=["/workspace/*.py"],
        )

        assert manager._matches_filters(event, conn)

    def test_double_star_pattern(self, manager: WebSocketManager) -> None:
        """Test ** recursive pattern matching."""
        from nexus.core.event_bus import FileEvent

        event = FileEvent(
            type="file_write",
            path="/workspace/src/main.py",
            tenant_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            tenant_id="test",
            subscription_id=None,
            user_id=None,
            patterns=["/workspace/**/*.py"],
        )

        assert manager._matches_filters(event, conn)

    def test_no_pattern_matches_all(self, manager: WebSocketManager) -> None:
        """Test that empty patterns match all events."""
        from nexus.core.event_bus import FileEvent

        event = FileEvent(
            type="file_write",
            path="/any/path/file.txt",
            tenant_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            tenant_id="test",
            subscription_id=None,
            user_id=None,
            patterns=[],  # No patterns = match all
        )

        assert manager._matches_filters(event, conn)

    def test_event_type_filter(self, manager: WebSocketManager) -> None:
        """Test event type filtering."""
        from nexus.core.event_bus import FileEvent

        event = FileEvent(
            type="file_delete",
            path="/workspace/main.py",
            tenant_id="test",
        )

        conn_matches = ConnectionInfo(
            websocket=MagicMock(),
            tenant_id="test",
            subscription_id=None,
            user_id=None,
            event_types=["file_delete"],
        )

        conn_no_match = ConnectionInfo(
            websocket=MagicMock(),
            tenant_id="test",
            subscription_id=None,
            user_id=None,
            event_types=["file_write"],
        )

        assert manager._matches_filters(event, conn_matches)
        assert not manager._matches_filters(event, conn_no_match)
