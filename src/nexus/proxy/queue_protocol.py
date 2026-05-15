"""Offline queue protocol and in-memory implementation.

``OfflineQueueProtocol`` defines the contract for offline queue backends
so ``ProxyBrick`` doesn't depend on the concrete ``OfflineQueue`` (SQLite).

``InMemoryQueue`` provides a lightweight test/fallback implementation.
"""

import hashlib
import itertools
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class QueuedOperation:
    """A single queued operation awaiting replay."""

    id: int
    method: str
    args_json: str
    kwargs_json: str
    payload_ref: str | None
    retry_count: int
    created_at: float
    idempotency_key: str | None = None
    vector_clock: str | None = None
    priority: int = 0


class QueueFullError(Exception):
    """Raised when the in-memory queue exceeds its maximum size."""


class OfflineQueueProtocol(Protocol):
    """Structural protocol for offline operation queues.

    Not ``@runtime_checkable`` — only used for static type checking (#13-A).
    """

    async def initialize(self) -> None: ...

    async def enqueue(
        self,
        method: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        vector_clock: str | None = None,
        priority: int = 0,
    ) -> int: ...

    async def dequeue_batch(self, limit: int = 50) -> list[QueuedOperation]: ...

    async def mark_done(self, op_id: int) -> None: ...

    async def mark_failed(self, op_id: int) -> None: ...

    async def mark_dead_letter(self, op_id: int) -> None: ...

    async def has_idempotency_key(self, key: str) -> bool: ...

    async def pending_count(self) -> int: ...

    async def close(self) -> None: ...


class InMemoryQueue:
    """In-memory offline queue for testing and fallback (#2-A, #14-A).

    Parameters
    ----------
    max_size:
        Maximum number of pending operations before ``QueueFullError``.
    """

    def __init__(self, max_size: int = 10_000) -> None:
        self._max_size = max_size
        self._counter = itertools.count(1)
        self._pending: deque[QueuedOperation] = deque()
        self._in_flight: dict[int, QueuedOperation] = {}
        self._done: set[int] = set()
        self._done_keys: set[str] = set()
        self._dead_letter: set[int] = set()

    async def initialize(self) -> None:
        """No-op for in-memory queue. Safe to call multiple times."""

    @staticmethod
    def _generate_idempotency_key(method: str, kwargs: dict[str, Any] | None) -> str:
        """Derive a deterministic idempotency key from method + kwargs."""
        import json

        canonical = json.dumps({"m": method, "k": kwargs or {}}, sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:32]

    async def enqueue(
        self,
        method: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        payload_ref: str | None = None,
        vector_clock: str | None = None,
        priority: int = 0,
    ) -> int:
        """Add an operation to the queue. Returns the operation id."""
        import json

        if len(self._pending) >= self._max_size:
            raise QueueFullError(
                f"Queue full: {len(self._pending)} pending ops (max {self._max_size})"
            )

        op_id = next(self._counter)
        idem_key = self._generate_idempotency_key(method, kwargs)
        op = QueuedOperation(
            id=op_id,
            method=method,
            args_json=json.dumps(args),
            kwargs_json=json.dumps(kwargs or {}),
            payload_ref=payload_ref,
            retry_count=0,
            created_at=time.time(),
            idempotency_key=idem_key,
            vector_clock=vector_clock,
            priority=priority,
        )
        self._pending.append(op)
        return op_id

    async def dequeue_batch(self, limit: int = 50) -> list[QueuedOperation]:
        """Fetch up to *limit* pending operations (FIFO order)."""
        batch: list[QueuedOperation] = []
        remaining: deque[QueuedOperation] = deque()

        while self._pending and len(batch) < limit:
            op = self._pending.popleft()
            if op.id not in self._done and op.id not in self._dead_letter:
                batch.append(op)
                self._in_flight[op.id] = op
            # else: skip already-completed ops

        # Put back unprocessed items
        remaining.extend(self._pending)
        self._pending = remaining

        return batch

    async def mark_done(self, op_id: int) -> None:
        """Mark an operation as successfully replayed."""
        self._done.add(op_id)
        removed = self._in_flight.pop(op_id, None)
        if removed and removed.idempotency_key:
            self._done_keys.add(removed.idempotency_key)
        self._pending = deque(op for op in self._pending if op.id != op_id)

    async def mark_failed(self, op_id: int) -> None:
        """Re-enqueue with incremented retry count."""
        # Check in-flight first (ops removed by dequeue_batch)
        in_flight_op = self._in_flight.pop(op_id, None)
        if in_flight_op is not None:
            updated = QueuedOperation(
                id=in_flight_op.id,
                method=in_flight_op.method,
                args_json=in_flight_op.args_json,
                kwargs_json=in_flight_op.kwargs_json,
                payload_ref=in_flight_op.payload_ref,
                retry_count=in_flight_op.retry_count + 1,
                created_at=in_flight_op.created_at,
                idempotency_key=in_flight_op.idempotency_key,
                vector_clock=in_flight_op.vector_clock,
                priority=in_flight_op.priority,
            )
            self._pending.append(updated)
            return

        # Fallback: scan _pending (for ops not yet dequeued)
        new_pending: deque[QueuedOperation] = deque()
        for op in self._pending:
            if op.id == op_id:
                updated = QueuedOperation(
                    id=op.id,
                    method=op.method,
                    args_json=op.args_json,
                    kwargs_json=op.kwargs_json,
                    payload_ref=op.payload_ref,
                    retry_count=op.retry_count + 1,
                    created_at=op.created_at,
                    idempotency_key=op.idempotency_key,
                    vector_clock=op.vector_clock,
                    priority=op.priority,
                )
                new_pending.append(updated)
            else:
                new_pending.append(op)
        self._pending = new_pending

    async def mark_dead_letter(self, op_id: int) -> None:
        """Permanently remove an operation."""
        self._dead_letter.add(op_id)
        self._in_flight.pop(op_id, None)
        self._pending = deque(op for op in self._pending if op.id != op_id)

    async def has_idempotency_key(self, key: str) -> bool:
        """Check if an operation with this idempotency key was already completed."""
        return key in self._done_keys

    async def pending_count(self) -> int:
        """Return the number of pending operations."""
        return len(self._pending) + len(self._in_flight)

    async def close(self) -> None:
        """Clear all state."""
        self._pending.clear()
        self._in_flight.clear()
        self._done.clear()
        self._done_keys.clear()
        self._dead_letter.clear()
