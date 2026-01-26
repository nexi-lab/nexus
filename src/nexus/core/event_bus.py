"""Distributed event bus interfaces and Redis implementation.

This module provides the event bus abstraction and Redis Pub/Sub implementation
for distributed file system events across multiple Nexus nodes. It's part of
Block 2 (Issue #1106) for the distributed event system.

Architecture:
- EventBusProtocol: Abstract interface for event bus implementations
- GlobalEventBus: Redis Pub/Sub implementation (default)
- Future: etcd, ZooKeeper, P2P implementations (Issue #1141)

Multi-Region Support:
- GlobalEventBus connects to a single Redis URL (NEXUS_REDIS_URL)
- Multi-region event sync depends on Redis deployment configuration
- See distributed_lock.py for similar patterns with locks

Layer 2 in the dual-track event system:
- Layer 1: Same-box local watching (inotify/ReadDirectoryChangesW) - Block 1
- Layer 2: Distributed event bus (this module) - Block 2
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


class FileEventType(str, Enum):
    """Types of file system events."""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"
    DIR_CREATE = "dir_create"
    DIR_DELETE = "dir_delete"


@dataclass
class FileEvent:
    """Unified file system event for both Layer 1 (local) and Layer 2 (distributed).

    This is the single source of truth for file events across both layers:
    - Layer 1 (inotify/ReadDirectoryChangesW): Creates via from_file_change()
    - Layer 2 (Redis Pub/Sub): Creates directly with all fields

    Attributes:
        type: Type of event (file_write, file_delete, file_rename, etc.)
        path: Virtual path that changed
        tenant_id: Tenant that owns the file (None for Layer 1 local events)
        timestamp: When the event occurred (ISO format)
        event_id: Unique event ID for deduplication
        old_path: Previous path (for rename events only)
        size: File size in bytes (for write events)
        etag: Content hash (for write events)
        agent_id: Agent that performed the operation (optional)
    """

    type: FileEventType | str
    path: str
    tenant_id: str | None = None  # None for Layer 1 (local) events
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    old_path: str | None = None
    size: int | None = None
    etag: str | None = None
    agent_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "type": self.type.value if isinstance(self.type, FileEventType) else self.type,
            "path": self.path,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
        }
        # Optional fields - only include if set
        if self.tenant_id is not None:
            result["tenant_id"] = self.tenant_id
        if self.old_path is not None:
            result["old_path"] = self.old_path
        if self.size is not None:
            result["size"] = self.size
        if self.etag is not None:
            result["etag"] = self.etag
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        return result

    def to_json(self) -> str:
        """Serialize to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FileEvent:
        """Create FileEvent from dictionary."""
        return cls(
            type=data["type"],
            path=data["path"],
            tenant_id=data.get("tenant_id"),  # Optional for Layer 1
            timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            event_id=data.get("event_id", str(uuid.uuid4())),
            old_path=data.get("old_path"),
            size=data.get("size"),
            etag=data.get("etag"),
            agent_id=data.get("agent_id"),
        )

    @classmethod
    def from_json(cls, json_str: str | bytes) -> FileEvent:
        """Deserialize from JSON string."""
        if isinstance(json_str, bytes):
            json_str = json_str.decode("utf-8")
        return cls.from_dict(json.loads(json_str))

    @classmethod
    def from_file_change(
        cls,
        change: Any,  # FileChange from file_watcher.py (avoid circular import)
        tenant_id: str | None = None,
    ) -> FileEvent:
        """Create FileEvent from Layer 1 FileChange.

        Maps ChangeType to FileEventType:
        - CREATED → FILE_WRITE (new file created)
        - MODIFIED → FILE_WRITE (file content changed)
        - DELETED → FILE_DELETE
        - RENAMED → FILE_RENAME

        Args:
            change: FileChange from file_watcher.py
            tenant_id: Optional tenant ID to associate

        Returns:
            FileEvent with unified format
        """
        # Map ChangeType string values to FileEventType
        change_type = change.type.value if hasattr(change.type, "value") else change.type

        type_mapping = {
            "created": FileEventType.FILE_WRITE,
            "modified": FileEventType.FILE_WRITE,
            "deleted": FileEventType.FILE_DELETE,
            "renamed": FileEventType.FILE_RENAME,
        }

        event_type = type_mapping.get(change_type, change_type)

        return cls(
            type=event_type,
            path=change.path,
            tenant_id=tenant_id,
            old_path=getattr(change, "old_path", None),
        )

    def matches_path_pattern(self, pattern: str) -> bool:
        """Check if this event matches a path pattern.

        Supports:
        - Exact match: "/inbox/file.txt"
        - Directory match: "/inbox/" (matches all files in /inbox/)
        - Glob patterns: "/inbox/*.txt", "/inbox/**"

        Args:
            pattern: Path pattern to match against

        Returns:
            True if event path matches the pattern
        """
        # Exact match
        if self.path == pattern:
            return True

        # Directory match - pattern ends with / OR pattern is a directory path
        # Handle both "/inbox/" and "/inbox" as directory patterns
        if pattern.endswith("/"):
            if self.path.startswith(pattern):
                return True
            if self.path == pattern.rstrip("/"):
                return True
        else:
            # Pattern without trailing slash - treat as directory if path is under it
            # e.g., pattern "/inbox" should match path "/inbox/test.txt"
            if self.path.startswith(pattern + "/"):
                return True

        # For rename events, also check old_path
        if self.old_path:
            if self.old_path == pattern:
                return True
            if pattern.endswith("/") and self.old_path.startswith(pattern):
                return True
            if not pattern.endswith("/") and self.old_path.startswith(pattern + "/"):
                return True

        # Glob pattern match
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(self.path, pattern):
                return True
            if self.old_path and fnmatch.fnmatch(self.old_path, pattern):
                return True

        return False


# =============================================================================
# Abstract Interface (Protocol)
# =============================================================================


@runtime_checkable
class EventBusProtocol(Protocol):
    """Protocol defining the event bus interface.

    This protocol allows different backend implementations:
    - Redis Pub/Sub (default, implemented as GlobalEventBus)
    - etcd watch (future)
    - ZooKeeper watchers (future)
    - P2P gossip protocol (future)

    All implementations must provide these async methods.
    """

    async def start(self) -> None:
        """Start the event bus. Must be called before publish/subscribe."""
        ...

    async def stop(self) -> None:
        """Stop the event bus and clean up resources."""
        ...

    async def publish(self, event: FileEvent) -> int:
        """Publish an event.

        Args:
            event: FileEvent to publish

        Returns:
            Number of subscribers that received the event
        """
        ...

    async def wait_for_event(
        self,
        tenant_id: str,
        path_pattern: str,
        timeout: float = 30.0,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            tenant_id: Tenant ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds

        Returns:
            FileEvent if matched, None on timeout
        """
        ...

    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        ...

    def subscribe(self, tenant_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a tenant (async generator)."""
        ...


class EventBusBase(ABC):
    """Abstract base class for event bus implementations.

    Provides common functionality and enforces the interface contract.
    Subclasses must implement all abstract methods.
    """

    @abstractmethod
    async def start(self) -> None:
        """Start the event bus."""
        pass

    @abstractmethod
    async def stop(self) -> None:
        """Stop the event bus."""
        pass

    @abstractmethod
    async def publish(self, event: FileEvent) -> int:
        """Publish an event."""
        pass

    @abstractmethod
    async def wait_for_event(
        self,
        tenant_id: str,
        path_pattern: str,
        timeout: float = 30.0,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern."""
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        pass

    @abstractmethod
    def subscribe(self, tenant_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a tenant (async generator).

        Use this for background listeners like cache invalidation.

        Args:
            tenant_id: Tenant ID to subscribe to

        Yields:
            FileEvent objects as they are received
        """
        pass

    async def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics. Override in subclasses for more details."""
        return {
            "backend": self.__class__.__name__,
            "status": "running" if await self.health_check() else "stopped",
        }


# =============================================================================
# Redis Pub/Sub Implementation
# =============================================================================


class RedisEventBus(EventBusBase):
    """Redis Pub/Sub implementation of the event bus.

    Uses per-tenant channels for efficient event routing.
    Channel format: nexus:events:{tenant_id}

    Example:
        >>> bus = RedisEventBus(redis_client)
        >>> await bus.start()
        >>>
        >>> # Publish event
        >>> event = FileEvent(
        ...     type=FileEventType.FILE_WRITE,
        ...     path="/inbox/test.txt",
        ...     tenant_id="default",
        ... )
        >>> await bus.publish(event)
        >>>
        >>> # Wait for events
        >>> event = await bus.wait_for_event("default", "/inbox/", timeout=30.0)
    """

    CHANNEL_PREFIX = "nexus:events"

    def __init__(self, redis_client: DragonflyClient):
        """Initialize RedisEventBus.

        Args:
            redis_client: DragonflyClient instance for Redis connection
        """
        self._redis = redis_client
        self._pubsub: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    def _channel_name(self, tenant_id: str) -> str:
        """Get Redis channel name for a tenant."""
        return f"{self.CHANNEL_PREFIX}:{tenant_id}"

    async def start(self) -> None:
        """Start the event bus listener."""
        if self._started:
            return

        async with self._lock:
            if self._started:
                return

            self._pubsub = self._redis.client.pubsub()
            self._started = True
            logger.info("RedisEventBus started")

    async def stop(self) -> None:
        """Stop the event bus listener and clean up."""
        if not self._started:
            return

        async with self._lock:
            if not self._started:
                return

            if self._pubsub:
                await self._pubsub.aclose()
                self._pubsub = None

            self._started = False
            logger.info("RedisEventBus stopped")

    async def publish(self, event: FileEvent) -> int:
        """Publish an event to the tenant's channel."""
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        channel = self._channel_name(event.tenant_id)
        message = event.to_json()

        try:
            num_subscribers = await self._redis.client.publish(channel, message)
            logger.debug(
                f"Published {event.type} event for {event.path} to {channel} "
                f"({num_subscribers} subscribers)"
            )
            return num_subscribers
        except Exception as e:
            logger.error(f"Failed to publish event: {e}")
            raise

    async def wait_for_event(
        self,
        tenant_id: str,
        path_pattern: str,
        timeout: float = 30.0,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern."""
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        channel = self._channel_name(tenant_id)
        pubsub = self._redis.client.pubsub()

        try:
            await pubsub.subscribe(channel)
            logger.debug(f"Subscribed to {channel} for pattern {path_pattern}")

            deadline = asyncio.get_event_loop().time() + timeout

            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    logger.debug(f"Timeout waiting for event on {channel}")
                    return None

                try:
                    message = await asyncio.wait_for(
                        pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0),
                        timeout=min(remaining, 1.0),
                    )

                    if message is None:
                        continue

                    if message["type"] != "message":
                        continue

                    try:
                        event = FileEvent.from_json(message["data"])
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Invalid event message: {e}")
                        continue

                    if event.matches_path_pattern(path_pattern):
                        logger.debug(f"Matched event: {event.type} on {event.path}")
                        return event

                except TimeoutError:
                    if asyncio.get_event_loop().time() >= deadline:
                        return None
                    continue

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def subscribe(
        self,
        tenant_id: str,
    ) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a tenant.

        This is an async generator that yields FileEvent objects as they are received.
        Use this for background listeners like cache invalidation.

        Args:
            tenant_id: Tenant ID to subscribe to

        Yields:
            FileEvent objects as they are received

        Example:
            >>> async for event in bus.subscribe("tenant1"):
            ...     print(f"Received {event.type} on {event.path}")
            ...     # Handle event (e.g., invalidate cache)
        """
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        channel = self._channel_name(tenant_id)
        pubsub = self._redis.client.pubsub()

        try:
            await pubsub.subscribe(channel)
            logger.debug(f"Subscribed to {channel} for cache invalidation")

            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=1.0,
                    )

                    if message is None:
                        # Yield control to allow cancellation
                        await asyncio.sleep(0)
                        continue

                    if message["type"] != "message":
                        continue

                    try:
                        event = FileEvent.from_json(message["data"])
                        yield event
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Invalid event message: {e}")
                        continue

                except asyncio.CancelledError:
                    logger.debug(f"Subscription to {channel} cancelled")
                    raise
                except Exception as e:
                    logger.warning(f"Error receiving message: {e}")
                    await asyncio.sleep(1.0)  # Back off on errors
                    continue

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        if not self._started:
            return False

        try:
            return await self._redis.health_check()
        except Exception as e:
            logger.warning(f"Event bus health check failed: {e}")
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics."""
        redis_info = await self._redis.get_info()
        return {
            "backend": "redis_pubsub",
            "status": "running" if self._started else "stopped",
            "channel_prefix": self.CHANNEL_PREFIX,
            "redis_status": redis_info.get("status", "unknown"),
        }


# Backward compatibility alias
GlobalEventBus = RedisEventBus


# =============================================================================
# Factory and Singleton Management
# =============================================================================


def create_event_bus(
    backend: str = "redis",
    redis_client: DragonflyClient | None = None,
    **kwargs: Any,  # noqa: ARG001 - Reserved for future backends
) -> EventBusBase:
    """Factory function to create an event bus instance.

    Args:
        backend: Backend type ("redis", future: "etcd", "zookeeper", "p2p")
        redis_client: DragonflyClient for Redis backend
        **kwargs: Additional backend-specific arguments

    Returns:
        EventBusBase implementation

    Raises:
        ValueError: If backend is not supported
        ValueError: If required arguments are missing
    """
    if backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for Redis backend")
        return RedisEventBus(redis_client)

    # Future backends
    # elif backend == "etcd":
    #     return EtcdEventBus(...)
    # elif backend == "zookeeper":
    #     return ZooKeeperEventBus(...)
    # elif backend == "p2p":
    #     return P2PEventBus(...)

    raise ValueError(f"Unsupported event bus backend: {backend}")


# Singleton instance for shared use
_global_event_bus: EventBusBase | None = None


def get_global_event_bus() -> EventBusBase | None:
    """Get the global event bus instance.

    Returns:
        EventBusBase instance if initialized, None otherwise
    """
    return _global_event_bus


def set_global_event_bus(bus: EventBusBase | None) -> None:
    """Set the global event bus instance.

    Args:
        bus: EventBusBase instance to set as global, or None to clear
    """
    global _global_event_bus
    _global_event_bus = bus
