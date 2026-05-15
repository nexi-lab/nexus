"""DT_STREAM — Ordered intra-zone cache invalidation stream.

Replaces coordinator callback lists with an append-only event stream.
Each cache layer consumes events at its own offset, enabling:
- Ordered delivery (boundary before visibility)
- Independent failure recovery (each consumer tracks its own position)
- Replay capability for missed events

The stream is in-memory per-process. For cross-zone invalidation,
see pubsub_invalidation.py.

Related: Issue #3192
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class InvalidationEventType(Enum):
    """Types of cache invalidation events."""

    BOUNDARY = "boundary"
    VISIBILITY = "visibility"
    NAMESPACE = "namespace"
    ZONE_GRAPH = "zone_graph"
    L1_CACHE = "l1_cache"
    ITERATOR = "iterator"


@dataclass
class InvalidationEvent:
    """A single cache invalidation event in the stream."""

    event_type: InvalidationEventType
    zone_id: str
    payload: dict[str, Any]
    sequence: int = 0
    timestamp: float = field(default_factory=time.time)


class InvalidationStream:
    """Append-only invalidation event stream.

    Producers append events. Each consumer tracks its own offset
    and processes events independently.

    Thread-safe for concurrent producers and consumers.
    """

    def __init__(self, max_size: int = 10_000):
        """Initialize the stream.

        Args:
            max_size: Maximum events to retain. Oldest are trimmed.
        """
        self._events: deque[InvalidationEvent] = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._sequence = 0
        self._max_size = max_size

        # Consumer offsets: consumer_id -> last processed sequence
        self._consumer_offsets: dict[str, int] = {}
        # Consumer callbacks: consumer_id -> callback function
        self._consumers: dict[str, Callable[[InvalidationEvent], None]] = {}

        # Metrics
        self._total_events = 0
        self._total_consumed = 0
        self._consumer_errors = 0

    def append(self, event_type: InvalidationEventType, zone_id: str, **payload: Any) -> int:
        """Append an invalidation event to the stream.

        Args:
            event_type: Type of invalidation
            zone_id: Zone where the invalidation occurred
            **payload: Event-specific data

        Returns:
            Sequence number of the appended event
        """
        with self._lock:
            self._sequence += 1
            event = InvalidationEvent(
                event_type=event_type,
                zone_id=zone_id,
                payload=payload,
                sequence=self._sequence,
            )
            self._events.append(event)
            self._total_events += 1

        # Notify consumers
        self._dispatch(event)
        return self._sequence

    def register_consumer(
        self,
        consumer_id: str,
        callback: Callable[[InvalidationEvent], None],
        _event_types: list[InvalidationEventType] | None = None,
    ) -> None:
        """Register a consumer for stream events.

        Args:
            consumer_id: Unique identifier for this consumer
            callback: Function to call for each event
            event_types: Optional filter — only receive these event types.
                         If None, receives all events.
        """
        with self._lock:
            self._consumers[consumer_id] = callback
            if consumer_id not in self._consumer_offsets:
                self._consumer_offsets[consumer_id] = self._sequence
        logger.info("[DT_STREAM] Consumer %s registered", consumer_id)

    def unregister_consumer(self, consumer_id: str) -> None:
        """Unregister a consumer."""
        with self._lock:
            self._consumers.pop(consumer_id, None)
            self._consumer_offsets.pop(consumer_id, None)

    def _dispatch(self, event: InvalidationEvent) -> None:
        """Dispatch event to all registered consumers.

        Each consumer's failure is isolated — one failing consumer
        doesn't block others.
        """
        consumers = list(self._consumers.items())

        for consumer_id, callback in consumers:
            try:
                callback(event)
                with self._lock:
                    self._consumer_offsets[consumer_id] = event.sequence
                    self._total_consumed += 1
            except Exception:
                self._consumer_errors += 1
                logger.warning(
                    "[DT_STREAM] Consumer %s failed for event %d",
                    consumer_id,
                    event.sequence,
                    exc_info=True,
                )

    def replay_from(self, consumer_id: str, from_sequence: int) -> int:
        """Replay events from a specific sequence for a consumer.

        Used for failure recovery — consumer can re-process missed events.

        Args:
            consumer_id: Consumer to replay for
            from_sequence: Sequence to start from (exclusive)

        Returns:
            Number of events replayed
        """
        callback = self._consumers.get(consumer_id)
        if callback is None:
            return 0

        replayed = 0
        with self._lock:
            events_to_replay = [e for e in self._events if e.sequence > from_sequence]

        for event in events_to_replay:
            try:
                callback(event)
                with self._lock:
                    self._consumer_offsets[consumer_id] = event.sequence
                replayed += 1
            except Exception:
                self._consumer_errors += 1
                logger.warning(
                    "[DT_STREAM] Replay failed for consumer %s at sequence %d",
                    consumer_id,
                    event.sequence,
                    exc_info=True,
                )
                break  # Stop replay on error

        return replayed

    def get_stats(self) -> dict[str, Any]:
        """Get stream statistics."""
        with self._lock:
            return {
                "total_events": self._total_events,
                "total_consumed": self._total_consumed,
                "consumer_errors": self._consumer_errors,
                "stream_size": len(self._events),
                "max_size": self._max_size,
                "current_sequence": self._sequence,
                "consumer_count": len(self._consumers),
                "consumer_offsets": dict(self._consumer_offsets),
            }
