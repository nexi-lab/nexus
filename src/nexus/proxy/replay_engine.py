"""Replay engine — extracted from ProxyBrick._replay_loop().

Drains the offline queue and replays operations through the transport
when the circuit breaker allows requests.

Issue #3062: Vector-clock causal ordering added.  Batches dequeued
FIFO are re-sorted by causal order before replay.
"""

import asyncio
import base64
import binascii
import contextlib
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from nexus.proxy.errors import RemoteCallError, is_connection_error
from nexus.proxy.queue_protocol import QueuedOperation

if TYPE_CHECKING:
    from nexus.proxy.circuit_breaker import AsyncCircuitBreaker
    from nexus.proxy.queue_protocol import OfflineQueueProtocol
    from nexus.proxy.transport import HttpTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vector-clock utilities (Issue #3062)
# ---------------------------------------------------------------------------


def _parse_vector_clock(vc_json: str | None) -> dict[str, int]:
    """Parse a JSON-encoded vector clock into a dict.

    Returns an empty dict for None or invalid input (defensive).
    """
    if not vc_json:
        return {}
    try:
        parsed = json.loads(vc_json)
        if isinstance(parsed, dict):
            return {str(k): int(v) for k, v in parsed.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return {}


def _vc_happens_before(a: dict[str, int], b: dict[str, int]) -> bool:
    """Return True if vector clock *a* causally happens-before *b*.

    a < b iff: for all keys, a[k] <= b[k], AND at least one a[k] < b[k].
    """
    if not a or not b:
        return False
    all_keys = set(a) | set(b)
    at_least_one_less = False
    for k in all_keys:
        ak = a.get(k, 0)
        bk = b.get(k, 0)
        if ak > bk:
            return False
        if ak < bk:
            at_least_one_less = True
    return at_least_one_less


def _sort_by_causal_order(ops: list[QueuedOperation]) -> list[QueuedOperation]:
    """Sort operations by vector-clock causal order.

    Uses a topological sort based on the happens-before relation.
    Concurrent (incomparable) operations are ordered by their original
    queue id as a deterministic tiebreaker.
    """
    if not ops or len(ops) <= 1:
        return list(ops)

    # Parse clocks once
    clocks = [_parse_vector_clock(op.vector_clock) for op in ops]

    # If no operations have vector clocks, skip sorting (pure FIFO)
    if all(not vc for vc in clocks):
        return list(ops)

    # Kahn's algorithm for topological sort
    n = len(ops)
    # Build adjacency: edge i->j means ops[i] happens-before ops[j]
    in_degree = [0] * n
    adj: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i != j and _vc_happens_before(clocks[i], clocks[j]):
                adj[i].append(j)
                in_degree[j] += 1

    # BFS with tie-breaking by op.id (deterministic for concurrent ops)
    import heapq

    queue: list[tuple[int, int]] = []  # (op.id, index) for stable ordering
    for i in range(n):
        if in_degree[i] == 0:
            heapq.heappush(queue, (ops[i].id, i))

    result: list[QueuedOperation] = []
    while queue:
        _, idx = heapq.heappop(queue)
        result.append(ops[idx])
        for j in adj[idx]:
            in_degree[j] -= 1
            if in_degree[j] == 0:
                heapq.heappush(queue, (ops[j].id, j))

    # If graph has a cycle (shouldn't happen with valid VCs), append remaining
    if len(result) < n:
        seen = {id(op) for op in result}
        for op in ops:
            if id(op) not in seen:
                result.append(op)

    return result


class ReplayEngine:
    """Background replay loop — extracted from ``ProxyBrick._replay_loop()``.

    Parameters
    ----------
    queue:
        Offline queue implementing ``OfflineQueueProtocol``.
    transport:
        HTTP transport for replaying operations.
    circuit:
        Circuit breaker for connectivity state.
    batch_size:
        Operations per replay batch.
    poll_interval:
        Seconds between replay polls.
    """

    def __init__(
        self,
        queue: "OfflineQueueProtocol",
        transport: "HttpTransport",
        circuit: "AsyncCircuitBreaker",
        batch_size: int,
        poll_interval: float,
        on_replay_success: Callable[[], None] | None = None,
    ) -> None:
        self._queue = queue
        self._transport = transport
        self._circuit = circuit
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._on_replay_success = on_replay_success
        self._stopped = False
        self._wake = asyncio.Event()
        self._replayed_keys: dict[str, None] = {}
        self._max_replayed_keys = 10_000

    def wake(self) -> None:
        """Signal the replay loop to check the queue immediately."""
        self._wake.set()

    async def run(self) -> None:
        """Background task that drains the offline queue when online."""
        while not self._stopped:
            try:
                # Wait for wake signal or poll interval, whichever comes first
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                self._wake.clear()
                if self._circuit.is_open:
                    continue

                batch = await self._queue.dequeue_batch(self._batch_size)
                if not batch:
                    continue

                # Re-check after dequeue — circuit may have opened during the async call
                if self._circuit.is_open:
                    continue

                # Issue #3062: Sort by vector-clock causal order before replay.
                # Concurrent (incomparable) ops use queue id as tiebreaker.
                batch = _sort_by_causal_order(batch)

                logger.info("Replaying %d queued operations", len(batch))
                for op in batch:
                    # Gate each op through allow_request() to respect half-open limits
                    if not await self._circuit.allow_request():
                        logger.warning("Circuit does not allow requests — stopping batch")
                        break

                    try:
                        # Idempotency: check persistent store first (survives restarts)
                        if op.idempotency_key:
                            if hasattr(
                                self._queue, "has_idempotency_key"
                            ) and await self._queue.has_idempotency_key(op.idempotency_key):
                                logger.info(
                                    "Skipping duplicate op %d (persistent key=%s)",
                                    op.id,
                                    op.idempotency_key[:8],
                                )
                                await self._queue.mark_done(op.id)
                                continue
                            if op.idempotency_key in self._replayed_keys:
                                logger.info(
                                    "Skipping duplicate op %d (key=%s)",
                                    op.id,
                                    op.idempotency_key[:8],
                                )
                                await self._queue.mark_done(op.id)
                                continue

                        kwargs = json.loads(op.kwargs_json)
                        if not isinstance(kwargs, dict):
                            logger.error("Invalid kwargs_json for op %d: not a dict", op.id)
                            await self._queue.mark_dead_letter(op.id)
                            continue

                        # Dispatch: use stream_upload for ops with a stored payload
                        if op.payload_ref:
                            payload_data = base64.b64decode(op.payload_ref)
                            await self._transport.stream_upload(
                                op.method, payload_data, params=kwargs
                            )
                        else:
                            await self._transport.call(op.method, params=kwargs)

                        await self._queue.mark_done(op.id)
                        await self._circuit.record_success()
                        if self._on_replay_success is not None:
                            self._on_replay_success()
                        if op.idempotency_key:
                            self._replayed_keys[op.idempotency_key] = None
                            if len(self._replayed_keys) > self._max_replayed_keys:
                                # Evict oldest entries (first inserted in dict order)
                                excess = len(self._replayed_keys) - self._max_replayed_keys
                                for evict_key in list(self._replayed_keys)[:excess]:
                                    del self._replayed_keys[evict_key]
                    except (json.JSONDecodeError, binascii.Error) as decode_exc:
                        logger.error("Failed to decode op %d: %s", op.id, decode_exc)
                        await self._queue.mark_dead_letter(op.id)
                    except RemoteCallError as exc:
                        if is_connection_error(exc):
                            await self._circuit.record_failure()
                            await self._queue.mark_failed(op.id)
                            logger.warning(
                                "Replay failed for op %d (%s) — stopping batch",
                                op.id,
                                op.method,
                            )
                            break
                        await self._queue.mark_failed(op.id)
                        logger.error("Replay error for op %d (%s): %s", op.id, op.method, exc)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Unexpected error in replay loop")
                await asyncio.sleep(self._poll_interval)

    async def stop(self) -> None:
        """Signal stop and wake the loop so it exits promptly."""
        self._stopped = True
        self._wake.set()
