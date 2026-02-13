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
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.cache.dragonfly import DragonflyClient

logger = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Get current UTC time as a naive datetime (no timezone info).

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE columns don't store timezone info,
    so we need naive datetimes. This avoids the deprecated datetime.utcnow().
    """
    return datetime.now(UTC).replace(tzinfo=None)


class FileEventType(StrEnum):
    """Types of file system events."""

    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    FILE_RENAME = "file_rename"
    METADATA_CHANGE = "metadata_change"  # chmod, chown, truncate (Issue #1115)
    DIR_CREATE = "dir_create"
    DIR_DELETE = "dir_delete"
    # Issue #1129: Bidirectional sync events
    SYNC_TO_BACKEND_REQUESTED = "sync_to_backend_requested"
    SYNC_TO_BACKEND_COMPLETED = "sync_to_backend_completed"
    SYNC_TO_BACKEND_FAILED = "sync_to_backend_failed"
    CONFLICT_DETECTED = "conflict_detected"


@dataclass
class FileEvent:
    """Unified file system event for both Layer 1 (local) and Layer 2 (distributed).

    This is the single source of truth for file events across both layers:
    - Layer 1 (inotify/ReadDirectoryChangesW): Creates via from_file_change()
    - Layer 2 (Redis Pub/Sub): Creates directly with all fields

    Attributes:
        type: Type of event (file_write, file_delete, file_rename, etc.)
        path: Virtual path that changed
        zone_id: Zone that owns the file (None for Layer 1 local events)
        timestamp: When the event occurred (ISO format)
        event_id: Unique event ID for deduplication
        old_path: Previous path (for rename events only)
        size: File size in bytes (for write events)
        etag: Content hash (for write events)
        agent_id: Agent that performed the operation (optional)
        revision: Filesystem revision number for consistency tracking (Issue #1187)
    """

    type: FileEventType | str
    path: str
    zone_id: str | None = None  # None for Layer 1 (local) events
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    old_path: str | None = None
    size: int | None = None
    etag: str | None = None
    agent_id: str | None = None
    revision: int | None = None  # Issue #1187: For consistency tracking

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result: dict[str, Any] = {
            "type": self.type.value if isinstance(self.type, FileEventType) else self.type,
            "path": self.path,
            "timestamp": self.timestamp,
            "event_id": self.event_id,
        }
        # Optional fields - only include if set
        if self.zone_id is not None:
            result["zone_id"] = self.zone_id
        if self.old_path is not None:
            result["old_path"] = self.old_path
        if self.size is not None:
            result["size"] = self.size
        if self.etag is not None:
            result["etag"] = self.etag
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.revision is not None:
            result["revision"] = self.revision
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
            zone_id=data.get("zone_id"),  # Optional for Layer 1
            timestamp=data.get("timestamp", datetime.now(UTC).isoformat()),
            event_id=data.get("event_id", str(uuid.uuid4())),
            old_path=data.get("old_path"),
            size=data.get("size"),
            etag=data.get("etag"),
            agent_id=data.get("agent_id"),
            revision=data.get("revision"),  # Issue #1187
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
        zone_id: str | None = None,
    ) -> FileEvent:
        """Create FileEvent from Layer 1 FileChange.

        Maps ChangeType to FileEventType:
        - CREATED → FILE_WRITE (new file created)
        - MODIFIED → FILE_WRITE (file content changed)
        - DELETED → FILE_DELETE
        - RENAMED → FILE_RENAME

        Args:
            change: FileChange from file_watcher.py
            zone_id: Optional zone ID to associate

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
            zone_id=zone_id,
            old_path=getattr(change, "old_path", None),
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FileEvent):
            return NotImplemented
        return self.event_id == other.event_id

    def __hash__(self) -> int:
        return hash(self.event_id)

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

        # Glob pattern match — delegate to reactive_subscriptions for consistency
        if "*" in pattern or "?" in pattern:
            from nexus.core.reactive_subscriptions import path_matches_pattern

            if path_matches_pattern(self.path, pattern):
                return True
            if self.old_path and path_matches_pattern(self.old_path, pattern):
                return True

        return False


@dataclass
class AckableEvent:
    """Wrapper around FileEvent with acknowledgment semantics.

    Used by durable subscribers (e.g., NATS JetStream pull consumers) to
    explicitly acknowledge or reject event processing. For backends without
    native ack support (e.g., Redis pub/sub), ack/nack are no-ops.
    """

    event: FileEvent
    _ack_fn: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)
    _nack_fn: Callable[[float | None], Awaitable[None]] | None = field(default=None, repr=False)
    _in_progress_fn: Callable[[], Awaitable[None]] | None = field(default=None, repr=False)

    async def ack(self) -> None:
        """Acknowledge the event (prevent redelivery)."""
        if self._ack_fn:
            await self._ack_fn()

    async def nack(self, delay: float | None = None) -> None:
        """Negative acknowledge (trigger redelivery after optional delay)."""
        if self._nack_fn:
            await self._nack_fn(delay)

    async def in_progress(self) -> None:
        """Signal that processing is ongoing (extend ack deadline)."""
        if self._in_progress_fn:
            await self._in_progress_fn()


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
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_revision: Only return events with revision > this value.

        Returns:
            FileEvent if matched, None on timeout
        """
        ...

    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        ...

    def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a zone (async generator)."""
        ...

    def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,
        deliver_policy: str = "all",
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable consumer semantics.

        Args:
            zone_id: Zone ID to subscribe to
            consumer_name: Durable consumer name (survives reconnects)
            deliver_policy: Delivery policy ("all", "last", "new")

        Yields:
            AckableEvent objects with ack/nack support
        """
        ...


class EventBusBase(ABC):
    """Abstract base class for event bus implementations.

    Provides common functionality and enforces the interface contract.
    Subclasses must implement all abstract methods.

    Shared logic (node ID, checkpoints, startup sync) lives here so
    every backend gets it for free.
    """

    CHECKPOINT_KEY_PREFIX = "node_sync_checkpoint"

    def __init__(
        self,
        session_factory: Any | None = None,
        node_id: str | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._node_id = node_id or self._generate_node_id()

    @staticmethod
    def _generate_node_id() -> str:
        """Generate a unique node ID based on hostname and process."""
        import os
        import socket

        hostname = socket.gethostname()
        pid = os.getpid()
        return f"{hostname}-{pid}"

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
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_revision: Only return events with revision > this value (Issue #1187)

        Returns:
            FileEvent if matched, None on timeout
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the event bus is healthy."""
        pass

    @abstractmethod
    def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a zone (async generator).

        Use this for background listeners like cache invalidation.

        Args:
            zone_id: Zone ID to subscribe to

        Yields:
            FileEvent objects as they are received
        """
        pass

    @abstractmethod
    def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,
        deliver_policy: str = "all",
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable consumer semantics.

        Args:
            zone_id: Zone ID to subscribe to
            consumer_name: Durable consumer name (survives reconnects)
            deliver_policy: Delivery policy ("all", "last", "new")

        Yields:
            AckableEvent objects with ack/nack support
        """
        pass

    async def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics. Override in subclasses for more details."""
        return {
            "backend": self.__class__.__name__,
            "status": "running" if await self.health_check() else "stopped",
        }

    # =========================================================================
    # SSOT: Checkpoint & Startup Sync (shared across backends)
    # =========================================================================

    def _get_checkpoint_key(self) -> str:
        """Get the SystemSettings key for this node's checkpoint."""
        return f"{self.CHECKPOINT_KEY_PREFIX}:{self._node_id}"

    async def _get_checkpoint(self) -> datetime | None:
        """Get the last sync checkpoint from the database.

        Runs synchronous SQLAlchemy in a thread executor to avoid blocking
        the event loop. Handles missing system_settings table gracefully
        (e.g. SQLite embedded mode).
        """
        if not self._session_factory:
            return None

        session_factory = self._session_factory  # bind for mypy narrowing

        def _query() -> datetime | None:
            from sqlalchemy import select
            from sqlalchemy.exc import OperationalError, ProgrammingError

            from nexus.storage.models import SystemSettingsModel

            try:
                with session_factory() as session:
                    stmt = select(SystemSettingsModel).where(
                        SystemSettingsModel.key == self._get_checkpoint_key()
                    )
                    setting = session.execute(stmt).scalar_one_or_none()

                    if setting:
                        return datetime.fromisoformat(setting.value)
                    return None
            except (OperationalError, ProgrammingError):
                # Table may not exist in SQLite embedded mode
                return None

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _query)

    async def _update_checkpoint(self, timestamp: datetime) -> None:
        """Update the sync checkpoint in the database.

        Runs synchronous SQLAlchemy in a thread executor to avoid blocking
        the event loop.
        """
        if not self._session_factory:
            return

        session_factory = self._session_factory  # bind for mypy narrowing

        def _update() -> None:
            from sqlalchemy import select

            from nexus.storage.models import SystemSettingsModel

            with session_factory() as session:
                key = self._get_checkpoint_key()
                stmt = select(SystemSettingsModel).where(SystemSettingsModel.key == key)
                setting = session.execute(stmt).scalar_one_or_none()

                if setting:
                    setting.value = timestamp.isoformat()
                else:
                    setting = SystemSettingsModel(
                        key=key,
                        value=timestamp.isoformat(),
                        description=f"Event sync checkpoint for node {self._node_id}",
                    )
                    session.add(setting)

                session.commit()

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _update)

    async def startup_sync(
        self,
        event_handler: Callable[[FileEvent], Awaitable[None]] | None = None,
        default_lookback_hours: int = 1,
    ) -> int:
        """Sync missed events from PG SSOT on startup.

        This method should be called after start() to reconcile any events
        that were missed while this node was down.

        Args:
            event_handler: Async callback to handle each missed event.
                          If None, events are logged but not processed.
            default_lookback_hours: If no checkpoint exists, look back this many hours.

        Returns:
            Number of events synced.
        """
        if not self._session_factory:
            logger.warning("startup_sync skipped: no session_factory configured")
            return 0

        from sqlalchemy import select

        from nexus.storage.models import OperationLogModel

        # Get last checkpoint
        checkpoint = await self._get_checkpoint()
        if checkpoint is None:
            # First run: use default lookback
            # Use naive datetime to match database (TIMESTAMP WITHOUT TIME ZONE)
            checkpoint = _utcnow_naive() - timedelta(hours=default_lookback_hours)
            logger.info(f"No checkpoint found, using default lookback: {default_lookback_hours}h")
        elif checkpoint.tzinfo is not None:
            # Convert to naive if checkpoint has timezone info
            checkpoint = checkpoint.replace(tzinfo=None)

        logger.info(f"Starting sync from checkpoint: {checkpoint.isoformat()}")

        # Query operations since checkpoint
        with self._session_factory() as session:
            stmt = (
                select(OperationLogModel)
                .where(OperationLogModel.created_at > checkpoint)
                .where(OperationLogModel.status == "success")
                .order_by(OperationLogModel.created_at)
            )
            operations = session.execute(stmt).scalars().all()

            if not operations:
                logger.info("No missed events to sync")
                await self._update_checkpoint(_utcnow_naive())
                return 0

            logger.info(f"Found {len(operations)} missed events to sync")

            # Process each operation
            synced_count = 0
            latest_timestamp = checkpoint

            for op in operations:
                # Convert OperationLogModel to FileEvent
                event = FileEvent(
                    type=self._operation_type_to_event_type(op.operation_type),
                    path=op.path,
                    zone_id=op.zone_id,
                    timestamp=op.created_at.isoformat(),
                    old_path=op.new_path,  # new_path in operation_log is old_path for rename
                )

                if event_handler:
                    try:
                        await event_handler(event)
                        synced_count += 1
                    except Exception as e:
                        logger.error(f"Failed to handle event {event.event_id}: {e}")
                else:
                    logger.debug(f"Synced event: {event.type} on {event.path}")
                    synced_count += 1

                if op.created_at > latest_timestamp:
                    latest_timestamp = op.created_at

            # Update checkpoint
            await self._update_checkpoint(latest_timestamp)
            logger.info(f"Startup sync complete: {synced_count} events processed")

            return synced_count

    @staticmethod
    def _operation_type_to_event_type(op_type: str) -> FileEventType | str:
        """Convert operation_log type to FileEventType."""
        mapping = {
            "write": FileEventType.FILE_WRITE,
            "delete": FileEventType.FILE_DELETE,
            "rename": FileEventType.FILE_RENAME,
            "mkdir": FileEventType.DIR_CREATE,
            "rmdir": FileEventType.DIR_DELETE,
        }
        return mapping.get(op_type, op_type)


# =============================================================================
# Redis Pub/Sub Implementation
# =============================================================================


class RedisEventBus(EventBusBase):
    """Redis Pub/Sub implementation of the event bus with PG SSOT.

    Uses per-zone channels for efficient event routing.
    Channel format: nexus:events:{zone_id}

    SSOT Architecture:
        - PostgreSQL (operation_log) is the source of truth
        - Redis Pub/Sub is best-effort notification
        - Startup sync reconciles missed events from PG

    Example:
        >>> bus = RedisEventBus(redis_client, session_factory=SessionLocal)
        >>> await bus.start()
        >>> await bus.startup_sync()  # Sync missed events from PG
        >>>
        >>> # Publish event
        >>> event = FileEvent(
        ...     type=FileEventType.FILE_WRITE,
        ...     path="/inbox/test.txt",
        ...     zone_id="default",
        ... )
        >>> await bus.publish(event)
    """

    CHANNEL_PREFIX = "nexus:events"

    def __init__(
        self,
        redis_client: DragonflyClient,
        session_factory: Any | None = None,
        node_id: str | None = None,
    ):
        """Initialize RedisEventBus.

        Args:
            redis_client: DragonflyClient instance for Redis connection
            session_factory: SQLAlchemy SessionLocal for PG SSOT (optional)
            node_id: Unique node identifier for checkpoint tracking (auto-generated if None)
        """
        super().__init__(session_factory=session_factory, node_id=node_id)
        self._redis = redis_client
        self._pubsub: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    def _channel_name(self, zone_id: str) -> str:
        """Get Redis channel name for a zone."""
        return f"{self.CHANNEL_PREFIX}:{zone_id}"

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
        """Publish an event to the zone's channel."""
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        zone_id = event.zone_id or "default"
        channel = self._channel_name(zone_id)
        message = event.to_json()

        try:
            num_subscribers: int = await self._redis.client.publish(channel, message)
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
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_revision: Only return events with revision > this value (Issue #1187).
                           Events with revision <= since_revision are skipped.

        Returns:
            FileEvent if matched, None on timeout
        """
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        channel = self._channel_name(zone_id)
        pubsub = self._redis.client.pubsub()

        try:
            await pubsub.subscribe(channel)
            logger.debug(f"Subscribed to {channel} for pattern {path_pattern}")

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
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
                        # Issue #1187: Filter by revision if specified
                        if since_revision is not None and (
                            event.revision is None or event.revision <= since_revision
                        ):
                            logger.debug(
                                f"Skipping event {event.type} on {event.path}: "
                                f"revision {event.revision} <= since_revision {since_revision}"
                            )
                            continue
                        logger.debug(f"Matched event: {event.type} on {event.path}")
                        return event

                except TimeoutError:
                    if loop.time() >= deadline:
                        return None
                    continue

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    async def subscribe(
        self,
        zone_id: str,
    ) -> AsyncIterator[FileEvent]:
        """Subscribe to all events for a zone.

        This is an async generator that yields FileEvent objects as they are received.
        Use this for background listeners like cache invalidation.

        Args:
            zone_id: Zone ID to subscribe to

        Yields:
            FileEvent objects as they are received

        Example:
            >>> async for event in bus.subscribe("zone1"):
            ...     print(f"Received {event.type} on {event.path}")
            ...     # Handle event (e.g., invalidate cache)
        """
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        channel = self._channel_name(zone_id)
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

    async def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,  # noqa: ARG002
        deliver_policy: str = "all",  # noqa: ARG002
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable semantics (Redis compat wrapper).

        Redis pub/sub has no native durability, so this wraps subscribe()
        and yields AckableEvents with no-op ack/nack callbacks.
        """
        async for event in self.subscribe(zone_id):
            yield AckableEvent(event=event)

    async def get_stats(self) -> dict[str, Any]:
        """Get event bus statistics."""
        redis_info = await self._redis.get_info()
        checkpoint = await self._get_checkpoint()
        return {
            "backend": "redis_pubsub",
            "status": "running" if self._started else "stopped",
            "channel_prefix": self.CHANNEL_PREFIX,
            "redis_status": redis_info.get("status", "unknown"),
            "node_id": self._node_id,
            "last_checkpoint": checkpoint.isoformat() if checkpoint else None,
            "ssot_enabled": self._session_factory is not None,
        }


# Backward compatibility alias
GlobalEventBus = RedisEventBus


# =============================================================================
# Factory and Singleton Management
# =============================================================================


def create_event_bus(
    backend: str = "redis",
    redis_client: DragonflyClient | None = None,
    nats_url: str | None = None,
    **kwargs: Any,
) -> EventBusBase:
    """Factory function to create an event bus instance.

    Args:
        backend: Backend type ("redis" or "nats")
        redis_client: DragonflyClient for Redis backend
        nats_url: NATS server URL for NATS backend
        **kwargs: Additional backend-specific arguments (session_factory, node_id, etc.)

    Returns:
        EventBusBase implementation

    Raises:
        ValueError: If backend is not supported or required arguments are missing
    """
    if backend == "nats":
        if nats_url is None:
            raise ValueError("nats_url is required for NATS backend")
        from nexus.core.event_bus_nats import NatsEventBus

        return NatsEventBus(nats_url=nats_url, **kwargs)

    if backend == "redis":
        if redis_client is None:
            raise ValueError("redis_client is required for Redis backend")
        return RedisEventBus(redis_client, **kwargs)

    raise ValueError(f"Unsupported event bus backend: {backend}")


# Singleton instance for shared use
_global_event_bus: EventBusBase | None = None


def get_global_event_bus() -> EventBusBase | None:
    """Get the global event bus instance.

    Returns:
        EventBusBase instance if initialized, None otherwise
    """
    return _global_event_bus


def require_global_event_bus() -> EventBusBase:
    """Get the global event bus, raising if not initialized.

    Returns:
        The global EventBusBase instance.

    Raises:
        RuntimeError: If no global event bus has been set.
    """
    bus = _global_event_bus
    if bus is None:
        raise RuntimeError("Global event bus not initialized. Call set_global_event_bus() first.")
    return bus


def set_global_event_bus(bus: EventBusBase | None) -> None:
    """Set the global event bus instance.

    Args:
        bus: EventBusBase instance to set as global, or None to clear
    """
    global _global_event_bus
    _global_event_bus = bus
