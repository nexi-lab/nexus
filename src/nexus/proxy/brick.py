"""Proxy brick — transparent edge-to-cloud forwarding.

``ProxyBrick`` forwards protocol operations to a remote kernel over HTTP.
When the remote is unreachable, operations are queued to a WAL-backed
offline queue and replayed automatically when connectivity resumes.
"""

import asyncio
import base64
import contextlib
import logging
from dataclasses import asdict
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from nexus.proxy.circuit_breaker import AsyncCircuitBreaker, CircuitState
from nexus.proxy.config import ProxyBrickConfig
from nexus.proxy.errors import (
    CircuitOpenError,
    OfflineQueuedError,
    RemoteCallError,
    is_connection_error,
)
from nexus.proxy.offline_queue import OfflineQueue
from nexus.proxy.replay_engine import ReplayEngine
from nexus.proxy.transport import HttpTransport

if TYPE_CHECKING:
    from nexus.proxy.edge_sync import EdgeSyncManager
    from nexus.proxy.queue_protocol import OfflineQueueProtocol

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
        queue: "OfflineQueueProtocol | None" = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpTransport(config)
        self._queue: OfflineQueueProtocol = queue or OfflineQueue(
            config.queue_db_path, max_retry_count=config.max_retry_count
        )
        self._circuit = AsyncCircuitBreaker(
            failure_threshold=config.cb_failure_threshold,
            recovery_timeout=config.cb_recovery_timeout,
            half_open_max_calls=config.cb_half_open_max_calls,
        )
        self._replay_engine: ReplayEngine | None = None
        self._replay_task: asyncio.Task[None] | None = None
        self._edge_sync: EdgeSyncManager | None = None
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
        self._replay_engine = ReplayEngine(
            queue=self._queue,
            transport=self._transport,
            circuit=self._circuit,
            batch_size=self._config.replay_batch_size,
            poll_interval=self._config.replay_poll_interval,
            on_replay_success=self._on_replay_success,
        )
        self._replay_task = asyncio.create_task(self._replay_engine.run())

        # Edge sync manager (Issue #1707)
        from nexus.proxy.edge_sync import EdgeSyncManager as _ESM

        self._edge_sync = _ESM(
            queue=self._queue,
            transport=self._transport,
            circuit=self._circuit,
            health_check_url=self._config.reconnect_health_check_url,
            replay_wake=self._wake_replay,
        )
        await self._edge_sync.start()
        logger.info("ProxyBrick started for %s", self._config.remote_url)

    async def stop(self) -> None:
        """Gracefully shut down — cancel replay and close resources."""
        self._stopped = True
        if self._edge_sync is not None:
            await self._edge_sync.stop()
        if self._replay_engine is not None:
            await self._replay_engine.stop()
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

    async def _do_forward(self, method: str, *, data: bytes | None = None, **kwargs: Any) -> Any:
        """Unified forward — handles both regular and streaming calls (#6-A)."""
        payload_ref = base64.b64encode(data).decode() if data is not None else None

        allowed = await self._circuit.allow_request()
        if not allowed:
            if self._edge_sync is not None:
                self._edge_sync.notify_disconnected()
            queue_id = await self._queue.enqueue(method, kwargs=kwargs, payload_ref=payload_ref)
            self._wake_replay()
            logger.warning("Circuit open — operation '%s' queued (id=%d)", method, queue_id)
            raise CircuitOpenError(
                self._config.remote_url,
                retry_after=self._config.cb_recovery_timeout,
            )

        try:
            if data is not None:
                result = await self._transport.stream_upload(method, data, params=kwargs)
            else:
                result = await self._transport.call(method, params=kwargs)
            await self._circuit.record_success()
            if self._edge_sync is not None:
                self._edge_sync.notify_connected()
            return result
        except RemoteCallError as exc:
            if is_connection_error(exc):
                await self._circuit.record_failure()
                if self._edge_sync is not None:
                    self._edge_sync.notify_disconnected()
                queue_id = await self._queue.enqueue(method, kwargs=kwargs, payload_ref=payload_ref)
                self._wake_replay()
                logger.warning("Operation '%s' queued for offline replay (id=%d)", method, queue_id)
                raise OfflineQueuedError(method, queue_id) from exc
            raise

    async def _forward(self, method: str, **kwargs: Any) -> Any:
        """Forward a method call to the remote kernel."""
        return await self._do_forward(method, **kwargs)

    async def _forward_stream(self, method: str, data: bytes, **kwargs: Any) -> Any:
        """Forward a large-payload call via streaming upload."""
        return await self._do_forward(method, data=data, **kwargs)

    def _wake_replay(self) -> None:
        """Signal the replay engine to process the queue immediately."""
        if self._replay_engine is not None:
            self._replay_engine.wake()

    def _on_replay_success(self) -> None:
        """Called by ReplayEngine after a successful replay — advances reconnect state."""
        if self._edge_sync is not None:
            self._edge_sync.notify_connected()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def circuit_state(self) -> CircuitState:
        """Current circuit breaker state (may be slightly stale)."""
        return self._circuit.state

    @property
    def edge_sync_manager(self) -> "EdgeSyncManager | None":
        """The edge sync manager, if initialized."""
        return self._edge_sync

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

    Method names use the ``sys_`` prefix to match the kernel syscall convention
    (Issue #834).
    """

    async def sys_read(self, path: str, zone_id: str) -> bytes:
        result = await self._forward("read", path=path, zone_id=zone_id)
        if isinstance(result, bytes):
            return result
        if isinstance(result, str):
            return result.encode()
        raise TypeError(f"Expected bytes or str from remote read, got {type(result).__name__}")

    async def sys_write(self, path: str, data: bytes, zone_id: str) -> None:
        if len(data) > self._config.stream_threshold_bytes:
            await self._forward_stream("write", data, path=path, zone_id=zone_id)
            return
        encoded = base64.b64encode(data).decode()
        await self._forward("write", path=path, content=encoded, zone_id=zone_id)

    async def write(self, path: str, data: bytes, zone_id: str) -> None:
        """Tier 2 write with create-on-write — delegates to sys_write for proxy."""
        await self.sys_write(path, data, zone_id)

    async def sys_readdir(self, path: str, zone_id: str) -> list[str]:
        return cast(list[str], await self._forward("list_dir", path=path, zone_id=zone_id))

    # Convenience alias — proxy tests and callers may still use list_dir.
    async def list_dir(self, path: str, zone_id: str) -> list[str]:
        return await self.sys_readdir(path, zone_id)

    async def sys_rename(self, src: str, dst: str, zone_id: str) -> None:
        await self._forward("rename", src=src, dst=dst, zone_id=zone_id)

    async def mkdir(self, path: str, zone_id: str) -> None:
        await self._forward("mkdir", path=path, zone_id=zone_id)

    async def access(self, path: str, zone_id: str) -> bool:
        return cast(bool, await self._forward("exists", path=path, zone_id=zone_id))

    async def count_dir(self, path: str, zone_id: str) -> int:
        return cast(int, await self._forward("count_dir", path=path, zone_id=zone_id))

    async def rename(self, src: str, dst: str, zone_id: str) -> None:
        """VFSOperations-compatible alias for sys_rename."""
        await self.sys_rename(src, dst, zone_id)

    async def sys_unlink(self, path: str, zone_id: str) -> None:
        """Delete a file at the given path via remote kernel."""
        await self._forward("sys_unlink", path=path, zone_id=zone_id)

    async def file_mtime(self, path: str, zone_id: str) -> "datetime | None":
        """Return server-observed mtime via the ``sys_stat`` RPC (modified_at field).

        ``sys_stat`` is an existing RPC in the server dispatch table (mapped to
        ``handle_get_metadata``). Its response includes ``modified_at`` set by
        the kernel at write time — server-controlled, not sender-influenced.

        Returns ``None`` when the stat call fails or ``modified_at`` is absent.
        Callers treat ``None`` as a safe-fail: retention is skipped.
        """
        try:
            result = await self._forward("sys_stat", path=path, zone_id=zone_id)
            if not isinstance(result, dict):
                return None
            metadata = result.get("metadata") or result
            if not isinstance(metadata, dict):
                return None
            modified_at = metadata.get("modified_at")
            if modified_at is None:
                return None
            if isinstance(modified_at, datetime):
                return modified_at
            if isinstance(modified_at, str):
                return datetime.fromisoformat(modified_at)
        except Exception:
            pass
        return None


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
