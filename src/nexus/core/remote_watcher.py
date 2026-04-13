"""Kernel-tier RemoteWatchProtocol implementations.

    StreamRemoteWatcher     — DT_STREAM transport (default, no external deps)
    StreamEventObserver     — OBSERVE-phase publisher for StreamRemoteWatcher

StreamRemoteWatcher uses kernel DT_STREAM as transport. Events are written
by StreamEventObserver and read by ``wait_for_event()`` via blocking
stream reads. No NATS or Dragonfly URL required — works out-of-the-box.

For EventBus-backed remote watching (NATS/Dragonfly), see
``nexus.services.event_bus.remote_watcher.EventBusRemoteWatcher``.

See: KERNEL-ARCHITECTURE.md §4.3
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.core.file_events import FileEvent
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# Well-known stream path for kernel event delivery
_EVENT_STREAM_PATH = "/__sys__/events/watch"
_EVENT_STREAM_CAPACITY = 4 * 1024 * 1024  # 4MB default


class StreamRemoteWatcher:
    """RemoteWatchProtocol via kernel DT_STREAM — no external dependencies.

    Default implementation. Events are published by ``StreamEventObserver``
    (OBSERVE phase) and read by ``wait_for_event()`` via non-destructive
    offset-based stream reads.

    Single-node: in-memory Rust MemoryStreamBackend (~0.5μs/op).
    Multi-node:  WALStreamBackend for Raft-replicated event delivery (future).
    """

    def __init__(self, nx: "NexusFS") -> None:
        self._nx = nx
        self._offsets: dict[str, int] = {}  # zone_id → read offset
        self._initialized = False

    def _ensure_stream(self) -> None:
        """Lazily create the event DT_STREAM via Rust kernel."""
        if self._initialized:
            return
        if not self._nx.has_stream(_EVENT_STREAM_PATH):
            self._nx.stream_create(_EVENT_STREAM_PATH, _EVENT_STREAM_CAPACITY)
        self._initialized = True

    def publish(self, event: "FileEvent") -> None:
        """Write a FileEvent to the DT_STREAM (called by StreamEventObserver)."""
        self._ensure_stream()
        payload = event.to_json().encode("utf-8")
        with contextlib.suppress(Exception):
            self._nx.stream_write_nowait(_EVENT_STREAM_PATH, payload)

    async def wait_for_event(
        self,
        zone_id: str,
        path_pattern: str,
        timeout: float = 30.0,
        since_version: int | None = None,
    ) -> "FileEvent | None":
        """Block until a matching FileEvent arrives on the DT_STREAM.

        Reads from tracked offset, skipping non-matching events.
        Returns None on timeout.
        """
        from nexus.core.file_events import FileEvent as FE

        self._ensure_stream()
        offset = self._offsets.get(zone_id, 0)
        deadline = asyncio.get_event_loop().time() + timeout

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None

            try:
                # Try nowait first; fall back to Rust blocking read
                result = self._nx.stream_read_at(_EVENT_STREAM_PATH, offset)
                if result is None:
                    data, new_offset = await asyncio.wait_for(
                        asyncio.to_thread(
                            self._nx.stream_read_at_blocking,
                            _EVENT_STREAM_PATH,
                            offset,
                            int(remaining * 1000),
                        ),
                        timeout=remaining,
                    )
                else:
                    data, new_offset = result
                offset = new_offset
                self._offsets[zone_id] = offset

                event = FE.from_json(data.decode("utf-8"))

                # Filter: zone, pattern, version
                if zone_id and event.zone_id and event.zone_id != zone_id:
                    continue
                if not event.matches_path_pattern(path_pattern):
                    continue
                if (
                    since_version is not None
                    and event.version is not None
                    and event.version <= since_version
                ):
                    continue

                return event

            except TimeoutError:
                return None
            except Exception:
                # StreamEmpty/StreamClosed — wait briefly and retry
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    return None
                await asyncio.sleep(min(0.1, remaining))


# ---------------------------------------------------------------------------
# StreamEventObserver — OBSERVE-phase publisher for StreamRemoteWatcher
# ---------------------------------------------------------------------------


class StreamEventObserver:
    """Publishes FileEvents to StreamRemoteWatcher.

    Retained for explicit publish() calls from factory wiring.
    Observer dispatch is handled by the Rust kernel's MutationObserver
    trait. stream_write_nowait is ~0.5us (no network I/O).
    """

    def __init__(self, watcher: StreamRemoteWatcher) -> None:
        self._watcher = watcher

    def publish(self, event: "FileEvent") -> None:
        """Publish event to stream — stream_write_nowait is ~0.5μs."""
        self._watcher.publish(event)
