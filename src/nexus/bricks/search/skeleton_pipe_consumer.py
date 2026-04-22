"""DT_PIPE-backed async consumer for document_skeleton indexing (Issue #3725).

Follows the ZoektPipeConsumer pattern:
    - Sync notify_write() / notify_delete() / notify_rename() buffer events into a deque.
    - A background flush task drains the deque into the NexusFS pipe via sys_write.
    - A background consumer reads from the pipe via sys_read and dispatches to
      SkeletonIndexer in micro-batches (15A: asyncio.gather, BATCH_SIZE at a time).

Key differences from ZoektPipeConsumer:
    - Each write event triggers a file read (2KB head), so micro-batching with
      asyncio.gather is critical for throughput.
    - Rename events must update the path + title atomically (delete old + index new).
    - Back-pressure: events are dropped with a WARNING log when the deque is full.

Issue #3725 review decisions honoured:
    - 4A  Async pipe consumer (not synchronous post-flush hook)
    - 13B Bootstrap from DB rows in SearchDaemon (not this module)
    - 15A Micro-batched concurrent reads via asyncio.gather
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.bricks.search.skeleton_indexer import SkeletonIndexer

logger = logging.getLogger(__name__)

_SKELETON_PIPE_PATH = "/nexus/pipes/skeleton-writes"
_SKELETON_PIPE_CAPACITY = 65_536  # 64KB

# Number of events processed concurrently in a single asyncio.gather call (15A).
BATCH_SIZE: int = 100


class SkeletonPipeConsumer:
    """DT_PIPE consumer for skeleton index notifications.

    Lifecycle:
        1. Created in factory (brick tier) with a SkeletonIndexer.
        2. NexusFS bound via bind_fs().
        3. start() creates pipe, spawns consumer + flush tasks.
        4. stop() gracefully drains and cancels.
    """

    def __init__(
        self,
        indexer: "SkeletonIndexer",
        *,
        debounce_seconds: float = 0.5,
        fallback_loop: Any | None = None,
    ) -> None:
        self._indexer = indexer
        self._debounce = debounce_seconds
        # Captured event loop for call_soon_threadsafe fallback when the pipe is
        # not ready and _buffer() is called from a synchronous thread
        # (asyncio.to_thread).  Set at construction time from the running loop.
        self._fallback_loop = fallback_loop

        self._nx: Any | None = None
        self._pipe_ready = False
        self._consumer_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None

        # Sync-to-async bridge: sync callers buffer here, flush task drains via sys_write.
        # maxlen matches ZoektPipeConsumer; events beyond this are dropped with a warning.
        self._write_buffer: deque[bytes] = deque(maxlen=10_000)

    # ------------------------------------------------------------------
    # Deferred injection
    # ------------------------------------------------------------------

    def bind_fs(self, nx: Any) -> None:
        """Bind NexusFS for sys_read/sys_write pipe access."""
        self._nx = nx

    # ------------------------------------------------------------------
    # Sync callbacks (called from RecordStoreWriteObserver / rename handler)
    # ------------------------------------------------------------------

    def notify_write(self, path: str, path_id: str | None, zone_id: str) -> None:
        """Buffer a write event for async processing.

        path_id may be None when called from a VFS hook (e.g. _SkeletonWriteHook
        in search.py lifespan) — SkeletonIndexer resolves it from DB in that case.
        """
        self._buffer({"type": "write", "path": path, "path_id": path_id, "zone_id": zone_id})

    def notify_delete(self, path: str, path_id: str | None, zone_id: str) -> None:
        """Buffer a delete event for async processing.  path_id may be None."""
        self._buffer({"type": "delete", "path": path, "path_id": path_id, "zone_id": zone_id})

    def notify_rename(
        self,
        old_path: str,
        new_path: str,
        path_id: str | None,
        zone_id: str,
    ) -> None:
        """Buffer a rename event: delete old skeleton row, index new path.

        path_id may be None when called from VFS hooks.
        """
        self._buffer(
            {
                "type": "rename",
                "old_path": old_path,
                "new_path": new_path,
                "path_id": path_id,
                "zone_id": zone_id,
            }
        )

    def _buffer(self, msg: dict[str, Any]) -> None:
        if self._nx is not None and self._pipe_ready:
            if len(self._write_buffer) >= (self._write_buffer.maxlen or 10_000):
                logger.warning(
                    "[SKELETON] write buffer full (%d), dropping event for %s",
                    len(self._write_buffer),
                    msg.get("path") or msg.get("new_path", "?"),
                )
            self._write_buffer.append(json.dumps(msg).encode())
            return
        # Fallback: VFS hooks fire from asyncio.to_thread (sync thread) — use
        # call_soon_threadsafe with the captured loop so the coroutine is safely
        # scheduled on the event loop without needing get_running_loop().
        loop = self._fallback_loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):  # loop closed at shutdown
                loop.call_soon_threadsafe(
                    loop.create_task,
                    self._dispatch_single(msg),
                )

    # ------------------------------------------------------------------
    # Async lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Create skeleton pipe and spawn consumer + flush tasks."""
        if self._pipe_ready:
            return
        if self._nx is None:
            return  # CLI mode — no pipe infrastructure

        # pipe_create is idempotent: creates on first run, no-ops if already exists.
        self._nx.pipe_create(_SKELETON_PIPE_PATH, capacity=_SKELETON_PIPE_CAPACITY)
        self._pipe_ready = True
        self._consumer_task = asyncio.create_task(self._consume())
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def stop(self) -> None:
        """Graceful shutdown: drain flush task, signal pipe closed, wait for consumer."""
        if self._flush_task is not None and not self._flush_task.done():
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

        if self._consumer_task is not None and not self._consumer_task.done():
            if self._nx is not None and self._pipe_ready:
                with contextlib.suppress(Exception):
                    # destroy_pipe signals consumer to exit (NexusFileNotFoundError)
                    self._nx._kernel.destroy_pipe(_SKELETON_PIPE_PATH)
            try:
                await asyncio.wait_for(asyncio.shield(self._consumer_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._consumer_task
            self._consumer_task = None

        self._pipe_ready = False

    # ------------------------------------------------------------------
    # Flush loop: drain sync buffer → pipe
    # ------------------------------------------------------------------

    async def _flush_loop(self) -> None:
        assert self._nx is not None
        nx = self._nx
        while True:
            while self._write_buffer:
                data = self._write_buffer.popleft()
                try:
                    # sys_write routes to Rust dcache for DT_PIPE — no Python
                    # metastore entry needed (Rust handles inline).
                    nx.sys_write(_SKELETON_PIPE_PATH, data)
                except Exception:
                    logger.warning("[SKELETON] pipe write failed, dropping event")
            await asyncio.sleep(0.01)  # 10ms poll

    # ------------------------------------------------------------------
    # Background consumer with debounce + micro-batching (15A)
    # ------------------------------------------------------------------

    async def _consume(self) -> None:
        # sys_read returns b"" for empty DT_PIPE (POSIX non-blocking semantics).
        # NexusFileNotFoundError signals pipe destroyed (stop() signal).
        from nexus.contracts.exceptions import NexusFileNotFoundError

        assert self._nx is not None
        nx = self._nx
        _POLL = 0.01  # 10ms poll interval

        pending: list[dict[str, Any]] = []

        while True:
            # Block until first event: poll with sleep
            if not pending:
                while True:
                    try:
                        data = nx.sys_read(_SKELETON_PIPE_PATH)
                    except NexusFileNotFoundError:
                        logger.debug("[SKELETON] pipe closed, consumer exiting")
                        return
                    except Exception:
                        return
                    if data:
                        pending.append(json.loads(data))
                        break
                    await asyncio.sleep(_POLL)

            # Debounce: drain additional events for debounce_seconds
            deadline = asyncio.get_event_loop().time() + self._debounce
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    data = nx.sys_read(_SKELETON_PIPE_PATH)
                except NexusFileNotFoundError:
                    break
                except Exception:
                    break
                if data:
                    pending.append(json.loads(data))
                else:
                    await asyncio.sleep(min(remaining, _POLL))

            # Process in micro-batches (15A)
            await self._process_batch(pending)
            pending.clear()

    async def _process_batch(self, events: list[dict[str, Any]]) -> None:
        """Dispatch events to SkeletonIndexer in micro-batches via asyncio.gather."""
        for i in range(0, len(events), BATCH_SIZE):
            batch = events[i : i + BATCH_SIZE]
            tasks = [self._dispatch_single(e) for e in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for j, r in enumerate(results):
                if isinstance(r, Exception):
                    logger.warning(
                        "[SKELETON] batch dispatch error for event %s: %s",
                        batch[j].get("path") or batch[j].get("new_path", "?"),
                        r,
                    )

    async def _dispatch_single(self, msg: dict[str, Any]) -> None:
        """Route a single event to the appropriate SkeletonIndexer method."""
        t = msg.get("type")
        try:
            if t == "write":
                await self._indexer.index_file(
                    path_id=msg["path_id"],
                    virtual_path=msg["path"],
                    zone_id=msg["zone_id"],
                )
            elif t == "delete":
                await self._indexer.delete_file(
                    path_id=msg["path_id"],
                    virtual_path=msg["path"],
                    zone_id=msg["zone_id"],
                )
            elif t == "rename":
                # Delete old path entry, index new path (10A: title preserved via
                # fresh extraction on the renamed file at its new path_id location).
                await self._indexer.delete_file(
                    path_id=msg["path_id"],
                    virtual_path=msg["old_path"],
                    zone_id=msg["zone_id"],
                )
                await self._indexer.index_file(
                    path_id=msg["path_id"],
                    virtual_path=msg["new_path"],
                    zone_id=msg["zone_id"],
                )
            else:
                logger.debug("[SKELETON] unknown event type: %s", t)
        except Exception as e:
            logger.warning("[SKELETON] dispatch error for %s: %s", t, e)
