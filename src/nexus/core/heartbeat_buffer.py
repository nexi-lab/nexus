"""In-memory heartbeat buffer with batch flush (Issue #1589).

Decoupled from storage: accepts a flush_callback for DB writes.
Thread-safe via a single threading.Lock for all mutable state.

Extracted from AgentRegistry to follow SRP — heartbeat buffering has
different write patterns (high-frequency), consistency requirements
(eventually-consistent), and failure modes (restore-to-buffer) compared
to agent identity/lifecycle management.

Design decisions:
    - record() NOT heartbeat() — avoids name collision with AgentRegistry.heartbeat()
    - No _known_agents check — that stays in AgentRegistry (needs DB access)
    - flush_callback injected — no SQLAlchemy dependency
    - _restore_buffer() extracted as named method (fixes 5-level nesting)
    - Single _lock for all mutable state
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class HeartbeatBuffer:
    """In-memory heartbeat buffer with batch flush.

    Accepts a flush_callback for DB writes so the buffer has no storage
    dependency.  Thread-safe via a single threading.Lock.

    Args:
        flush_callback: Called with ``dict[str, datetime]`` to persist
            buffered heartbeats.  Must return the number of entries flushed.
        flush_interval: Seconds between automatic flushes triggered by
            :meth:`record`.  Defaults to 60.
        max_buffer_size: Hard cap on buffer entries.  A warning is emitted
            at 80 % capacity.  Defaults to 50 000.
    """

    def __init__(
        self,
        flush_callback: Callable[[dict[str, datetime]], int],
        flush_interval: int = 60,
        max_buffer_size: int = 50_000,
    ) -> None:
        self._flush_callback = flush_callback
        self._flush_interval = flush_interval
        self._max_buffer_size = max_buffer_size

        self._buffer: dict[str, datetime] = {}
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()

        # Observability counters
        self._total_recorded: int = 0
        self._total_flushed: int = 0
        self._flush_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, agent_id: str) -> None:
        """Record a heartbeat (buffer write only, no DB check).

        If the flush interval has elapsed the buffer is automatically
        flushed via the injected callback.

        Args:
            agent_id: Agent identifier.
        """
        now = datetime.now(UTC)
        should_flush = False
        buffer_snapshot: dict[str, datetime] | None = None

        with self._lock:
            self._buffer[agent_id] = now
            self._total_recorded += 1

            # Warn at 80 % buffer capacity
            buffer_len = len(self._buffer)
            threshold = int(0.8 * self._max_buffer_size)
            if buffer_len >= threshold:
                pct = buffer_len / self._max_buffer_size * 100
                logger.warning(
                    "[HEARTBEAT] Buffer at %.0f%% capacity (%d/%d)",
                    pct,
                    buffer_len,
                    self._max_buffer_size,
                )

            # Auto-flush when interval elapses
            elapsed = time.monotonic() - self._last_flush
            if elapsed >= self._flush_interval:
                buffer_snapshot = dict(self._buffer)
                self._buffer.clear()
                self._last_flush = time.monotonic()
                should_flush = True

        if should_flush and buffer_snapshot:
            self._do_flush(buffer_snapshot)

    def flush(self) -> int:
        """Flush buffer to DB via callback.

        Returns:
            Number of heartbeats flushed.
        """
        with self._lock:
            if not self._buffer:
                return 0
            buffer_snapshot = dict(self._buffer)
            self._buffer.clear()
            self._last_flush = time.monotonic()

        return self._do_flush(buffer_snapshot)

    def remove(self, agent_id: str) -> None:
        """Remove an agent from the buffer (called on unregister).

        Args:
            agent_id: Agent identifier.
        """
        with self._lock:
            self._buffer.pop(agent_id, None)

    def recently_heartbeated(self, cutoff: datetime) -> set[str]:
        """Return agent IDs with buffer timestamps >= *cutoff*.

        Used by :meth:`AgentRegistry.detect_stale` to exclude agents
        whose heartbeats have not yet been flushed to the database.

        Args:
            cutoff: Only include agents whose buffered timestamp is at
                or after this value.

        Returns:
            Set of agent IDs with recent buffered heartbeats.
        """
        with self._lock:
            return {aid for aid, ts in self._buffer.items() if ts >= cutoff}

    def stats(self) -> dict[str, Any]:
        """Return buffer statistics for observability.

        Returns:
            Dictionary with ``buffer_size``, ``total_recorded``,
            ``total_flushed``, and ``flush_count``.
        """
        with self._lock:
            return {
                "buffer_size": len(self._buffer),
                "total_recorded": self._total_recorded,
                "total_flushed": self._total_flushed,
                "flush_count": self._flush_count,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _do_flush(self, buffer_snapshot: dict[str, datetime]) -> int:
        """Execute the flush callback and update counters.

        On failure the entries are restored via :meth:`_restore_buffer`.

        Args:
            buffer_snapshot: Copy of the buffer to flush.

        Returns:
            Number of heartbeats flushed.
        """
        try:
            flushed = self._flush_callback(buffer_snapshot)
            with self._lock:
                self._total_flushed += flushed
                self._flush_count += 1
            return flushed
        except Exception:
            self._restore_buffer(buffer_snapshot)
            raise

    def _restore_buffer(self, buffer: dict[str, datetime]) -> int:
        """Restore entries after a flush failure.

        Merges *buffer* back into ``_buffer``, keeping the newer
        timestamp when both contain the same agent ID.  Respects
        ``_max_buffer_size`` to prevent OOM.

        Args:
            buffer: Entries to restore.

        Returns:
            Number of entries actually restored.
        """
        restored = 0
        with self._lock:
            for aid, ts in buffer.items():
                if len(self._buffer) >= self._max_buffer_size:
                    dropped = len(buffer) - restored
                    logger.warning(
                        "[HEARTBEAT] Buffer at max capacity (%d), "
                        "dropping %d entries to prevent OOM",
                        self._max_buffer_size,
                        dropped,
                    )
                    break
                existing = self._buffer.get(aid)
                if existing is None or ts > existing:
                    self._buffer[aid] = ts
                    restored += 1
        logger.warning(
            "[HEARTBEAT] Flush failed, %d entries restored to buffer",
            restored,
            exc_info=True,
        )
        return restored
