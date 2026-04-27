"""Session-scoped read accumulator for agent lineage tracking (Issue #3417).

Aggregates reads across multiple API requests for a single agent session,
keyed by (agent_id, agent_generation). When an agent writes, the accumulated
reads become the upstream lineage for that output.

Scoped tracking:
    Agents can open named scopes to isolate reads for different tasks.
    Only reads within a scope are attributed to the write that consumes it.
    Without explicit scopes, a default scope collects all reads (backward compat).

    Flow:
        acc.begin_scope("agent-1", 1, "task-A")
        # ... agent reads files ...
        reads = acc.consume("agent-1", 1, scope_id="task-A")  # only task-A reads

Design decisions:
    - In-memory storage keyed by (agent_id, agent_generation)
    - Scopes within each session: dict[scope_id → entries]
    - Active scope tracked per session (reads go into active scope)
    - TTL-based cleanup for abandoned sessions (default 30 minutes)
    - Max entries per session across all scopes (default 10K)
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
DEFAULT_SCOPE: str = "_default"


@dataclass
class _ReadEntry:
    """A single read recorded in a session."""

    path: str
    version: int
    content_id: str
    access_type: str
    timestamp: float


@dataclass
class _SessionState:
    """Internal state for a single agent session with scoped reads."""

    scopes: dict[str, list[_ReadEntry]] = field(default_factory=lambda: {DEFAULT_SCOPE: []})
    active_scope: str = DEFAULT_SCOPE
    last_access: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)
    saturated: bool = False  # True if max entries was hit (across all scopes)

    @property
    def total_entries(self) -> int:
        return sum(len(entries) for entries in self.scopes.values())


SessionKey = tuple[str, int | None]  # (agent_id, agent_generation)


class SessionReadAccumulator:
    """In-memory accumulator that tracks reads across requests per agent session.

    Thread-safe. Keyed by (agent_id, agent_generation) to isolate sessions.
    Supports named scopes within a session for per-task lineage isolation.

    Usage (simple — no scopes, backward compat):
        >>> acc = SessionReadAccumulator()
        >>> acc.record_read("agent-1", 1, "/data/input.csv", version=5, content_id="abc")
        >>> reads = acc.consume("agent-1", 1)
        >>> len(reads)
        1

    Usage (scoped — per-task isolation):
        >>> acc = SessionReadAccumulator()
        >>> acc.begin_scope("agent-1", 1, "task-A")
        >>> acc.record_read("agent-1", 1, "/data/a.csv", version=1, content_id="ea")
        >>> acc.begin_scope("agent-1", 1, "task-B")
        >>> acc.record_read("agent-1", 1, "/data/b.csv", version=2, content_id="eb")
        >>> reads_a = acc.consume("agent-1", 1, scope_id="task-A")
        >>> reads_b = acc.consume("agent-1", 1, scope_id="task-B")
        >>> len(reads_a), len(reads_b)
        (1, 1)
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

    def begin_scope(
        self,
        agent_id: str,
        agent_generation: int | None,
        scope_id: str,
    ) -> None:
        """Begin a named lineage scope. Reads after this go into the scope.

        If the scope already exists, it becomes active again (reads append).
        The previous scope's reads are preserved (not cleared).

        Args:
            agent_id: Agent starting the scope.
            agent_generation: Session generation counter.
            scope_id: Unique scope identifier (e.g., "task-A", "run-123").
        """
        key: SessionKey = (agent_id, agent_generation)
        session = self._get_or_create_session(key)

        with session.lock:
            session.last_access = time.monotonic()
            if scope_id not in session.scopes:
                session.scopes[scope_id] = []
            session.active_scope = scope_id

    def end_scope(
        self,
        agent_id: str,
        agent_generation: int | None,
        scope_id: str,
    ) -> list[dict[str, Any]]:
        """End a named scope and return its reads (consume + close).

        The scope is removed after consumption. If this was the active scope,
        active reverts to the default scope.

        Args:
            agent_id: Agent ending the scope.
            agent_generation: Session generation counter.
            scope_id: Scope to end.

        Returns:
            List of read dicts from the scope. Empty if scope didn't exist.
        """
        reads = self.consume(agent_id, agent_generation, scope_id=scope_id)

        key: SessionKey = (agent_id, agent_generation)
        with self._global_lock:
            session = self._sessions.get(key)
        if session is not None:
            with session.lock:
                session.scopes.pop(scope_id, None)
                if session.active_scope == scope_id:
                    session.active_scope = DEFAULT_SCOPE

        return reads

    def get_active_scope(
        self,
        agent_id: str,
        agent_generation: int | None,
    ) -> str:
        """Return the currently active scope for a session.

        Returns DEFAULT_SCOPE if no scope has been begun.
        """
        key: SessionKey = (agent_id, agent_generation)
        with self._global_lock:
            session = self._sessions.get(key)
        if session is None:
            return DEFAULT_SCOPE
        with session.lock:
            return session.active_scope

    def record_read(
        self,
        agent_id: str,
        agent_generation: int | None,
        path: str,
        *,
        version: int = 0,
        content_id: str = "",
        access_type: str = "content",
        scope_id: str | None = None,
    ) -> bool:
        """Record a read for an agent session into the active scope.

        Args:
            agent_id: Agent performing the read.
            agent_generation: Session generation counter.
            path: Path of the resource read.
            version: Version of the resource at read time.
            content_id: Content hash at read time.
            access_type: Type of access (content, metadata, list, exists).
            scope_id: Explicit scope to record into (overrides active scope).

        Returns:
            True if recorded, False if session is at max capacity.
        """
        key: SessionKey = (agent_id, agent_generation)
        session = self._get_or_create_session(key)

        with session.lock:
            session.last_access = time.monotonic()

            if session.total_entries >= self._max_entries:
                if not session.saturated:
                    session.saturated = True
                    logger.warning(
                        "Session read accumulator at capacity (%d) for agent %s gen %s",
                        self._max_entries,
                        agent_id,
                        agent_generation,
                    )
                return False

            target_scope = scope_id or session.active_scope
            if target_scope not in session.scopes:
                session.scopes[target_scope] = []

            session.scopes[target_scope].append(
                _ReadEntry(
                    path=path,
                    version=version,
                    content_id=content_id,
                    access_type=access_type,
                    timestamp=time.time(),
                )
            )
            return True

    def consume(
        self,
        agent_id: str,
        agent_generation: int | None,
        scope_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Consume and clear reads from a specific scope (or active scope).

        Only the targeted scope is cleared. Other scopes are untouched.

        Args:
            agent_id: Agent whose reads to consume.
            agent_generation: Session generation counter.
            scope_id: Scope to consume. If None, consumes the active scope.

        Returns:
            List of read dicts with path, version, content_id, access_type.
            Empty list if scope has no reads or doesn't exist.
        """
        key: SessionKey = (agent_id, agent_generation)

        with self._global_lock:
            session = self._sessions.get(key)
            if session is None:
                return []

        with session.lock:
            target = scope_id or session.active_scope
            entries = session.scopes.get(target)
            if not entries:
                return []

            reads = [
                {
                    "path": e.path,
                    "version": e.version,
                    "content_id": e.content_id,
                    "access_type": e.access_type,
                }
                for e in entries
            ]
            entries.clear()
            # Reset saturated flag if we freed capacity
            if session.saturated and session.total_entries < self._max_entries:
                session.saturated = False
            session.last_access = time.monotonic()
            return reads

    def peek(
        self,
        agent_id: str,
        agent_generation: int | None,
        scope_id: str | None = None,
    ) -> int:
        """Return the number of accumulated reads in a scope (or active scope)."""
        key: SessionKey = (agent_id, agent_generation)
        with self._global_lock:
            session = self._sessions.get(key)
        if session is None:
            return 0
        with session.lock:
            target = scope_id or session.active_scope
            entries = session.scopes.get(target)
            return len(entries) if entries else 0

    def clear_session(
        self,
        agent_id: str,
        agent_generation: int | None,
    ) -> None:
        """Explicitly clear a session's accumulated reads (all scopes)."""
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
            total_scopes = 0
            for session in self._sessions.values():
                with session.lock:
                    total_entries += session.total_entries
                    total_scopes += len(session.scopes)

            return {
                "active_sessions": len(self._sessions),
                "total_entries": total_entries,
                "total_scopes": total_scopes,
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
