"""WebSocket Connection Manager for real-time event streaming.

This module implements a high-performance WebSocket manager that bridges
Redis Pub/Sub events to connected WebSocket clients.

Issue #1116: Add WebSocket Connection Manager for Real-Time Events

Performance Considerations:
- O(1) connection lookup via nested dict structure
- Single Redis subscription per tenant (not per connection)
- Efficient pattern matching reuses existing fnmatch logic
- Lazy tenant subscription (only subscribe when first client connects)
- Background task cleanup on disconnect
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

if TYPE_CHECKING:
    from nexus.core.event_bus import EventBusProtocol, FileEvent

logger = logging.getLogger(__name__)

# Constants
PING_INTERVAL = 25.0  # seconds - slightly less than typical 30s proxy timeout
PING_TIMEOUT = 10.0  # seconds to wait for pong response
MAX_MESSAGE_SIZE = 1024 * 1024  # 1MB max message size


@dataclass
class ConnectionInfo:
    """Metadata for a WebSocket connection."""

    websocket: WebSocket
    tenant_id: str
    subscription_id: str | None  # None for tenant-wide subscriptions
    user_id: str | None
    connected_at: float = field(default_factory=time.time)
    last_pong: float = field(default_factory=time.time)
    patterns: list[str] = field(default_factory=list)  # Glob patterns to filter
    event_types: list[str] = field(default_factory=list)  # Event types to filter
    messages_sent: int = 0
    messages_received: int = 0


@dataclass
class WebSocketStats:
    """Statistics for WebSocket connections."""

    total_connections: int = 0
    total_disconnections: int = 0
    total_messages_sent: int = 0
    total_messages_received: int = 0
    current_connections: int = 0
    connections_by_tenant: dict[str, int] = field(default_factory=dict)


class WebSocketManager:
    """Manages WebSocket connections and event broadcasting.

    Thread-safe manager for WebSocket connections with Redis Pub/Sub integration.
    Designed for high performance with efficient data structures and lazy subscriptions.

    Architecture:
        tenant_id -> connection_id -> ConnectionInfo
        One Redis subscription task per tenant (shared by all connections)

    Example:
        manager = WebSocketManager(event_bus)
        await manager.start()

        # In WebSocket endpoint
        await manager.connect(websocket, tenant_id, subscription_id, user_id)
        try:
            await manager.handle_client(websocket, connection_id)
        finally:
            await manager.disconnect(connection_id)
    """

    def __init__(self, event_bus: EventBusProtocol | None = None) -> None:
        """Initialize the WebSocket manager.

        Args:
            event_bus: EventBus for Redis Pub/Sub integration (optional)
        """
        self._event_bus = event_bus
        self._started = False

        # Connection tracking: tenant_id -> connection_id -> ConnectionInfo
        self._connections: dict[str, dict[str, ConnectionInfo]] = {}

        # Connection ID to tenant mapping for O(1) lookup
        self._connection_to_tenant: dict[str, str] = {}

        # Redis subscription tasks per tenant
        self._subscription_tasks: dict[str, asyncio.Task[None]] = {}

        # Heartbeat task
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Statistics
        self._stats = WebSocketStats()

        # Lock for connection modifications
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the WebSocket manager.

        Starts the heartbeat task for connection health monitoring.
        """
        if self._started:
            return

        self._started = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop(), name="ws-heartbeat")
        logger.info("WebSocket manager started")

    async def stop(self) -> None:
        """Stop the WebSocket manager and close all connections."""
        if not self._started:
            return

        self._started = False

        # Cancel heartbeat task
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None

        # Cancel all subscription tasks
        for task in self._subscription_tasks.values():
            task.cancel()
        for task in self._subscription_tasks.values():
            with suppress(asyncio.CancelledError):
                await task
        self._subscription_tasks.clear()

        # Close all connections gracefully
        async with self._lock:
            for tenant_connections in self._connections.values():
                for conn_info in tenant_connections.values():
                    with suppress(Exception):
                        await conn_info.websocket.close(code=1001, reason="Server shutting down")
            self._connections.clear()
            self._connection_to_tenant.clear()

        logger.info("WebSocket manager stopped")

    async def connect(
        self,
        websocket: WebSocket,
        tenant_id: str,
        connection_id: str,
        user_id: str | None = None,
        subscription_id: str | None = None,
        patterns: list[str] | None = None,
        event_types: list[str] | None = None,
    ) -> ConnectionInfo:
        """Register a new WebSocket connection.

        Args:
            websocket: The WebSocket connection
            tenant_id: Tenant ID for event filtering
            connection_id: Unique connection identifier
            user_id: Optional user ID for logging
            subscription_id: Optional subscription ID for filtering
            patterns: Glob patterns for path filtering
            event_types: Event types to receive

        Returns:
            ConnectionInfo for the new connection
        """
        await websocket.accept()

        conn_info = ConnectionInfo(
            websocket=websocket,
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            user_id=user_id,
            patterns=patterns or [],
            event_types=event_types or [],
        )

        async with self._lock:
            # Add to connection tracking
            if tenant_id not in self._connections:
                self._connections[tenant_id] = {}
            self._connections[tenant_id][connection_id] = conn_info
            self._connection_to_tenant[connection_id] = tenant_id

            # Update stats
            self._stats.total_connections += 1
            self._stats.current_connections += 1
            self._stats.connections_by_tenant[tenant_id] = (
                self._stats.connections_by_tenant.get(tenant_id, 0) + 1
            )

            # Start Redis subscription for this tenant if needed
            if self._event_bus and tenant_id not in self._subscription_tasks and self._started:
                task = asyncio.create_task(
                    self._redis_subscription_loop(tenant_id),
                    name=f"ws-redis-{tenant_id}",
                )
                self._subscription_tasks[tenant_id] = task

        logger.info(
            f"WebSocket connected: connection_id={connection_id}, "
            f"tenant={tenant_id}, user={user_id}"
        )

        return conn_info

    async def disconnect(self, connection_id: str) -> None:
        """Remove a WebSocket connection.

        Args:
            connection_id: The connection ID to remove
        """
        async with self._lock:
            tenant_id = self._connection_to_tenant.pop(connection_id, None)
            if not tenant_id:
                return

            if tenant_id in self._connections:
                conn_info = self._connections[tenant_id].pop(connection_id, None)
                if conn_info:
                    self._stats.total_disconnections += 1
                    self._stats.current_connections -= 1
                    if tenant_id in self._stats.connections_by_tenant:
                        self._stats.connections_by_tenant[tenant_id] -= 1
                        if self._stats.connections_by_tenant[tenant_id] <= 0:
                            del self._stats.connections_by_tenant[tenant_id]

                # Clean up empty tenant
                if not self._connections[tenant_id]:
                    del self._connections[tenant_id]

                    # Cancel Redis subscription if no more connections for tenant
                    if tenant_id in self._subscription_tasks:
                        self._subscription_tasks[tenant_id].cancel()
                        del self._subscription_tasks[tenant_id]

        logger.info(f"WebSocket disconnected: connection_id={connection_id}")

    async def handle_client(self, websocket: WebSocket, connection_id: str) -> None:
        """Handle messages from a WebSocket client.

        This is the main loop for handling client messages. It processes
        ping/pong for heartbeat and any other client messages.

        Args:
            websocket: The WebSocket connection
            connection_id: The connection ID

        Raises:
            WebSocketDisconnect: When the client disconnects
        """
        try:
            while True:
                data = await websocket.receive_json()
                await self._handle_client_message(connection_id, data)
        except WebSocketDisconnect:
            raise
        except Exception as e:
            logger.warning(f"Error handling client message: {e}")
            raise WebSocketDisconnect(code=1011) from e

    async def _handle_client_message(self, connection_id: str, data: dict[str, Any]) -> None:
        """Process a message from a client.

        Args:
            connection_id: The connection ID
            data: The parsed JSON message
        """
        tenant_id = self._connection_to_tenant.get(connection_id)
        if not tenant_id:
            return

        conn_info = self._connections.get(tenant_id, {}).get(connection_id)
        if not conn_info:
            return

        conn_info.messages_received += 1
        self._stats.total_messages_received += 1

        msg_type = data.get("type", "")

        if msg_type == "pong":
            conn_info.last_pong = time.time()
            logger.debug(f"Received pong from {connection_id}")
        elif msg_type == "ping":
            # Client-initiated ping, respond with pong
            await self._send_to_connection(conn_info, {"type": "pong"})
        elif msg_type == "subscribe":
            # Update subscription filters
            if "patterns" in data:
                conn_info.patterns = data["patterns"]
            if "event_types" in data:
                conn_info.event_types = data["event_types"]
            await self._send_to_connection(
                conn_info, {"type": "subscribed", "patterns": conn_info.patterns}
            )
        else:
            logger.debug(f"Unknown message type from {connection_id}: {msg_type}")

    async def broadcast_to_tenant(self, tenant_id: str, event: FileEvent) -> int:
        """Broadcast an event to all connections for a tenant.

        Args:
            tenant_id: The tenant ID
            event: The file event to broadcast

        Returns:
            Number of connections that received the event
        """
        connections = self._connections.get(tenant_id, {})
        if not connections:
            return 0

        message = {
            "type": "event",
            "data": event.to_dict(),
        }

        sent_count = 0
        failed_connections: list[str] = []

        for conn_id, conn_info in connections.items():
            # Apply filters
            if not self._matches_filters(event, conn_info):
                continue

            try:
                await self._send_to_connection(conn_info, message)
                sent_count += 1
            except Exception as e:
                logger.warning(f"Failed to send to {conn_id}: {e}")
                failed_connections.append(conn_id)

        # Clean up failed connections
        for conn_id in failed_connections:
            await self.disconnect(conn_id)

        return sent_count

    def _matches_filters(self, event: FileEvent, conn_info: ConnectionInfo) -> bool:
        """Check if an event matches the connection's filters.

        Args:
            event: The file event
            conn_info: The connection info with filters

        Returns:
            True if the event matches the filters
        """
        # Check event type filter
        if conn_info.event_types:
            event_type = event.type.value if hasattr(event.type, "value") else str(event.type)
            if event_type not in conn_info.event_types:
                return False

        # Check path pattern filter
        if conn_info.patterns:
            matched = False
            for pattern in conn_info.patterns:
                if self._path_matches_pattern(event.path, pattern):
                    matched = True
                    break
            if not matched:
                return False

        return True

    def _path_matches_pattern(self, path: str, pattern: str) -> bool:
        """Check if a path matches a glob pattern.

        Supports:
        - * matches any characters except /
        - ** matches any characters including /
        - ? matches a single character

        Args:
            path: The file path to check
            pattern: The glob pattern

        Returns:
            True if the path matches the pattern
        """
        # Handle ** patterns by converting to regex
        if "**" in pattern:
            import re

            # Escape special regex chars except * and ?
            regex_pattern = ""
            i = 0
            while i < len(pattern):
                if pattern[i : i + 2] == "**":
                    regex_pattern += ".*"  # ** matches anything including /
                    i += 2
                    # Skip trailing / after **
                    if i < len(pattern) and pattern[i] == "/":
                        regex_pattern += "/?"
                        i += 1
                elif pattern[i] == "*":
                    regex_pattern += "[^/]*"  # * matches anything except /
                    i += 1
                elif pattern[i] == "?":
                    regex_pattern += "."  # ? matches single char
                    i += 1
                elif pattern[i] in r"\.[]{}()+^$|":
                    regex_pattern += "\\" + pattern[i]
                    i += 1
                else:
                    regex_pattern += pattern[i]
                    i += 1

            # Anchor the pattern
            regex_pattern = "^" + regex_pattern + "$"

            try:
                return bool(re.match(regex_pattern, path))
            except re.error:
                return False

        # Simple patterns without ** use fnmatch
        return fnmatch.fnmatch(path, pattern)

    async def _send_to_connection(self, conn_info: ConnectionInfo, message: dict[str, Any]) -> None:
        """Send a message to a specific connection.

        Args:
            conn_info: The connection info
            message: The message to send
        """
        await conn_info.websocket.send_json(message)
        conn_info.messages_sent += 1
        self._stats.total_messages_sent += 1

    async def _redis_subscription_loop(self, tenant_id: str) -> None:
        """Subscribe to Redis events for a tenant and broadcast to WebSocket clients.

        This creates a single Redis subscription per tenant, shared by all
        WebSocket connections for that tenant.

        Args:
            tenant_id: The tenant ID to subscribe to
        """
        if not self._event_bus:
            return

        logger.info(f"Starting Redis subscription for tenant {tenant_id}")

        try:
            async for event in self._event_bus.subscribe(tenant_id):
                if not self._started:
                    break

                # Broadcast to all WebSocket clients for this tenant
                sent = await self.broadcast_to_tenant(tenant_id, event)
                if sent > 0:
                    logger.debug(f"Broadcast {event.type} on {event.path} to {sent} clients")

        except asyncio.CancelledError:
            logger.debug(f"Redis subscription cancelled for tenant {tenant_id}")
            raise
        except Exception as e:
            logger.error(f"Redis subscription error for tenant {tenant_id}: {e}")

    async def _heartbeat_loop(self) -> None:
        """Send periodic pings to all connections and check for stale connections."""
        while self._started:
            try:
                await asyncio.sleep(PING_INTERVAL)

                if not self._started:
                    break

                now = time.time()
                stale_connections: list[str] = []

                # Check all connections
                for _tenant_id, connections in list(self._connections.items()):
                    for conn_id, conn_info in list(connections.items()):
                        # Check if connection is stale (no pong received)
                        time_since_pong = now - conn_info.last_pong
                        if time_since_pong > PING_INTERVAL + PING_TIMEOUT:
                            logger.warning(
                                f"Connection {conn_id} stale (no pong in {time_since_pong:.1f}s)"
                            )
                            stale_connections.append(conn_id)
                            continue

                        # Send ping
                        try:
                            await self._send_to_connection(conn_info, {"type": "ping"})
                        except Exception as e:
                            logger.warning(f"Failed to ping {conn_id}: {e}")
                            stale_connections.append(conn_id)

                # Disconnect stale connections
                for conn_id in stale_connections:
                    with suppress(Exception):
                        stale_tenant_id = self._connection_to_tenant.get(conn_id)
                        if stale_tenant_id:
                            stale_conn = self._connections.get(stale_tenant_id, {}).get(conn_id)
                            if stale_conn:
                                await stale_conn.websocket.close(
                                    code=1001, reason="Connection timeout"
                                )
                    await self.disconnect(conn_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat loop error: {e}")
                await asyncio.sleep(5.0)  # Back off on errors

    def get_stats(self) -> dict[str, Any]:
        """Get WebSocket manager statistics.

        Returns:
            Statistics dictionary for health endpoint
        """
        return {
            "total_connections": self._stats.total_connections,
            "total_disconnections": self._stats.total_disconnections,
            "current_connections": self._stats.current_connections,
            "total_messages_sent": self._stats.total_messages_sent,
            "total_messages_received": self._stats.total_messages_received,
            "connections_by_tenant": dict(self._stats.connections_by_tenant),
            "active_tenant_subscriptions": len(self._subscription_tasks),
        }

    def get_connection_count(self) -> int:
        """Get the current number of active connections.

        Returns:
            Number of active WebSocket connections
        """
        return self._stats.current_connections

    def get_connection_info(self, connection_id: str) -> ConnectionInfo | None:
        """Get information about a specific connection.

        Args:
            connection_id: The connection ID

        Returns:
            ConnectionInfo or None if not found
        """
        tenant_id = self._connection_to_tenant.get(connection_id)
        if not tenant_id:
            return None
        return self._connections.get(tenant_id, {}).get(connection_id)
