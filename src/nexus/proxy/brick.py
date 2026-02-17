"""Proxy brick — transparent edge-to-cloud forwarding.

``ProxyBrick`` forwards protocol operations to a remote kernel over HTTP.
When the remote is unreachable, operations are queued to a WAL-backed
offline queue and replayed automatically when connectivity resumes.
"""

import asyncio
import base64
import contextlib
import json
import logging
from dataclasses import asdict
from typing import Any, cast

import httpx

from nexus.proxy.circuit_breaker import AsyncCircuitBreaker, CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import (
    CircuitOpenError,
    OfflineQueuedError,
    RemoteCallError,
)
from nexus.proxy.offline_queue import OfflineQueue
from nexus.proxy.transport import HttpTransport

logger = logging.getLogger(__name__)

class ProxyBrick:
    """Base proxy that forwards operations to a remote kernel.

    Subclasses implement specific protocol methods (VFS, EventLog, etc.)
    by delegating to ``_forward()``.

    Supports ``async with`` for guaranteed cleanup::

        async with ProxyBrick(config) as proxy:
            await proxy._forward("method", key="value")

    Parameters
    ----------
    config:
        Proxy configuration.
    transport:
        Optional pre-built transport (for testing).
    queue:
        Optional pre-built offline queue (for testing).
    """

    def __init__(
        self,
        config: ProxyBrickConfig,
        *,
        transport: HttpTransport | None = None,
        queue: OfflineQueue | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpTransport(config)
        self._queue = queue or OfflineQueue(
            config.queue_db_path, max_retry_count=config.max_retry_count
        )
        self._circuit = AsyncCircuitBreaker(
            failure_threshold=config.cb_failure_threshold,
            recovery_timeout=config.cb_recovery_timeout,
            half_open_max_calls=config.cb_half_open_max_calls,
        )
        self._replay_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "ProxyBrick":
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialize the offline queue and start the replay loop."""
        await self._queue.initialize()
        self._stopped = False
        self._replay_task = asyncio.create_task(self._replay_loop())
        logger.info("ProxyBrick started for %s", self._config.remote_url)

    async def stop(self) -> None:
        """Gracefully shut down — cancel replay and close resources."""
        self._stopped = True
        if self._replay_task is not None:
            self._replay_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._replay_task
            self._replay_task = None
        await self._transport.close()
        await self._queue.close()
        logger.info("ProxyBrick stopped")

    # ------------------------------------------------------------------
    # Core forwarding
    # ------------------------------------------------------------------

    async def _forward(self, method: str, **kwargs: Any) -> Any:
        """Forward a method call to the remote kernel.

        1. Check circuit breaker — if OPEN, enqueue and raise.
        2. Try transport.call() (with tenacity retries).
        3. On success → record_success().
        4. On connection failure → record_failure() → enqueue → raise OfflineQueuedError.
        """
        allowed = await self._circuit.allow_request()
        if not allowed:
            queue_id = await self._queue.enqueue(method, kwargs=kwargs)
            logger.warning("Circuit open — operation '%s' queued (id=%d)", method, queue_id)
            raise CircuitOpenError(
                self._config.remote_url,
                retry_after=self._config.cb_recovery_timeout,
            )

        try:
            result = await self._transport.call(method, params=kwargs)
            await self._circuit.record_success()
            return result
        except RemoteCallError as exc:
            if _is_connection_error(exc):
                await self._circuit.record_failure()
                queue_id = await self._queue.enqueue(method, kwargs=kwargs)
                logger.warning("Operation '%s' queued for offline replay (id=%d)", method, queue_id)
                raise OfflineQueuedError(method, queue_id) from exc
            raise

    async def _forward_stream(self, method: str, data: bytes, **kwargs: Any) -> Any:
        """Forward a large-payload call via streaming upload."""
        allowed = await self._circuit.allow_request()
        if not allowed:
            queue_id = await self._queue.enqueue(method, kwargs=kwargs)
            raise CircuitOpenError(
                self._config.remote_url,
                retry_after=self._config.cb_recovery_timeout,
            )

        try:
            result = await self._transport.stream_upload(method, data, params=kwargs)
            await self._circuit.record_success()
            return result
        except RemoteCallError as exc:
            if _is_connection_error(exc):
                await self._circuit.record_failure()
                queue_id = await self._queue.enqueue(method, kwargs=kwargs)
                logger.warning(
                    "Streaming operation '%s' queued for offline replay (id=%d)",
                    method,
                    queue_id,
                )
                raise OfflineQueuedError(method, queue_id) from exc
            raise

    # ------------------------------------------------------------------
    # Replay loop
    # ------------------------------------------------------------------

    async def _replay_loop(self) -> None:
        """Background task that drains the offline queue when online."""
        while not self._stopped:
            try:
                await asyncio.sleep(self._config.replay_poll_interval)
                if self._circuit.is_open:
                    continue

                batch = await self._queue.dequeue_batch(self._config.replay_batch_size)
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
                        if _is_connection_error(exc):
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
                await asyncio.sleep(self._config.replay_poll_interval)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def circuit_state(self) -> CircuitState:
        """Current circuit breaker state (may be slightly stale)."""
        return self._circuit.state

    async def pending_count(self) -> int:
        """Return the number of pending operations in the offline queue."""
        return await self._queue.pending_count()

# ======================================================================
# Protocol proxy implementations
# ======================================================================

class ProxyVFSBrick(ProxyBrick):
    """Proxy for ``VFSOperations`` — forwards file ops to a cloud kernel.

    Per KERNEL-ARCHITECTURE.md §4, ``zone_id`` is forwarded to the remote
    server for zone-scoped isolation.
    """

    async def read(self, path: str, zone_id: str) -> bytes:
        result = await self._forward("read", path=path, zone_id=zone_id)
        if isinstance(result, bytes):
            return result
        if isinstance(result, str):
            return result.encode()
        raise TypeError(f"Expected bytes or str from remote read, got {type(result).__name__}")

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        if len(data) > self._config.stream_threshold_bytes:
            await self._forward_stream("write", data, path=path, zone_id=zone_id)
            return
        encoded = base64.b64encode(data).decode()
        await self._forward("write", path=path, content=encoded, zone_id=zone_id)

    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        return cast(list[str], await self._forward("list_dir", path=path, zone_id=zone_id))

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        await self._forward("rename", src=src, dst=dst, zone_id=zone_id)

    async def mkdir(self, path: str, zone_id: str) -> None:
        await self._forward("mkdir", path=path, zone_id=zone_id)

    async def count_dir(self, path: str, zone_id: str) -> int:
        return cast(int, await self._forward("count_dir", path=path, zone_id=zone_id))

    async def exists(self, path: str, zone_id: str) -> bool:
        return cast(bool, await self._forward("exists", path=path, zone_id=zone_id))

class ProxyEventLogBrick(ProxyBrick):
    """Proxy for ``EventLogProtocol`` — forwards audit events to cloud."""

    async def append(self, event: Any) -> Any:
        return await self._forward("event_log.append", event=asdict(event))

    async def read(
        self,
        *,
        since_sequence: int = 0,
        limit: int = 100,
        zone_id: str | None = None,
    ) -> list[Any]:
        return await self._forward(  # type: ignore[no-any-return]
            "event_log.read",
            since_sequence=since_sequence,
            limit=limit,
            zone_id=zone_id,
        )

class ProxySchedulerBrick(ProxyBrick):
    """Proxy for ``SchedulerProtocol`` — forwards scheduling to cloud.

    Implements the full 8-method protocol (Issue #1274).
    """

    async def submit(self, request: Any) -> str:
        result = await self._forward("scheduler.submit", request=asdict(request))
        return str(result) if result is not None else ""

    async def next(self, *, executor_id: str | None = None) -> Any | None:
        return await self._forward("scheduler.next", executor_id=executor_id)

    async def pending_count(self, *, zone_id: str | None = None) -> int:
        return await self._forward("scheduler.pending_count", zone_id=zone_id)  # type: ignore[no-any-return]

    async def cancel(self, agent_id: str) -> int:
        return await self._forward("scheduler.cancel", agent_id=agent_id)  # type: ignore[no-any-return]

    async def get_status(self, task_id: str) -> dict[str, Any] | None:
        return await self._forward("scheduler.get_status", task_id=task_id)  # type: ignore[no-any-return]

    async def complete(self, task_id: str, *, error: str | None = None) -> None:
        await self._forward("scheduler.complete", task_id=task_id, error=error)

    async def classify(self, request: Any) -> str:
        result = await self._forward("scheduler.classify", request=asdict(request))
        return str(result) if result is not None else "batch"

    async def metrics(self, *, zone_id: str | None = None) -> dict[str, Any]:
        return await self._forward("scheduler.metrics", zone_id=zone_id)  # type: ignore[no-any-return]

class ProxyAgentRegistryBrick(ProxyBrick):
    """Proxy for ``AgentRegistryProtocol`` — forwards registry ops to cloud."""

    async def register(
        self,
        agent_id: str,
        owner_id: str,
        *,
        zone_id: str | None = None,
        name: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        return await self._forward(
            "agent_registry.register",
            agent_id=agent_id,
            owner_id=owner_id,
            zone_id=zone_id,
            name=name,
            metadata=metadata,
        )

    async def get(self, agent_id: str) -> Any | None:
        return await self._forward("agent_registry.get", agent_id=agent_id)

    async def transition(
        self,
        agent_id: str,
        target_state: str,
        *,
        expected_generation: int | None = None,
    ) -> Any:
        return await self._forward(
            "agent_registry.transition",
            agent_id=agent_id,
            target_state=target_state,
            expected_generation=expected_generation,
        )

    async def heartbeat(self, agent_id: str) -> None:
        await self._forward("agent_registry.heartbeat", agent_id=agent_id)

    async def list_by_zone(self, zone_id: str) -> list[Any]:
        return await self._forward("agent_registry.list_by_zone", zone_id=zone_id)  # type: ignore[no-any-return]

    async def unregister(self, agent_id: str) -> bool:
        return await self._forward("agent_registry.unregister", agent_id=agent_id)  # type: ignore[no-any-return]

# ======================================================================
# Helpers
# ======================================================================

def _is_connection_error(exc: RemoteCallError) -> bool:
    """Return True if the underlying cause is a connectivity failure."""
    cause = exc.cause
    if cause is None:
        return False
    return isinstance(cause, (httpx.ConnectError, httpx.TimeoutException, ConnectionError, OSError))
