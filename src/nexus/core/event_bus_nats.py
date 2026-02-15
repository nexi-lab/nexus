"""NATS JetStream event bus implementation.

Communication brick implementing EventBusProtocol via NATS JetStream.
Provides durable event delivery, stream replay, and consumer groups.

Subject hierarchy: nexus.events.{zone_id}.{event_type}
Stream: NEXUS_EVENTS (limits-based, 7d retention, file storage)
Consumers: Pull-based, durable, per consumer_name

Issue #1331: Replace Dragonfly pub/sub with NATS JetStream.
Keep Dragonfly for caching only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import nats
from nats.aio.client import Client as NatsClient
from nats.errors import (
    ConnectionClosedError,
    NoServersError,
)
from nats.errors import (
    TimeoutError as NatsTimeoutError,
)
from nats.js import JetStreamContext
from nats.js.api import (
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StorageType,
    StreamConfig,
)
from nats.js.errors import NotFoundError

from nexus.core.event_bus import (
    AckableEvent,
    EventBusBase,
    FileEvent,
    FileEventType,
)

if TYPE_CHECKING:
    from nats.aio.msg import Msg

logger = logging.getLogger(__name__)


def _deliver_policy_from_str(policy: str) -> DeliverPolicy:
    """Convert string deliver policy to NATS DeliverPolicy enum."""
    mapping = {
        "all": DeliverPolicy.ALL,
        "last": DeliverPolicy.LAST,
        "new": DeliverPolicy.NEW,
    }
    result = mapping.get(policy)
    if result is None:
        raise ValueError(f"Unknown deliver_policy: {policy!r}. Use 'all', 'last', or 'new'.")
    return result


class NatsEventBus(EventBusBase):
    """NATS JetStream implementation of EventBusBase.

    Provides durable event delivery with exactly-once semantics via
    JetStream message deduplication and pull-based consumers.
    """

    STREAM_NAME = "NEXUS_EVENTS"
    SUBJECT_PREFIX = "nexus.events"

    # Stream limits (nats-py accepts seconds for time fields)
    MAX_AGE_SECS = 7 * 24 * 3600  # 7 days
    MAX_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
    DUPLICATE_WINDOW_SECS = 120  # 2 min dedup window

    def __init__(
        self,
        nats_url: str = "nats://localhost:4222",
        session_factory: Any | None = None,
        node_id: str | None = None,
        max_reconnect_attempts: int = -1,
        reconnect_time_wait: float = 2.0,
    ) -> None:
        """Initialize NatsEventBus.

        Args:
            nats_url: NATS server URL (e.g., "nats://localhost:4222")
            session_factory: SQLAlchemy SessionLocal for PG SSOT (optional)
            node_id: Unique node identifier (auto-generated if None)
            max_reconnect_attempts: Max reconnect attempts (-1 = infinite)
            reconnect_time_wait: Seconds between reconnect attempts
        """
        super().__init__(session_factory=session_factory, node_id=node_id)
        self._nats_url = nats_url
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_time_wait = reconnect_time_wait
        self._nc: NatsClient | None = None
        self._js: JetStreamContext | None = None
        self._started = False
        self._lock = asyncio.Lock()

    def _subject(self, zone_id: str, event_type: str) -> str:
        """Build subject for a specific zone and event type."""
        return f"{self.SUBJECT_PREFIX}.{zone_id}.{event_type}"

    def _zone_wildcard(self, zone_id: str) -> str:
        """Build wildcard subject matching all event types in a zone."""
        return f"{self.SUBJECT_PREFIX}.{zone_id}.>"

    # =========================================================================
    # Lifecycle
    # =========================================================================

    async def start(self) -> None:
        """Connect to NATS and ensure JetStream stream exists."""
        if self._started:
            return

        async with self._lock:
            if self._started:
                return

            try:
                self._nc = await nats.connect(
                    self._nats_url,
                    max_reconnect_attempts=self._max_reconnect_attempts,
                    reconnect_time_wait=self._reconnect_time_wait,
                    disconnected_cb=self._on_disconnect,
                    reconnected_cb=self._on_reconnect,
                    error_cb=self._on_error,
                )
                self._js = self._nc.jetstream()

                # Ensure stream exists (idempotent — update if config changed)
                await self._js.add_stream(
                    StreamConfig(
                        name=self.STREAM_NAME,
                        subjects=[f"{self.SUBJECT_PREFIX}.>"],
                        retention=RetentionPolicy.LIMITS,
                        max_age=self.MAX_AGE_SECS,
                        storage=StorageType.FILE,
                        num_replicas=1,
                        duplicate_window=self.DUPLICATE_WINDOW_SECS,
                        max_bytes=self.MAX_BYTES,
                    )
                )

                self._started = True
                logger.info(f"NatsEventBus started (url={self._nats_url})")

            except (NoServersError, ConnectionClosedError, OSError) as e:
                logger.error(f"Failed to connect to NATS: {e}")
                raise

    async def stop(self) -> None:
        """Drain and close NATS connection."""
        if not self._started:
            return

        async with self._lock:
            if not self._started:
                return

            if self._nc and self._nc.is_connected:
                try:
                    await self._nc.drain()
                except Exception as e:
                    logger.warning(f"Error draining NATS connection: {e}")

            self._js = None
            self._nc = None
            self._started = False
            logger.info("NatsEventBus stopped")

    # =========================================================================
    # Publish
    # =========================================================================

    async def publish(self, event: FileEvent) -> int:
        """Publish event to JetStream with synchronous ack.

        Args:
            event: FileEvent to publish.

        Returns:
            Stream sequence number (ack.seq) as int.

        Raises:
            RuntimeError: If the bus is not started.
        """
        if not self._started or self._js is None:
            raise RuntimeError("NatsEventBus not started. Call start() first.")

        zone_id = event.zone_id or "default"
        event_type = event.type.value if isinstance(event.type, FileEventType) else event.type
        subject = self._subject(zone_id, event_type)

        payload = event.to_json().encode("utf-8")

        # Headers for metadata and deduplication
        headers = {
            "Nats-Msg-Id": event.event_id,  # JetStream dedup key
            "zone_id": zone_id,
        }

        try:
            ack = await self._js.publish(subject, payload, headers=headers)
            logger.debug(
                f"Published {event_type} event for {event.path} to {subject} (seq={ack.seq})"
            )
            return int(ack.seq)
        except Exception as e:
            logger.error(f"Failed to publish event to NATS: {e}")
            raise

    # =========================================================================
    # Subscribe (backward-compat wrapper)
    # =========================================================================

    async def subscribe(self, zone_id: str) -> AsyncIterator[FileEvent]:
        """Subscribe with auto-ack (backward compatibility wrapper).

        Wraps subscribe_durable() and auto-acks each message so callers
        that don't need explicit ack/nack can use the simple interface.
        """
        async for ackable in self.subscribe_durable(zone_id, f"auto-{self._node_id}"):
            await ackable.ack()
            yield ackable.event

    # =========================================================================
    # Durable Subscribe (pull consumer)
    # =========================================================================

    async def subscribe_durable(
        self,
        zone_id: str,
        consumer_name: str,
        deliver_policy: str = "all",
    ) -> AsyncIterator[AckableEvent]:
        """Subscribe with durable pull consumer.

        Creates or binds to a durable consumer and fetches messages in batches.
        Each message is wrapped in AckableEvent with ack/nack/in_progress callbacks.

        Args:
            zone_id: Zone ID to subscribe to.
            consumer_name: Durable consumer name (survives reconnects).
            deliver_policy: "all", "last", or "new".

        Yields:
            AckableEvent objects with ack/nack support.
        """
        if not self._started or self._js is None:
            raise RuntimeError("NatsEventBus not started. Call start() first.")

        subject = self._zone_wildcard(zone_id)
        policy = _deliver_policy_from_str(deliver_policy)

        # Create or bind durable pull consumer
        consumer_config = ConsumerConfig(
            durable_name=consumer_name,
            filter_subject=subject,
            deliver_policy=policy,
            ack_wait=30,  # 30s ack timeout
        )

        try:
            sub = await self._js.pull_subscribe(
                subject,
                durable=consumer_name,
                config=consumer_config,
            )
        except Exception as e:
            logger.error(f"Failed to create pull subscription: {e}")
            raise

        try:
            while True:
                try:
                    msgs = await sub.fetch(batch=10, timeout=5)
                except NatsTimeoutError:
                    # No messages available — yield control and retry
                    await asyncio.sleep(0)
                    continue
                except (ConnectionClosedError, asyncio.CancelledError):
                    raise
                except Exception as e:
                    logger.warning(f"Error fetching messages: {e}")
                    await asyncio.sleep(1.0)
                    continue

                for msg in msgs:
                    try:
                        event = FileEvent.from_json(msg.data)
                    except (json.JSONDecodeError, KeyError) as e:
                        logger.warning(f"Invalid event message on {msg.subject}: {e}")
                        await msg.ack()  # Ack bad messages to prevent redelivery
                        continue

                    yield AckableEvent(
                        event=event,
                        _ack_fn=self._make_ack_fn(msg),
                        _nack_fn=self._make_nack_fn(msg),
                        _in_progress_fn=self._make_in_progress_fn(msg),
                    )

        except asyncio.CancelledError:
            logger.debug(f"Durable subscription {consumer_name} cancelled")
            raise
        finally:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    @staticmethod
    def _make_ack_fn(msg: Msg) -> Any:
        async def _ack() -> None:
            await msg.ack()

        return _ack

    @staticmethod
    def _make_nack_fn(msg: Msg) -> Any:
        async def _nack(delay: float | None = None) -> None:
            if delay is not None:
                await msg.nak(delay=delay)
            else:
                await msg.nak()

        return _nack

    @staticmethod
    def _make_in_progress_fn(msg: Msg) -> Any:
        async def _in_progress() -> None:
            await msg.in_progress()

        return _in_progress

    # =========================================================================
    # Wait for event (ephemeral consumer)
    # =========================================================================

    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_revision: int | None = None,
    ) -> FileEvent | None:
        """Wait for a matching event using an ephemeral consumer.

        Args:
            zone_id: Zone ID to subscribe to.
            path_pattern: Path pattern to match.
            timeout: Maximum time to wait in seconds.
            since_revision: Only return events with revision > this value.

        Returns:
            FileEvent if matched, None on timeout.
        """
        if not self._started or self._js is None:
            raise RuntimeError("NatsEventBus not started. Call start() first.")

        subject = self._zone_wildcard(zone_id)

        try:
            sub = await self._js.subscribe(subject, ordered_consumer=True)
        except Exception as e:
            logger.error(f"Failed to create ephemeral subscription: {e}")
            raise

        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None

                try:
                    msg = await asyncio.wait_for(
                        sub.next_msg(),
                        timeout=min(remaining, 1.0),
                    )
                except (TimeoutError, NatsTimeoutError):
                    if loop.time() >= deadline:
                        return None
                    continue

                try:
                    event = FileEvent.from_json(msg.data)
                except (json.JSONDecodeError, KeyError):
                    continue

                if not event.matches_path_pattern(path_pattern):
                    continue

                # Filter by revision if specified
                if since_revision is not None and (
                    event.revision is None or event.revision <= since_revision
                ):
                    continue

                return event

        finally:
            with contextlib.suppress(Exception):
                await sub.unsubscribe()

    # =========================================================================
    # Health & Stats
    # =========================================================================

    async def health_check(self) -> bool:
        """Check NATS connection and JetStream availability."""
        if not self._started or self._nc is None:
            return False

        try:
            if not self._nc.is_connected:
                return False
            # Verify JetStream stream exists
            if self._js:
                await self._js.find_stream_name_by_subject(f"{self.SUBJECT_PREFIX}.>")
            return True
        except (NotFoundError, Exception) as e:
            logger.warning(f"NATS health check failed: {e}")
            return False

    async def get_stats(self) -> dict[str, Any]:
        """Return NATS/JetStream statistics."""
        checkpoint = await self._get_checkpoint()

        stats: dict[str, Any] = {
            "backend": "nats_jetstream",
            "status": "running" if self._started else "stopped",
            "nats_url": self._nats_url,
            "node_id": self._node_id,
            "last_checkpoint": checkpoint.isoformat() if checkpoint else None,
            "ssot_enabled": self._session_factory is not None,
        }

        if self._started and self._js is not None:
            try:
                stream_info = await self._js.stream_info(self.STREAM_NAME)
                state = stream_info.state
                stats["stream"] = {
                    "name": self.STREAM_NAME,
                    "messages": state.messages,
                    "bytes": state.bytes,
                    "first_seq": state.first_seq,
                    "last_seq": state.last_seq,
                    "consumer_count": state.consumer_count,
                }
            except Exception as e:
                stats["stream_error"] = str(e)

        return stats

    # =========================================================================
    # Reconnection callbacks
    # =========================================================================

    async def _on_disconnect(self) -> None:
        logger.warning("NATS disconnected")

    async def _on_reconnect(self) -> None:
        logger.info("NATS reconnected")

    async def _on_error(self, e: Exception) -> None:
        logger.error(f"NATS error: {e}")
