"""Replay engine — extracted from ProxyBrick._replay_loop().

Drains the offline queue and replays operations through the transport
when the circuit breaker allows requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

from nexus.proxy.errors import RemoteCallError, is_connection_error

if TYPE_CHECKING:
    from nexus.proxy.circuit_breaker import AsyncCircuitBreaker
    from nexus.proxy.queue_protocol import OfflineQueueProtocol
    from nexus.proxy.transport import HttpTransport

logger = logging.getLogger(__name__)


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
        queue: OfflineQueueProtocol,
        transport: HttpTransport,
        circuit: AsyncCircuitBreaker,
        batch_size: int,
        poll_interval: float,
    ) -> None:
        self._queue = queue
        self._transport = transport
        self._circuit = circuit
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._stopped = False
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Signal the replay loop to check the queue immediately.

        Called by ``ProxyBrick`` after enqueueing an operation so that
        replay starts without waiting for the next poll interval.
        """
        self._wake.set()

    async def run(self) -> None:
        """Background task that drains the offline queue when online."""
        while not self._stopped:
            try:
                # Wait for wake signal or poll interval, whichever comes first
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self._poll_interval)
                self._wake.clear()

                if self._circuit.is_open:
                    continue

                batch = await self._queue.dequeue_batch(self._batch_size)
                if not batch:
                    continue

                logger.info("Replaying %d queued operations", len(batch))
                for op in batch:
                    try:
                        kwargs = json.loads(op.kwargs_json)
                        if not isinstance(kwargs, dict):
                            logger.error("Invalid kwargs_json for op %d: not a dict", op.id)
                            await self._queue.mark_dead_letter(op.id)
                            continue
                        await self._transport.call(op.method, params=kwargs)
                        await self._queue.mark_done(op.id)
                        await self._circuit.record_success()
                    except json.JSONDecodeError as jexc:
                        logger.error("Failed to decode op %d: %s", op.id, jexc)
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
