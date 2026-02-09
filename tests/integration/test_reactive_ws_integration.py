"""Integration tests for ReactiveSubscriptionManager + WebSocketManager (Issue #1167).

Tests the full flow: register subscription -> event occurs -> WS receives/doesn't.
"""

from __future__ import annotations

import pytest

from nexus.core.event_bus import FileEvent
from nexus.core.reactive_subscriptions import (
    ReactiveSubscriptionManager,
    Subscription,
)
from nexus.core.read_set import ReadSet, ReadSetRegistry
from nexus.server.websocket.manager import WebSocketManager
from helpers.mock_websocket import MockWebSocket


class TestReactiveWSIntegration:
    """Integration tests for ReactiveSubscriptionManager with WebSocketManager."""

    @pytest.fixture
    def registry(self) -> ReadSetRegistry:
        return ReadSetRegistry()

    @pytest.fixture
    def reactive_manager(
        self, registry: ReadSetRegistry
    ) -> ReactiveSubscriptionManager:
        return ReactiveSubscriptionManager(registry=registry)

    @pytest.fixture
    def ws_manager(
        self, reactive_manager: ReactiveSubscriptionManager
    ) -> WebSocketManager:
        return WebSocketManager(reactive_manager=reactive_manager)

    @pytest.mark.asyncio
    async def test_read_set_subscription_receives_event(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Full flow: register read_set sub -> event -> WS receives."""
        await ws_manager.start()

        ws = MockWebSocket()
        await ws_manager.connect(
            websocket=ws,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn1",
        )

        # Register a read-set subscription for conn1
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

        # Broadcast event that matches
        event = FileEvent(
            type="file_write",
            path="/inbox/a.txt",
            zone_id="zone1",
            revision=20,
        )
        sent = await ws_manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        assert len(ws.sent_messages) == 1
        assert ws.sent_messages[0]["type"] == "event"
        assert ws.sent_messages[0]["data"]["path"] == "/inbox/a.txt"

        await ws_manager.stop()

    @pytest.mark.asyncio
    async def test_read_set_filters_non_matching(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Event to unrelated path does not reach WS."""
        await ws_manager.start()

        ws = MockWebSocket()
        await ws_manager.connect(
            websocket=ws,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn1",
        )

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

        # Broadcast event for a different path
        event = FileEvent(
            type="file_write",
            path="/docs/readme.md",
            zone_id="zone1",
            revision=20,
        )
        sent = await ws_manager.broadcast_to_zone("zone1", event)

        assert sent == 0
        assert len(ws.sent_messages) == 0

        await ws_manager.stop()

    @pytest.mark.asyncio
    async def test_pattern_subscription_backward_compat(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Legacy pattern subscriptions still work with reactive manager."""
        await ws_manager.start()

        ws = MockWebSocket()
        await ws_manager.connect(
            websocket=ws,  # type: ignore[arg-type]
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

        event = FileEvent(
            type="file_write",
            path="/inbox/sub/message.txt",
            zone_id="zone1",
            revision=20,
        )
        sent = await ws_manager.broadcast_to_zone("zone1", event)

        assert sent == 1
        assert len(ws.sent_messages) == 1

        await ws_manager.stop()

    @pytest.mark.asyncio
    async def test_mixed_mode_same_zone(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Both read_set and pattern subscriptions coexist in same zone."""
        await ws_manager.start()

        ws1 = MockWebSocket()
        ws2 = MockWebSocket()

        await ws_manager.connect(
            websocket=ws1,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn1",
        )
        await ws_manager.connect(
            websocket=ws2,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn2",
        )

        # conn1: read-set subscription
        rs = ReadSet(query_id="q1", zone_id="zone1")
        rs.record_read("file", "/inbox/a.txt", revision=10)

        sub_rs = Subscription(
            subscription_id="sub_rs",
            connection_id="conn1",
            zone_id="zone1",
            mode="read_set",
            query_id="q1",
        )
        await reactive_manager.register(sub_rs, read_set=rs)

        # conn2: pattern subscription
        sub_pat = Subscription(
            subscription_id="sub_pat",
            connection_id="conn2",
            zone_id="zone1",
            mode="pattern",
            patterns=("/inbox/**/*",),
        )
        await reactive_manager.register(sub_pat)

        # Event that matches both
        event = FileEvent(
            type="file_write",
            path="/inbox/a.txt",
            zone_id="zone1",
            revision=20,
        )
        sent = await ws_manager.broadcast_to_zone("zone1", event)

        assert sent == 2
        assert len(ws1.sent_messages) == 1
        assert len(ws2.sent_messages) == 1

        await ws_manager.stop()

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Disconnect removes subscription, future events not delivered."""
        await ws_manager.start()

        ws = MockWebSocket()
        await ws_manager.connect(
            websocket=ws,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn1",
        )

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

        # Disconnect
        await ws_manager.disconnect("conn1")

        # Verify subscription was cleaned up
        assert "sub1" not in reactive_manager._subscriptions

        # Reconnect different client, event should not be delivered to conn1
        ws2 = MockWebSocket()
        await ws_manager.connect(
            websocket=ws2,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn2",
        )

        event = FileEvent(
            type="file_write",
            path="/inbox/a.txt",
            zone_id="zone1",
            revision=30,
        )
        sent = await ws_manager.broadcast_to_zone("zone1", event)

        # conn2 has no reactive subscriptions, so nothing delivered
        assert sent == 0

        await ws_manager.stop()

    @pytest.mark.asyncio
    async def test_stats_endpoint_includes_reactive(
        self,
        ws_manager: WebSocketManager,
        reactive_manager: ReactiveSubscriptionManager,
    ) -> None:
        """Stats from both managers are available."""
        await ws_manager.start()

        ws = MockWebSocket()
        await ws_manager.connect(
            websocket=ws,  # type: ignore[arg-type]
            zone_id="zone1",
            connection_id="conn1",
        )

        sub = Subscription(
            subscription_id="sub1",
            connection_id="conn1",
            zone_id="zone1",
            mode="pattern",
        )
        await reactive_manager.register(sub)

        ws_stats = ws_manager.get_stats()
        reactive_stats = reactive_manager.get_stats()

        assert ws_stats["current_connections"] == 1
        assert reactive_stats["total_subscriptions"] == 1
        assert reactive_stats["pattern_subscriptions"] == 1
        assert "registry" in reactive_stats

        await ws_manager.stop()
