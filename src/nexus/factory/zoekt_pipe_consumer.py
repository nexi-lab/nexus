"""DT_PIPE-backed Zoekt index notification consumer.

Replaces the sync ``on_write_callback`` → ``ZoektIndexManager.notify_write()``
chain with DT_PIPE kernel IPC, decoupling the ObjectStore write hot path
from Zoekt's threading.Lock + threading.Timer debounce machinery.

Issue #810: Decouple Zoekt on_write_callback sync from ObjectStore write path.
Issue #808: Follows WorkflowDispatchService DT_PIPE pattern.

Architecture:
    CASLocalBackend.write_content() (sync)
      -> ZoektPipeConsumer.notify_write(path)
        -> pipe_write_nowait()  # ~5us, replaces lock acquisition + timer cancel

    Background consumer (async)
      -> _consume() loop
        -> pipe_read() (async, blocking)
        -> accumulate paths in set
        -> debounce via asyncio.wait_for timeout
        -> trigger_reindex_async() on ZoektIndexManager
"""

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.search.zoekt_client import ZoektIndexManager
    from nexus.system_services.pipe_manager import PipeManager

logger = logging.getLogger(__name__)

# Pipe path and capacity
_ZOEKT_PIPE_PATH = "/nexus/pipes/zoekt-writes"
_ZOEKT_PIPE_CAPACITY = 65_536  # 64KB


class ZoektPipeConsumer:
    """DT_PIPE consumer for Zoekt index notifications.

    Provides ``notify_write(path)`` and ``notify_sync_complete(files_synced)``
    as sync callbacks for CASLocalBackend. Events are written into a DT_PIPE
    ring buffer (~5us). A background consumer accumulates paths and triggers
    ``trigger_reindex_async()`` after a debounce window.

    Lifecycle:
        1. Created in factory (brick tier) with zoekt_index_manager
        2. PipeManager injected via set_pipe_manager() (deferred)
        3. start() creates pipe, spawns consumer
        4. stop() cancels consumer
    """

    def __init__(
        self,
        zoekt_index_manager: "ZoektIndexManager",
        *,
        debounce_seconds: float | None = None,
    ) -> None:
        self._zoekt = zoekt_index_manager
        self._debounce = debounce_seconds or zoekt_index_manager.debounce_seconds

        # Pipe state (deferred injection)
        self._pipe_manager: "PipeManager | None" = None
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def set_pipe_manager(self, pm: "PipeManager") -> None:
        """Inject PipeManager after factory boot."""
        self._pipe_manager = pm

    # ------------------------------------------------------------------
    # Sync callbacks (replace on_write_callback / on_sync_callback)
    # ------------------------------------------------------------------

    def notify_write(self, path: str) -> None:
        """Notify that a file was written. Used as on_write_callback."""
        if self._pipe_manager is not None and self._pipe_ready:
            from nexus.core.pipe import PipeFullError

            try:
                data = json.dumps({"type": "write", "path": path}).encode()
                self._pipe_manager.pipe_write_nowait(_ZOEKT_PIPE_PATH, data)
                return
            except PipeFullError:
                logger.warning("Zoekt pipe full, dropping write notification: %s", path)
                return

        # Fallback: direct call (CLI mode or pre-startup)
        self._zoekt.notify_write(path)

    def notify_sync_complete(self, files_synced: int = 0) -> None:
        """Notify that a sync completed. Used as on_sync_callback."""
        if self._pipe_manager is not None and self._pipe_ready:
            from nexus.core.pipe import PipeFullError

            try:
                data = json.dumps({"type": "sync", "files_synced": files_synced}).encode()
                self._pipe_manager.pipe_write_nowait(_ZOEKT_PIPE_PATH, data)
                return
            except PipeFullError:
                logger.warning("Zoekt pipe full, dropping sync notification")
                return

        # Fallback: direct call
        self._zoekt.notify_sync_complete(files_synced)

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create zoekt pipe and spawn consumer."""
        if self._pipe_ready:
            return

        if self._pipe_manager is None:
            return  # CLI mode

        from nexus.core.pipe import PipeError

        try:
            self._pipe_manager.create(
                _ZOEKT_PIPE_PATH,
                capacity=_ZOEKT_PIPE_CAPACITY,
                owner_id="kernel",
            )
        except PipeError:
            self._pipe_manager.open(_ZOEKT_PIPE_PATH, capacity=_ZOEKT_PIPE_CAPACITY)

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())

    async def stop(self) -> None:
        """Cancel consumer task."""
        if self._consumer_task is not None and not self._consumer_task.done():
            self._consumer_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._consumer_task
            self._consumer_task = None
        self._pipe_ready = False

    # ------------------------------------------------------------------
    # Background consumer with debounce
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background consumer: read from pipe, debounce, trigger reindex."""
        from nexus.core.pipe import PipeClosedError, PipeEmptyError, PipeNotFoundError

        assert self._pipe_manager is not None

        pipe_mgr = self._pipe_manager
        pending_paths: set[str] = set()
        has_sync = False

        while True:
            # If nothing pending, block until first event
            if not pending_paths and not has_sync:
                try:
                    first = await pipe_mgr.pipe_read(_ZOEKT_PIPE_PATH)
                except (PipeClosedError, PipeNotFoundError):
                    logger.debug("Zoekt pipe closed, consumer exiting")
                    break
                msg = json.loads(first)
                if msg["type"] == "write":
                    pending_paths.add(msg["path"])
                elif msg["type"] == "sync":
                    has_sync = True

            # Debounce: drain events for debounce_seconds
            deadline = asyncio.get_event_loop().time() + self._debounce
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(
                        pipe_mgr.pipe_read(_ZOEKT_PIPE_PATH),
                        timeout=remaining,
                    )
                    msg = json.loads(data)
                    if msg["type"] == "write":
                        pending_paths.add(msg["path"])
                    elif msg["type"] == "sync":
                        has_sync = True
                except TimeoutError:
                    break
                except (PipeClosedError, PipeNotFoundError):
                    break
                except PipeEmptyError:
                    break

            # Trigger reindex
            if pending_paths or has_sync:
                count = len(pending_paths)
                pending_paths.clear()
                has_sync = False
                try:
                    await self._zoekt.trigger_reindex_async()
                    logger.debug("Zoekt reindex triggered (%d pending paths)", count)
                except Exception as e:
                    logger.error("Zoekt reindex failed: %s", e)
