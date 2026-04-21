"""RedisEventBus — Redis Pub/Sub implementation of EventBusBase."""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from cachetools import TTLCache

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.services.event_bus.base import EventBusBase
from nexus.services.event_bus.decorators import requires_started
from nexus.services.event_bus.protocol import AckableEvent, PubSubClientProtocol
from nexus.services.event_bus.types import FileEvent

if TYPE_CHECKING:
    from nexus.contracts.auth_store_protocols import SystemSettingsStoreProtocol
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class RedisEventBus(EventBusBase):
    """Redis Pub/Sub implementation of the event bus with PG SSOT.

    Uses per-zone channels for efficient event routing.
    Channel format: nexus:events:{zone_id}

    SSOT Architecture:
        - PostgreSQL (operation_log) is the source of truth
        - Redis Pub/Sub is best-effort notification
        - Startup sync reconciles missed events from PG

    Example:
        >>> bus = RedisEventBus(redis_client, record_store=record_store)
        >>> await bus.start()
        >>> await bus.startup_sync()  # Sync missed events from PG
        >>>
        >>> # Publish event
        >>> event = FileEvent(
        ...     type=FileEventType.FILE_WRITE,
        ...     path="/inbox/test.txt",
        ...     zone_id=ROOT_ZONE_ID,
        ... )
        >>> await bus.publish(event)
    """

    CHANNEL_PREFIX = "nexus:events"

    def __init__(
        self,
        redis_client: PubSubClientProtocol,
        record_store: "RecordStoreABC | None" = None,
        node_id: str | None = None,
        settings_store: "SystemSettingsStoreProtocol | None" = None,
    ):
        """Initialize RedisEventBus.

        Args:
            redis_client: PubSubClientProtocol provider (e.g., DragonflyClient)
            record_store: RecordStoreABC for PG SSOT (optional)
            node_id: Unique node identifier for checkpoint tracking (auto-generated if None)
            settings_store: SystemSettingsStoreProtocol for checkpoint persistence (optional)
        """
        super().__init__(record_store=record_store, node_id=node_id, settings_store=settings_store)
        self._redis = redis_client
        self._pubsub: Any = None

        # Event deduplication cache (5s TTL) - prevents retry storms
        self._dedup_cache: TTLCache[str, bool] = TTLCache(maxsize=10000, ttl=5.0)

    def _channel_name(self, zone_id: str) -> str:
        """Get Redis channel name for a zone."""
        return f"{self.CHANNEL_PREFIX}:{zone_id}"

    async def _do_start(self) -> None:
        """Redis-specific startup: create pubsub."""
        self._pubsub = self._redis.client.pubsub()

    async def _do_stop(self) -> None:
        """Redis-specific shutdown: close pubsub."""
        if self._pubsub:
            await self._pubsub.aclose()
            self._pubsub = None

    @requires_started
    async def publish(self, event: FileEvent) -> int:
        """Publish an event to the zone's channel."""
        # Check deduplication cache
        if event.event_id in self._dedup_cache:
            logger.debug(f"Duplicate event {event.event_id}, skipping")
            return 0  # Already published

        zone_id = event.zone_id or ROOT_ZONE_ID
        channel = self._channel_name(zone_id)

        # Explicit serialization error handling
        try:
            message = event.to_json()
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"Event serialization failed: {e}. "
                f"Event: type={event.type}, path={event.path}, event_id={event.event_id}"
            ) from e

        try:
            num_subscribers: int = await self._redis.client.publish(channel, message)
            logger.debug(
                f"Published {event.type} event for {event.path} to {channel} "
                f"({num_subscribers} subscribers)"
            )

            # Mark as published
            self._dedup_cache[event.event_id] = True

            return num_subscribers
        except Exception as e:
            logger.error(f"Failed to publish event to Redis: {e}")
            raise

    @requires_started
    async def publish_batch(self, events: list[FileEvent]) -> list[int]:
        """Publish batch of events using Redis pipeline (single RTT).

        Args:
            events: List of FileEvent objects to publish

        Returns:
            List of subscriber counts per event (50x faster than sequential publish)
        """
        if not events:
            return []

        # Filter out duplicates
        unique_events = [e for e in events if e.event_id not in self._dedup_cache]

        if not unique_events:
            logger.debug(f"All {len(events)} events were duplicates, skipping")
            return [0] * len(events)

        if len(unique_events) < len(events):
            logger.debug(f"Filtered {len(events) - len(unique_events)} duplicate events")

        # Use Redis pipeline for single RTT
        pipeline = self._redis.client.pipeline()

        for event in unique_events:
            zone_id = event.zone_id or ROOT_ZONE_ID
            channel = self._channel_name(zone_id)

            try:
                message = event.to_json()
            except (TypeError, ValueError) as e:
                logger.error(f"Event serialization failed, skipping: {e}. Event: {event!r}")
                continue

            pipeline.publish(channel, message)

        try:
            results: list[int] = await pipeline.execute()
            logger.debug(f"Published batch of {len(unique_events)} events to Redis")

            # Mark all as published
            for event in unique_events:
                self._dedup_cache[event.event_id] = True

            return results
        except Exception as e:
            logger.error(f"Failed to publish batch to Redis: {e}")
            raise

    @requires_started
    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> FileEvent | None:
        """Wait for an event matching the path pattern.

        Args:
            zone_id: Zone ID to subscribe to
            path_pattern: Path pattern to match
            timeout: Maximum time to wait in seconds
            since_version: If set, skip events with version <= this value

        Returns:
            FileEvent if matched, None on timeout
        """
        try:
            return await self._wait_for_event_impl(
                zone_id,
                path_pattern,
                timeout,
                use_fresh_connection=False,
                since_version=since_version,
            )
        except RuntimeError as exc:
            if "attached to a different loop" in str(exc):
                logger.debug("Pubsub event-loop mismatch; retrying with fresh connection")
                return await self._wait_for_event_impl(
                    zone_id,
                    path_pattern,
                    timeout,
                    use_fresh_connection=True,
                    since_version=since_version,
                )
            raise

    async def _wait_for_event_impl(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float,
        *,
        use_fresh_connection: bool = False,
        since_version: int | None = None,
    ) -> FileEvent | None:
        """Internal implementation for wait_for_event."""
        channel = self._channel_name(zone_id)
        fresh_client = None

        if use_fresh_connection:
            import redis.asyncio as aioredis

            fresh_client = aioredis.Redis(
                connection_pool=aioredis.ConnectionPool.from_url(
                    self._redis.url,
                    decode_responses=False,
                )
            )
            pubsub = fresh_client.pubsub()
        else:
            pubsub = self._redis.client.pubsub()

        try:
            await pubsub.subscribe(channel)
            logger.debug("Subscribed to %s for pattern %s", channel, path_pattern)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    logger.debug("Timeout waiting for event on %s", channel)
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
                        logger.warning("Invalid event message: %s", e)
                        continue

                    if event.matches_path_pattern(path_pattern):
                        # Skip events at or below the requested version threshold
                        if since_version is not None and (event.version or 0) <= since_version:
                            continue
                        logger.debug("Matched event: %s on %s", event.type, event.path)
                        return event

                except TimeoutError:
                    if loop.time() >= deadline:
                        return None
                    continue

        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            if fresh_client:
                await fresh_client.close()

    @requires_started
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
