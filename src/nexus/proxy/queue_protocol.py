"""Offline queue protocol and in-memory implementation.

``OfflineQueueProtocol`` defines the contract for offline queue backends
so ``ProxyBrick`` doesn't depend on the concrete ``OfflineQueue`` (SQLite).

``InMemoryQueue`` provides a lightweight test/fallback implementation.
"""

from __future__ import annotations

import itertools
import time
from collections import deque
from typing import Any, Protocol

from nexus.proxy.offline_queue import QueuedOperation


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
    ) -> int: ...

    async def dequeue_batch(self, limit: int = 50) -> list[QueuedOperation]: ...

    async def mark_done(self, op_id: int) -> None: ...

    async def mark_failed(self, op_id: int) -> None: ...

    async def mark_dead_letter(self, op_id: int) -> None: ...

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
        self._done: set[int] = set()
        self._dead_letter: set[int] = set()

    async def initialize(self) -> None:
        """No-op for in-memory queue. Safe to call multiple times."""

    async def enqueue(
        self,
        method: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        payload_ref: str | None = None,
    ) -> int:
        """Add an operation to the queue. Returns the operation id."""
        import json

        if len(self._pending) >= self._max_size:
            raise QueueFullError(
                f"Queue full: {len(self._pending)} pending ops (max {self._max_size})"
            )

        op_id = next(self._counter)
        op = QueuedOperation(
            id=op_id,
            method=method,
            args_json=json.dumps(args),
            kwargs_json=json.dumps(kwargs or {}),
            payload_ref=payload_ref,
            retry_count=0,
            created_at=time.time(),
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
            # else: skip already-completed ops

        # Put back unprocessed items
        remaining.extend(self._pending)
        self._pending = remaining

        return batch

    async def mark_done(self, op_id: int) -> None:
        """Mark an operation as successfully replayed."""
        self._done.add(op_id)
        self._pending = deque(op for op in self._pending if op.id != op_id)

    async def mark_failed(self, op_id: int) -> None:
        """Re-enqueue with incremented retry count."""
        new_pending: deque[QueuedOperation] = deque()
        for op in self._pending:
            if op.id == op_id:
                # Create new op with incremented retry count
                updated = QueuedOperation(
                    id=op.id,
                    method=op.method,
                    args_json=op.args_json,
                    kwargs_json=op.kwargs_json,
                    payload_ref=op.payload_ref,
                    retry_count=op.retry_count + 1,
                    created_at=op.created_at,
                )
                new_pending.append(updated)
            else:
                new_pending.append(op)
        self._pending = new_pending

    async def mark_dead_letter(self, op_id: int) -> None:
        """Permanently remove an operation."""
        self._dead_letter.add(op_id)
        self._pending = deque(op for op in self._pending if op.id != op_id)

    async def pending_count(self) -> int:
        """Return the number of pending operations."""
        return len(self._pending)

    async def close(self) -> None:
        """Clear all state."""
        self._pending.clear()
        self._done.clear()
        self._dead_letter.clear()
