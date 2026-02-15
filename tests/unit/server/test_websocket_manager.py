"""Unit tests for WebSocket manager.

Tests the WebSocketManager class for real-time event streaming.
Issue #1116: Add WebSocket Connection Manager for Real-Time Events
Issue #1170: Batch Subscription Updates for Consistent Client State
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from nexus.core.reactive_subscriptions import (
    ReactiveSubscriptionManager,
    Subscription,
)
from nexus.core.read_set import ReadSet, ReadSetRegistry
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
            zone_id="zone1",
            connection_id="conn1",
            user_id="user1",
        )

        assert mock_websocket.accepted
        assert conn_info.zone_id == "zone1"
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
            zone_id="zone1",
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
            zone_id="zone1",
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
            zone_id="zone1",
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
    async def test_broadcast_to_zone(self, manager: WebSocketManager) -> None:
        """Test broadcasting events to zone connections."""
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
            zone_id="zone1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            zone_id="zone1",
            connection_id="conn2",
        )

        # Broadcast event
        sent = await manager.broadcast_to_zone("zone1", mock_event)

        assert sent == 2
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1
        assert ws1.sent_messages[0]["type"] == "event"
        assert ws2.sent_messages[0]["type"] == "event"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_broadcast_with_pattern_filter(self, manager: WebSocketManager) -> None:
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
            zone_id="zone1",
            connection_id="conn1",
            patterns=["/workspace/**/*.py"],  # Matches
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            zone_id="zone1",
            connection_id="conn2",
            patterns=["/inbox/**/*"],  # Doesn't match
        )

        # Broadcast event
        sent = await manager.broadcast_to_zone("zone1", mock_event)

        assert sent == 1  # Only one client matches
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_broadcast_with_event_type_filter(self, manager: WebSocketManager) -> None:
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
            zone_id="zone1",
            connection_id="conn1",
            event_types=["file_write", "file_delete"],  # Matches
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            zone_id="zone1",
            connection_id="conn2",
            event_types=["file_write"],  # Doesn't match
        )

        # Broadcast event
        sent = await manager.broadcast_to_zone("zone1", mock_event)

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
            zone_id="zone1",
            connection_id="conn1",
        )

        stats = manager.get_stats()

        assert stats["total_connections"] == 1
        assert stats["current_connections"] == 1
        assert stats["connections_by_zone"] == {"zone1": 1}

        await manager.disconnect("conn1")

        stats = manager.get_stats()
        assert stats["total_connections"] == 1
        assert stats["total_disconnections"] == 1
        assert stats["current_connections"] == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_zone_isolation(self, manager: WebSocketManager) -> None:
        """Test that events are isolated by zone."""
        await manager.start()

        # Create mock event for zone1
        mock_event = MagicMock()
        mock_event.type = "file_write"
        mock_event.path = "/workspace/main.py"
        mock_event.to_dict.return_value = {"type": "file_write", "path": "/workspace/main.py"}

        # Connect clients to different zones
        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            zone_id="zone1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            zone_id="zone2",
            connection_id="conn2",
        )

        # Broadcast to zone1 only
        sent = await manager.broadcast_to_zone("zone1", mock_event)

        assert sent == 1
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 0  # zone2 should not receive

        await manager.stop()

    @pytest.mark.asyncio
    async def test_graceful_shutdown(self, manager: WebSocketManager) -> None:
        """Test that shutdown closes all connections gracefully."""
        await manager.start()

        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await manager.connect(
            websocket=ws1,  # type: ignore
            zone_id="zone1",
            connection_id="conn1",
        )
        await manager.connect(
            websocket=ws2,  # type: ignore
            zone_id="zone2",
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
            zone_id="zone1",
            subscription_id=None,
            user_id=None,
        )

        assert conn.zone_id == "zone1"
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
            zone_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            zone_id="test",
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
            zone_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            zone_id="test",
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
            zone_id="test",
        )

        conn = ConnectionInfo(
            websocket=MagicMock(),
            zone_id="test",
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
            zone_id="test",
        )

        conn_matches = ConnectionInfo(
            websocket=MagicMock(),
            zone_id="test",
            subscription_id=None,
            user_id=None,
            event_types=["file_delete"],
        )

        conn_no_match = ConnectionInfo(
            websocket=MagicMock(),
            zone_id="test",
            subscription_id=None,
            user_id=None,
            event_types=["file_write"],
        )

        assert manager._matches_filters(event, conn_matches)
        assert not manager._matches_filters(event, conn_no_match)


# ---------------------------------------------------------------------------
# TestBatchSubscriptionUpdates (#1170)
# ---------------------------------------------------------------------------


class TestBatchSubscriptionUpdates:
    """Integration tests for batch_update message format.

    Issue #1170: Batch Subscription Updates for Consistent Client State
    """

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def reactive_manager(self, registry: ReadSetRegistry) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    @pytest.fixture
    def manager(self, reactive_manager: ReactiveSubscriptionManager) -> WebSocketManager:
        """WebSocketManager with reactive manager configured."""
        return WebSocketManager(reactive_manager=reactive_manager)

    def _make_event(
        self,
        path: str = "/inbox/a.txt",
        zone_id: str = "zone1",
        event_type: str = "file_write",
        revision: int = 42,
    ) -> MagicMock:
        """Create a mock FileEvent."""
        event = MagicMock()
        event.type = event_type
        event.path = path
        event.zone_id = zone_id
        event.revision = revision
        event.timestamp = "2026-02-15T10:00:00"
        event.to_dict.return_value = {
            "type": event_type,
            "path": path,
            "zone_id": zone_id,
            "revision": revision,
            "timestamp": "2026-02-15T10:00:00",
        }
        return event

    @pytest.mark.asyncio
    async def test_batch_update_message_format(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Batch update contains correct structure: type, event, commit_id, timestamp, updates."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(
            websocket=ws,  # type: ignore
            zone_id="zone1",
            connection_id="conn1",
        )

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
            patterns=("/inbox/**/*",),
        )
        await reactive_manager.register(sub)

        event = self._make_event(path="/inbox/a.txt")
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        assert len(ws.sent_messages) == 1
        msg = ws.sent_messages[0]
        assert msg["type"] == "batch_update"
        assert msg["commit_id"] == 42
        assert msg["timestamp"] == "2026-02-15T10:00:00"
        assert "event" in msg
        assert msg["event"]["path"] == "/inbox/a.txt"
        assert len(msg["updates"]) == 1
        assert msg["updates"][0]["subscription_id"] == "sub1"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_groups_multiple_subs(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Multiple subs on same connection grouped into single batch message."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(
            websocket=ws,  # type: ignore
            zone_id="zone1",
            connection_id="conn1",
        )

        # Two pattern subscriptions on same connection
        sub1 = Subscription(
            subscription_id="sub_list",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
            query_id="q_list",
        )
        sub2 = Subscription(
            subscription_id="sub_count",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
            query_id="q_count",
        )
        await reactive_manager.register(sub1)
        await reactive_manager.register(sub2)

        event = self._make_event()
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 1  # One message to one connection
        assert len(ws.sent_messages) == 1
        msg = ws.sent_messages[0]
        assert msg["type"] == "batch_update"
        sub_ids = {u["subscription_id"] for u in msg["updates"]}
        assert sub_ids == {"sub_list", "sub_count"}

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_separate_connections(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Different connections get separate batch messages."""
        await manager.start()

        ws1 = MockWebSocket()
        ws2 = MockWebSocket()
        await manager.connect(websocket=ws1, zone_id="zone1", connection_id="conn1")  # type: ignore
        await manager.connect(websocket=ws2, zone_id="zone1", connection_id="conn2")  # type: ignore

        sub1 = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
        )
        sub2 = Subscription(
            subscription_id="sub2",
            connection_id="conn2",
            zone_id="zone1",
            mode="pattern",
        )
        await reactive_manager.register(sub1)
        await reactive_manager.register(sub2)

        event = self._make_event()
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 2
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1
        assert ws1.sent_messages[0]["type"] == "batch_update"
        assert ws2.sent_messages[0]["type"] == "batch_update"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_no_match_sends_nothing(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """No matching subscriptions sends no messages."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(websocket=ws, zone_id="zone1", connection_id="conn1")  # type: ignore

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
            patterns=("/docs/**/*.md",),
        )
        await reactive_manager.register(sub)

        event = self._make_event(path="/inbox/a.txt")
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 0
        assert len(ws.sent_messages) == 0

        await manager.stop()

    @pytest.mark.asyncio
    async def test_legacy_fallback_without_reactive_manager(self) -> None:
        """Without reactive manager, falls back to legacy event messages."""
        manager = WebSocketManager()  # No reactive_manager
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(websocket=ws, zone_id="zone1", connection_id="conn1")  # type: ignore

        event = self._make_event()
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        assert len(ws.sent_messages) == 1
        msg = ws.sent_messages[0]
        assert msg["type"] == "event"  # Legacy format, not batch_update
        assert "data" in msg

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_with_read_set_subscription(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Read-set subscriptions work with batch format."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(websocket=ws, zone_id="zone1", connection_id="conn1")  # type: ignore

        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="read_set",
            query_id="q1",
        )
        await reactive_manager.register(sub, read_set=rs)

        event = self._make_event(path="/inbox/a.txt", revision=20)
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        msg = ws.sent_messages[0]
        assert msg["type"] == "batch_update"
        assert msg["updates"][0]["subscription_id"] == "sub1"
        assert msg["updates"][0]["query_id"] == "q1"

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_stats_tracking(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Batch sends update message stats correctly."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(websocket=ws, zone_id="zone1", connection_id="conn1")  # type: ignore

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
        )
        await reactive_manager.register(sub)

        event = self._make_event()
        await manager.broadcast_to_zone("zone1", event)

        stats = manager.get_stats()
        assert stats["total_messages_sent"] == 1

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_failed_connection_cleanup(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Failed sends trigger connection cleanup."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(websocket=ws, zone_id="zone1", connection_id="conn1")  # type: ignore

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
        )
        await reactive_manager.register(sub)

        # Close websocket to trigger send failure
        ws.closed = True

        event = self._make_event()
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 0
        assert manager.get_connection_count() == 0  # Connection cleaned up

        await manager.stop()

    @pytest.mark.asyncio
    async def test_batch_fallback_on_reactive_error(
        self,
        manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """When reactive lookup raises, falls back to legacy event messages."""
        await manager.start()

        ws = MockWebSocket()
        await manager.connect(
            websocket=ws,  # type: ignore
            zone_id="zone1",
            connection_id="conn1",
        )

        # Make reactive manager raise on lookup
        reactive_manager.find_affected_subscriptions = MagicMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("registry corrupted"),
        )

        event = self._make_event()
        sent = await manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        msg = ws.sent_messages[0]
        assert msg["type"] == "event"  # Fell back to legacy format

        await manager.stop()
