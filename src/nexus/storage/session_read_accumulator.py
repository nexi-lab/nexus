"""Session-scoped read accumulator for agent lineage tracking (Issue #3417).

Aggregates reads across multiple API requests for a single agent session,
keyed by (agent_id, agent_generation). When an agent writes, the accumulated
reads become the upstream lineage for that output.

Design decisions:
    - In-memory storage keyed by (agent_id, agent_generation)
    - TTL-based cleanup for abandoned sessions (default 30 minutes)
    - Max entries per session (default 10K) to prevent unbounded growth
    - Thread-safe via per-session threading.Lock
    - Lazy cleanup on access + periodic sweep
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Defaults
DEFAULT_TTL_SECONDS: float = 1800.0  # 30 minutes
DEFAULT_MAX_ENTRIES: int = 10_000
DEFAULT_SWEEP_INTERVAL: float = 300.0  # 5 minutes


@dataclass
class _ReadEntry:
    """A single read recorded in a session."""

    path: str
    version: int
    etag: str
    access_type: str
    timestamp: float


@dataclass
class _SessionState:
    """Internal state for a single agent session."""

    entries: list[_ReadEntry] = field(default_factory=list)
    last_access: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    saturated: bool = False  # True if max entries was hit


SessionKey = tuple[str, int | None]  # (agent_id, agent_generation)


class SessionReadAccumulator:
    """In-memory accumulator that tracks reads across requests per agent session.

    Thread-safe. Keyed by (agent_id, agent_generation) to isolate sessions.

    Usage:
        >>> acc = SessionReadAccumulator()
        >>> acc.record_read("agent-1", 1, "/data/input.csv", version=5, etag="abc")
        >>> acc.record_read("agent-1", 1, "/data/config.yaml", version=3, etag="def")
        >>> reads = acc.consume("agent-1", 1)
        >>> len(reads)
        2
        >>> acc.consume("agent-1", 1)  # consumed — now empty
        []
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        sweep_interval: float = DEFAULT_SWEEP_INTERVAL,
    ) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._sweep_interval = sweep_interval
        self._sessions: dict[SessionKey, _SessionState] = {}
        self._global_lock = threading.Lock()
        self._last_sweep: float = time.monotonic()

    def record_read(
        self,
        agent_id: str,
        agent_generation: int | None,
        path: str,
        *,
        version: int = 0,
        etag: str = "",
        access_type: str = "content",
    ) -> bool:
        """Record a read for an agent session.

        Args:
            agent_id: Agent performing the read.
            agent_generation: Session generation counter.
            path: Path of the resource read.
            version: Version of the resource at read time.
            etag: Content hash at read time.
            access_type: Type of access (content, metadata, list, exists).

        Returns:
            True if recorded, False if session is at max capacity.
        """
        key: SessionKey = (agent_id, agent_generation)
        session = self._get_or_create_session(key)

        with session.lock:
            session.last_access = time.monotonic()

            if len(session.entries) >= self._max_entries:
                if not session.saturated:
                    session.saturated = True
                    logger.warning(
                        "Session read accumulator at capacity (%d) for agent %s gen %s",
                        self._max_entries,
                        agent_id,
                        agent_generation,
                    )
                return False

            session.entries.append(
                _ReadEntry(
                    path=path,
                    version=version,
                    etag=etag,
                    access_type=access_type,
                    timestamp=time.time(),
                )
            )
            return True

    def consume(
        self,
        agent_id: str,
        agent_generation: int | None,
    ) -> list[dict[str, Any]]:
        """Consume and clear all accumulated reads for a session.

        Called by the lineage hook when an agent writes. Returns the reads
        as dicts suitable for LineageAspect.from_session_reads().

        Args:
            agent_id: Agent whose reads to consume.
            agent_generation: Session generation counter.

        Returns:
            List of read dicts with path, version, etag, access_type.
            Empty list if no reads accumulated.
        """
        key: SessionKey = (agent_id, agent_generation)

        with self._global_lock:
            session = self._sessions.get(key)
            if session is None:
                return []

        with session.lock:
            if not session.entries:
                return []

            reads = [
                {
                    "path": e.path,
                    "version": e.version,
                    "etag": e.etag,
                    "access_type": e.access_type,
                }
                for e in session.entries
            ]
            session.entries.clear()
            session.saturated = False
            session.last_access = time.monotonic()
            return reads

    def peek(
        self,
        agent_id: str,
        agent_generation: int | None,
    ) -> int:
        """Return the number of accumulated reads without consuming them."""
        key: SessionKey = (agent_id, agent_generation)
        with self._global_lock:
            session = self._sessions.get(key)
        if session is None:
            return 0
        with session.lock:
            return len(session.entries)

    def clear_session(
        self,
        agent_id: str,
        agent_generation: int | None,
    ) -> None:
        """Explicitly clear a session's accumulated reads."""
        key: SessionKey = (agent_id, agent_generation)
        with self._global_lock:
            self._sessions.pop(key, None)

    def cleanup_expired(self) -> int:
        """Remove sessions that have exceeded their TTL.

        Returns:
            Number of sessions removed.
        """
        now = time.monotonic()
        expired_keys: list[SessionKey] = []

        with self._global_lock:
            for key, session in self._sessions.items():
                if now - session.last_access > self._ttl:
                    expired_keys.append(key)

            for key in expired_keys:
                del self._sessions[key]

        if expired_keys:
            logger.info(
                "Session read accumulator: cleaned up %d expired sessions",
                len(expired_keys),
            )
        return len(expired_keys)

    def maybe_sweep(self) -> int:
        """Run cleanup if sweep interval has elapsed. Returns count removed."""
        now = time.monotonic()
        if now - self._last_sweep < self._sweep_interval:
            return 0
        self._last_sweep = now
        return self.cleanup_expired()

    def get_stats(self) -> dict[str, Any]:
        """Return accumulator statistics."""
        with self._global_lock:
            total_entries = 0
            for session in self._sessions.values():
                with session.lock:
                    total_entries += len(session.entries)

            return {
                "active_sessions": len(self._sessions),
                "total_entries": total_entries,
                "ttl_seconds": self._ttl,
                "max_entries_per_session": self._max_entries,
            }

    def _get_or_create_session(self, key: SessionKey) -> _SessionState:
        """Get or create a session state, with lazy sweep."""
        self.maybe_sweep()

        with self._global_lock:
            session = self._sessions.get(key)
            if session is None:
                session = _SessionState()
                self._sessions[key] = session
            return session


# Module-level singleton for use by the lineage hook and API layer
_global_accumulator: SessionReadAccumulator | None = None
_init_lock = threading.Lock()


def get_accumulator() -> SessionReadAccumulator:
    """Get the global SessionReadAccumulator singleton."""
    global _global_accumulator
    if _global_accumulator is None:
        with _init_lock:
            if _global_accumulator is None:
                _global_accumulator = SessionReadAccumulator()
    return _global_accumulator


def reset_accumulator() -> None:
    """Reset the global accumulator (for testing only)."""
    global _global_accumulator
    _global_accumulator = None
