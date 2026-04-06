"""DT_PIPE wakeup signal implementations for IPC.

Bridges the IPC brick's WakeupNotifier/WakeupListener/NotifyPipeFactory
protocols to the kernel's PipeManager.

Issue #3197:
  - PipeWakeupNotifier: ms (Redis round-trip) -> us wakeup via DT_PIPE
  - PipeWakeupListener: drain-and-process pattern for signal coalescing
  - PipeNotifyFactory: creates small-capacity notify pipes on provisioning

Issue #3194:
  - wait_for_signal(): generic drain-and-process utility with timeout fallback
    Reused by PipeWakeupListener (IPC).

Architecture:
  - DT_PIPE for same-node wakeup (us latency, no Redis dependency)
  - EventBus retained for cross-node notifications
  - VFS files for durability (DLQ, audit trail) — unchanged

Note: Uses duck-typing (Any) for PipeManager to respect the LEGO brick
import boundary (bricks must not import from nexus.core directly).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from nexus.bricks.ipc.conventions import notify_pipe_path
from nexus.lib.pipe_wakeup import wait_for_signal  # re-export for backward compat

logger = logging.getLogger(__name__)

# Wakeup signal: single byte, content doesn't matter
_WAKEUP_SIGNAL = b"\x01"

# Notify pipe capacity — small because wakeup signals are 1 byte each.
# 256 bytes = 256 pending signals, far more than needed with drain-and-process.
NOTIFY_PIPE_CAPACITY = 256


class PipeWakeupNotifier:
    """Sends wakeup signals via DT_PIPE. Best-effort — never raises.

    Used by MessageSender to notify recipients of new inbox messages
    at ~0.5us latency (vs ms for Redis EventBus round-trip).

    Args:
        pipe_manager: PipeManager instance (duck-typed to avoid core imports).
    """

    def __init__(self, pipe_manager: Any) -> None:
        self._pm = pipe_manager

    async def notify(self, agent_id: str) -> None:
        """Write a 1-byte wakeup signal to the agent's notify pipe.

        Best-effort: catches all errors and logs a debug message.
        The poll fallback will catch any missed wakeups.
        """
        path = notify_pipe_path(agent_id)
        try:
            self._pm.pipe_write_nowait(path, _WAKEUP_SIGNAL)
        except Exception:
            logger.debug(
                "Wakeup signal failed for agent %s (best-effort, poll will catch up)",
                agent_id,
            )


class PipeWakeupListener:
    """Listens for wakeup signals from DT_PIPE with drain-and-process pattern.

    Used by MessageProcessor to wake on new inbox messages. The
    drain-and-process pattern coalesces burst signals: if 10 messages
    arrive before the processor reads, all 10 signals are drained in
    one shot, resulting in a single process_inbox() call.

    Thread-safe: MemoryPipeBackend.write_nowait uses call_soon_threadsafe
    for cross-thread wakeup (RPC handler threads -> event loop thread).

    Args:
        pipe_manager: PipeManager instance (duck-typed to avoid core imports).
        agent_id: Agent whose notify pipe to listen on.
    """

    def __init__(self, pipe_manager: Any, agent_id: str) -> None:
        self._pm = pipe_manager
        self._agent_id = agent_id
        self._path = notify_pipe_path(agent_id)

    async def wait_for_wakeup(self) -> None:
        """Block until at least one wakeup signal arrives, then drain all pending.

        Implements the drain-and-process pattern (eventfd read semantics):
        1. Block on first signal (pipe_read, blocking=True)
        2. Drain remaining signals (pipe_read, blocking=False)
        3. Return — caller processes inbox once for all coalesced signals
        """
        await wait_for_signal(self._pm, self._path)

    def close(self) -> None:
        """Signal the listener to stop. Wakes any blocked wait_for_wakeup()."""
        with contextlib.suppress(Exception):
            self._pm.signal_close(self._path)


class PipeNotifyFactory:
    """Creates DT_PIPE notification pipes during agent provisioning.

    Called by AgentProvisioner.provision() to set up a small-capacity
    wakeup pipe alongside the standard inbox/outbox directories.

    Pipe capacity is intentionally small (256 bytes) since wakeup
    signals are 1 byte each and the drain-and-process pattern
    coalesces them.

    Args:
        pipe_manager: PipeManager instance (duck-typed to avoid core imports).
        capacity: Pipe capacity in bytes. Default 256.
    """

    def __init__(self, pipe_manager: Any, capacity: int = NOTIFY_PIPE_CAPACITY) -> None:
        self._pm = pipe_manager
        self._capacity = capacity

    def create_notify_pipe(self, agent_id: str) -> None:
        """Create a small-capacity notify pipe for the agent. Idempotent."""
        path = notify_pipe_path(agent_id)
        try:
            self._pm.create(path, capacity=self._capacity)
        except Exception:
            # Already exists — idempotent provisioning
            logger.debug("Notify pipe already exists for agent %s", agent_id)


class CacheStoreEventPublisher:
    """Bridges CacheStoreABC pub/sub to the IPC EventPublisher protocol.

    MessageSender uses EventPublisher.publish(channel, data) to notify
    recipients of new inbox messages.  This adapter serializes the event
    dict to JSON and publishes it via CacheStore (Dragonfly/Redis) pub/sub,
    enabling cross-node EventBus notifications without requiring a separate
    EventBus service for IPC.

    Satisfies the ``EventPublisher`` protocol from ``protocols.py``.
    """

    def __init__(self, cache_store: Any) -> None:
        self._cs = cache_store

    async def publish(self, channel: str, data: dict) -> None:
        """Publish an IPC event to a CacheStore pub/sub channel."""
        import json

        try:
            await self._cs.publish(channel, json.dumps(data).encode())
        except Exception:
            logger.debug(
                "CacheStore EventPublisher failed for channel %s (best-effort)",
                channel,
            )
