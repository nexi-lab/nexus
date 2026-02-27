"""RedisEventBus — Redis Pub/Sub implementation of EventBusBase."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from nexus.core.file_events import FileEvent
from nexus.services.event_bus.base import EventBusBase
from nexus.services.event_bus.protocol import AckableEvent, PubSubClientProtocol

logger = logging.getLogger(__name__)


class RedisEventBus(EventBusBase):
    """Redis Pub/Sub implementation of the event bus with PG SSOT.

    Uses per-zone channels for efficient event routing.
    Channel format: nexus:events:{zone_id}

    SSOT Architecture:
        - PostgreSQL (operation_log) is the source of truth
        - Redis Pub/Sub is best-effort notification
        - Startup sync reconciles missed events from PG

    Optional WAL integration (Issue #1397):
        When an ``event_log`` is provided, every published event is
        durably persisted *before* being broadcast via Redis Pub/Sub.
        This gives subscribers a reliable replay log for catch-up.

    Example:
        >>> bus = RedisEventBus(redis_client, session_factory=SessionLocal)
        >>> await bus.start()
        >>> await bus.startup_sync()  # Sync missed events from PG
        >>>
        >>> # Publish event
        >>> event = FileEvent(
        ...     type=FileEventType.FILE_WRITE,
        ...     path="/inbox/test.txt",
        ...     zone_id="root",
        ... )
        >>> await bus.publish(event)
    """

    CHANNEL_PREFIX = "nexus:events"

    def __init__(
        self,
        redis_client: PubSubClientProtocol,
        session_factory: Any | None = None,
        node_id: str | None = None,
        event_log: Any | None = None,
    ):
        """Initialize RedisEventBus.

        Args:
            redis_client: PubSubClientProtocol provider (e.g., DragonflyClient)
            session_factory: SQLAlchemy SessionLocal for PG SSOT (optional)
            node_id: Unique node identifier for checkpoint tracking (auto-generated if None)
            event_log: Optional EventLogProtocol for durable WAL persistence (Issue #1397)
        """
        super().__init__(session_factory=session_factory, node_id=node_id)
        self._redis = redis_client
        self._event_log = event_log
        self._pubsub: Any = None
        self._started = False
        self._lock = asyncio.Lock()

    def set_event_log(self, event_log: Any) -> None:
        """Wire an event log for WAL-first durability (Issue #1397).

        Called during server startup after the event log is initialized,
        since the event bus may be constructed before the event log is available.
        """
        self._event_log = event_log

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
        """Publish an event to the zone's channel.

        If an event_log is configured (Issue #1397), the event is durably
        persisted to the WAL *before* being broadcast via Redis Pub/Sub.
        """
        if not self._started:
            raise RuntimeError("RedisEventBus not started. Call start() first.")

        # WAL-first: persist before fan-out (Issue #1397)
        if self._event_log is not None:
            try:
                await self._event_log.append(event)
            except Exception as e:
                logger.error(f"Event log append failed (event still published): {e}")

        zone_id = event.zone_id or "root"
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
