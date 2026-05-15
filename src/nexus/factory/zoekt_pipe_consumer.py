"""DT_PIPE-backed Zoekt index notification consumer.

Replaces the sync ``on_write_callback`` → ``ZoektIndexManager.notify_write()``
chain with DT_PIPE kernel IPC via sys_write/sys_read, decoupling the
ObjectStore write hot path from Zoekt's threading.Lock + timer debounce.

Issue #810: Decouple Zoekt on_write_callback sync from ObjectStore write path.
Issue #1772: Migrated to sys_write/sys_read backed by the Rust kernel pipe registry.

Architecture:
    CASLocalBackend.write_content() (sync)
      -> ZoektPipeConsumer.notify_write(path)
        -> deque buffer → flush task → sys_write  # decoupled

    Background consumer (async)
      -> _consume() loop
        -> sys_read() (async, blocking)
        -> accumulate paths in set
        -> debounce via asyncio.wait_for timeout
        -> trigger_reindex_async() on ZoektIndexManager
"""

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nexus.bricks.search.zoekt_client import ZoektIndexManager
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# Pipe path and capacity
_ZOEKT_PIPE_PATH = "/nexus/pipes/zoekt-writes"
_ZOEKT_PIPE_CAPACITY = 65_536  # 64KB


class ZoektPipeConsumer:
    """DT_PIPE consumer for Zoekt index notifications via NexusFS syscalls.

    Provides ``notify_write(path)`` and ``notify_sync_complete(files_synced)``
    as sync callbacks for CASLocalBackend. Events are buffered in a deque
    and flushed asynchronously via ``sys_write``. A background consumer
    reads via ``sys_read`` and triggers ``trigger_reindex_async()``.

    Lifecycle:
        1. Created in factory (brick tier) with zoekt_index_manager
        2. NexusFS bound via bind_fs() (deferred)
        3. start() creates pipe via sys_setattr, spawns consumer + flush task
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

        # NexusFS reference (deferred injection)
        self._nx: "NexusFS | None" = None
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None

        # Sync-to-async bridge: sync callers buffer here, flush task drains via sys_write
        self._write_buffer: deque[bytes] = deque(maxlen=10_000)

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def bind_fs(self, nx: "NexusFS") -> None:
        """Bind NexusFS for sys_read/sys_write pipe access."""
        self._nx = nx

    # ------------------------------------------------------------------
    # Sync callbacks (replace on_write_callback / on_sync_callback)
    # ------------------------------------------------------------------

    def notify_write(self, path: str) -> None:
        """Notify that a file was written. Used as on_write_callback."""
        if self._nx is not None and self._pipe_ready:
            data = json.dumps({"type": "write", "path": path}).encode()
            self._write_buffer.append(data)
            return

        # Fallback: direct call (CLI mode, pre-startup)
        self._zoekt.notify_write(path)

    def notify_sync_complete(self, files_synced: int = 0) -> None:
        """Notify that a sync completed. Used as on_sync_callback."""
        if self._nx is not None and self._pipe_ready:
            data = json.dumps({"type": "sync", "files_synced": files_synced}).encode()
            self._write_buffer.append(data)
            return

        # Fallback: direct call (CLI mode, pre-startup)
        self._zoekt.notify_sync_complete(files_synced)

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create zoekt pipe via sys_setattr and spawn consumer + flush task."""
        if self._pipe_ready:
            return

        if self._nx is None:
            return  # CLI mode

        # Create the pipe via the public syscall — the Rust kernel router
        # picks up DT_PIPE entry_type from the metastore.
        self._nx.sys_setattr(_ZOEKT_PIPE_PATH)

        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Graceful shutdown: cancel flush task, signal pipe closed, drain consumer."""
        # Stop flush task first
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

        if self._consumer_task is not None and not self._consumer_task.done():
            # Signal close via sys_unlink
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    self._nx.sys_unlink(_ZOEKT_PIPE_PATH)

            try:
                await asyncio.wait_for(asyncio.shield(self._consumer_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task

            self._consumer_task = None
        self._pipe_ready = False

    # ------------------------------------------------------------------
    # Flush loop: drain sync buffer → sys_write
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        """Background task: drain _write_buffer into pipe via sys_write."""
        assert self._nx is not None
        nx = self._nx

        while True:
            if self._write_buffer:
                while self._write_buffer:
                    data = self._write_buffer.popleft()
                    try:
                        nx.sys_write(_ZOEKT_PIPE_PATH, data)
                    except Exception:
                        logger.warning("Zoekt pipe write failed, dropping event")
            await asyncio.sleep(0.01)  # 10ms poll interval

    # ------------------------------------------------------------------
    # Background consumer with debounce
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        """Background consumer: read from pipe via sys_read, debounce, trigger reindex."""
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None

        nx = self._nx
        pending_paths: set[str] = set()
        has_sync = False

        while True:
            # If nothing pending, block until first event (in thread to avoid blocking event loop)
            if not pending_paths and not has_sync:
                try:
                    first = await asyncio.to_thread(nx.sys_read, _ZOEKT_PIPE_PATH)
                except NexusFileNotFoundError:
                    logger.debug("Zoekt pipe closed, consumer exiting")
                    break
                # DT_PIPE returns raw bytes (only DT_STREAM uses the dict shape).
                assert isinstance(first, bytes)
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
                    data = await asyncio.to_thread(nx.sys_read, _ZOEKT_PIPE_PATH)
                    assert isinstance(data, bytes)
                    msg = json.loads(data)
                    if msg["type"] == "write":
                        pending_paths.add(msg["path"])
                    elif msg["type"] == "sync":
                        has_sync = True
                except TimeoutError:
                    break
                except NexusFileNotFoundError:
                    break
                except Exception:
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
