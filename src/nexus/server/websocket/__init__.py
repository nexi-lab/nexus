"""WebSocket support for real-time event streaming.

This module provides WebSocket-based push notifications for file system events,
complementing the existing webhook subscription system. It enables browsers and
AI agents to receive real-time events without HTTP polling overhead.

Issue #1116: Add WebSocket Connection Manager for Real-Time Events
Epic: #1109 (Real-Time Event System)

Architecture:
- WebSocketManager: Manages connections, broadcasts events
- Integration with EventBus: Bridges Redis Pub/Sub to WebSocket clients
- Authentication: Token-based via query parameter (browser compatible)
- Heartbeat: Application-level ping/pong for connection health

Example:
    # Client connects with token
    ws = websocket.connect("ws://localhost:2026/ws/events/sub123?token=sk-xxx")

    # Server pushes events
    {"type": "event", "data": {"event": "file_write", "path": "/workspace/main.py"}}

    # Heartbeat
    {"type": "ping"} -> {"type": "pong"}
"""

from nexus.server.websocket.manager import WebSocketManager

__all__ = ["WebSocketManager"]
