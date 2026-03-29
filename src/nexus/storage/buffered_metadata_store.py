"""Buffered metadata store with deferred write-back and batched Raft flush.

Wraps any MetastoreABC implementation (typically RaftMetadataStore) to add
write-back buffering for ``consistency="wb"`` writes.  Metadata updates are
held in a local in-memory buffer and flushed to the underlying store in
batches by a background thread, amortising the per-write Raft consensus
cost from ~5-10 ms down to ~50-100 μs.

Read-path consistency is maintained transparently: every ``get()`` checks the
pending buffer before hitting the underlying engine, so callers always see
the most recent metadata — even if it has not yet been committed to Raft.

Architecture decision references (Issue #3393):
    A1 — Store-level intercept on get() for buffer overlay
    A2 — EC mode write tokens for durability (Raft log is the WAL)
    P1 — Lock-free fast path via _has_pending bool flag
    P2 — Buffer enqueue inside VFS lock (caller responsibility)
"""

from __future__ import annotations

import builtins
import logging
from collections import deque
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC
from nexus.lib.deferred_buffer import DeferredBuffer

logger = logging.getLogger(__name__)


class DeferredMetadataBuffer(DeferredBuffer):
    """Background flush worker for buffered metadata writes.

    Accumulates FileMetadata entries and flushes them to the underlying
    metastore via put_batch() at a configurable interval.
    """

    def __init__(
        self,
        store: MetastoreABC,
        *,
        flush_interval_sec: float = 0.1,
        max_batch_size: int = 100,
        max_retries: int = 3,
    ):
        super().__init__(
            flush_interval_sec=flush_interval_sec,
            max_batch_size=max_batch_size,
            max_retries=max_retries,
            thread_name="DeferredMetadataBuffer-Flush",
        )
        self._store = store
        self._pending: deque[FileMetadata] = deque()
        self._retry_counts: dict[str, int] = {}  # path -> attempt count
        self._total_metadata_flushed = 0
        # Tombstone set: paths deleted while items may be in-flight in the
        # flush thread.  Checked in _flush_items() to prevent resurrecting
        # files that were deleted between _drain_items() and put_batch().
        self._deleted_paths: set[str] = set()

    # ── Public enqueue ──

    def enqueue(self, metadata: FileMetadata) -> None:
        """Buffer a metadata entry for deferred flush (non-blocking).

        If a pending entry already exists for the same path, the newer
        entry replaces it (last-writer-wins within the buffer).
        """
        with self._lock:
            # Last-writer-wins: remove any existing entry for this path
            self._pending = deque(m for m in self._pending if m.path != metadata.path)
            self._pending.append(metadata)
            queue_size = len(self._pending)
            # Clear tombstone — a new write after delete is intentional re-create
            self._deleted_paths.discard(metadata.path)

        self._check_batch_overflow(queue_size)

    def get_pending(self, path: str) -> FileMetadata | None:
        """Check if a path has a pending (unflushed) metadata entry.

        Called by BufferedMetadataStore.get() to overlay buffered data.
        """
        with self._lock:
            for m in reversed(self._pending):
                if m.path == path:
                    return m
        return None

    def remove_pending(self, path: str) -> None:
        """Remove a path from the pending buffer and add a tombstone.

        Prevents stale buffer entries from shadowing a delete, and
        prevents the flush thread from resurrecting a file that was
        deleted after _drain_items() but before put_batch().
        """
        with self._lock:
            self._pending = deque(m for m in self._pending if m.path != path)
            self._deleted_paths.add(path)

    # ── DeferredBuffer abstract methods ──

    def _has_items(self) -> bool:
        return bool(self._pending)

    def _drain_items(self) -> list[Any]:
        items = list(self._pending)
        self._pending.clear()
        return items

    def _flush_items(self, items: list[Any], *, catch_unexpected: bool) -> int:
        metadata_list: list[FileMetadata] = items

        # Filter out items whose paths were deleted after drain but before
        # flush (tombstone check).  Prevents resurrecting deleted files.
        with self._lock:
            if self._deleted_paths:
                metadata_list = [m for m in metadata_list if m.path not in self._deleted_paths]
                # Clear tombstones — they've served their purpose for this batch
                self._deleted_paths.clear()

        if not metadata_list:
            return 0

        retryable_errors: tuple[type[BaseException], ...]
        if catch_unexpected:
            retryable_errors = (Exception,)
        else:
            retryable_errors = (RuntimeError, TimeoutError, OSError)

        try:
            self._store.put_batch(
                metadata_list,
                consistency="sc",
                skip_snapshot=True,
            )
            flushed = len(metadata_list)
            self._total_metadata_flushed += flushed
            # Clear retry counts on success
            for m in metadata_list:
                self._retry_counts.pop(m.path, None)
            return flushed
        except retryable_errors as e:
            # Re-queue with retry tracking; dead-letter on max retries
            requeue: list[FileMetadata] = []
            for m in metadata_list:
                count = self._retry_counts.get(m.path, 0) + 1
                if count >= self._max_retries:
                    logger.error(
                        "Metadata item dead-lettered after %d retries: path=%s, error=%s",
                        count,
                        m.path,
                        e,
                    )
                    self._dead_letter_item(
                        "metadata",
                        {"path": m.path, "version": m.version},
                        e,
                        count,
                    )
                    self._retry_counts.pop(m.path, None)
                else:
                    logger.warning(
                        "Metadata flush failed (attempt %d/%d), re-queueing: path=%s, error=%s",
                        count,
                        self._max_retries,
                        m.path,
                        e,
                    )
                    self._retry_counts[m.path] = count
                    requeue.append(m)
            if requeue:
                with self._lock:
                    # Prepend requeued items so they flush first next cycle
                    self._pending.extendleft(reversed(requeue))
            return 0

    def _get_item_stats(self) -> dict[str, Any]:
        with self._lock:
            pending = len(self._pending)
        return {
            "pending_metadata": pending,
            "total_metadata_flushed": self._total_metadata_flushed,
        }


class BufferedMetadataStore(MetastoreABC):
    """MetastoreABC wrapper that adds write-back buffering.

    Intercepts ``put(consistency="wb")`` to buffer metadata locally for
    batched Raft flush.  All other consistency modes pass through to the
    underlying store unchanged.

    ``get()`` transparently checks the pending buffer first (P1 fast-path:
    skips lock acquisition when no items are pending).
    """

    def __init__(
        self,
        inner: MetastoreABC,
        *,
        flush_interval_sec: float = 0.1,
        max_batch_size: int = 100,
        max_retries: int = 3,
    ):
        super().__init__()
        self._inner = inner
        self._buffer = DeferredMetadataBuffer(
            inner,
            flush_interval_sec=flush_interval_sec,
            max_batch_size=max_batch_size,
            max_retries=max_retries,
        )
        # P1: Lock-free fast path — skip buffer check when nothing is pending.
        # A stale True is harmless (extra dict check); stale False can't happen
        # because we set True before enqueue returns.
        self._has_pending = False

    # ── Lifecycle ──

    async def start(self) -> None:
        """Start the background metadata flush worker."""
        await self._buffer.start()

    async def stop(self) -> None:
        """Stop the buffer and flush remaining metadata."""
        await self._buffer.stop()

    def _start_sync(self) -> None:
        self._buffer._start_sync()

    def _stop_sync(self) -> None:
        self._buffer._stop_sync()

    # ── Raw methods (required by MetastoreABC, delegate to inner) ──

    def _get_raw(self, path: str) -> FileMetadata | None:
        return self._inner._get_raw(path)

    def _put_raw(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        return self._inner._put_raw(metadata, consistency=consistency)

    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        return self._inner._delete_raw(path, consistency=consistency)

    def _exists_raw(self, path: str) -> bool:
        return self._inner._exists_raw(path)

    def _list_raw(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> builtins.list[FileMetadata]:
        return self._inner._list_raw(prefix, recursive, **kwargs)

    # ── Overridden public methods (buffer overlay) ──

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata, checking the pending buffer first.

        P1 fast-path: if _has_pending is False (common case for reads),
        skip the lock + dict lookup entirely (~1ns vs ~100ns).
        """
        if self._has_pending:
            pending = self._buffer.get_pending(path)
            if pending is not None:
                return pending
        return self._inner.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store metadata — buffers locally for ``consistency="wb"``.

        WB mode: enqueues into the deferred buffer for batched flush.
        SC/EC mode: passes through to the underlying store.
        """
        if consistency == "wb":
            self._has_pending = True
            self._buffer.enqueue(metadata)
            return None
        return self._inner.put(metadata, consistency=consistency)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        # Evict from pending buffer so stale entries don't shadow the delete
        if self._has_pending:
            self._buffer.remove_pending(path)
            self._has_pending = bool(self._buffer._pending)
        return self._inner.delete(path, consistency=consistency)

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit directory, including buffered entries."""
        # Check inner store first
        if hasattr(self._inner, "is_implicit_directory") and self._inner.is_implicit_directory(
            path
        ):
            return True
        # Check pending buffer for files under this path
        if self._has_pending:
            prefix = path.rstrip("/") + "/"
            with self._buffer._lock:
                return any(m.path.startswith(prefix) for m in self._buffer._pending)
        return False

    def exists(self, path: str) -> bool:
        if self._has_pending:
            pending = self._buffer.get_pending(path)
            if pending is not None:
                return True
        return self._inner.exists(path)

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        result = self._inner.list(prefix, recursive, **kwargs)
        if self._has_pending:
            result = self._merge_pending_into_list(result, prefix, recursive)
        return result

    def list_iter(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> Iterator[FileMetadata]:
        # Materialize to merge buffered entries, then yield
        yield from self.list(prefix, recursive, **kwargs)

    def _merge_pending_into_list(
        self,
        committed: builtins.list[FileMetadata],
        prefix: str,
        recursive: bool,
    ) -> builtins.list[FileMetadata]:
        """Merge buffered entries into a list result from the inner store."""
        with self._buffer._lock:
            pending = builtins.list(self._buffer._pending)
        if not pending:
            return committed

        # Build a dict keyed by path for O(1) override
        by_path: dict[str, FileMetadata] = {m.path: m for m in committed}
        for m in pending:
            if not m.path.startswith(prefix):
                continue
            if not recursive:
                rel = m.path[len(prefix) :].lstrip("/")
                if "/" in rel:
                    continue
            by_path[m.path] = m
        return sorted(by_path.values(), key=lambda meta: meta.path)

    def close(self) -> None:
        # Flush remaining items before closing
        self._buffer._stop_sync()
        self._inner.close()

    # ── Batch operations (delegate to inner) ──

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        result = self._inner.get_batch(paths)
        if self._has_pending:
            for path in paths:
                pending = self._buffer.get_pending(path)
                if pending is not None:
                    result[path] = pending
        return result

    def put_batch(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",
        skip_snapshot: bool = False,
    ) -> None:
        self._inner.put_batch(metadata_list, consistency=consistency, skip_snapshot=skip_snapshot)

    def delete_batch(self, paths: Sequence[str]) -> None:
        if self._has_pending:
            for path in paths:
                self._buffer.remove_pending(path)
            self._has_pending = bool(self._buffer._pending)
        self._inner.delete_batch(paths)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        return self._inner.batch_get_content_ids(paths)

    def is_committed(self, token: int) -> str | None:
        return self._inner.is_committed(token)

    # ── Buffer-specific API ──

    def flush(self) -> None:
        """Force an immediate flush of all pending metadata."""
        self._buffer.flush()
        self._has_pending = bool(self._buffer._pending)

    def get_buffer_stats(self) -> dict[str, Any]:
        """Get buffer statistics."""
        return self._buffer.get_stats()

    def get_dead_letter(self) -> builtins.list[dict[str, Any]]:
        """Return the dead-letter queue for inspection."""
        return self._buffer.get_dead_letter()

    # ── Proxy attributes from inner store ──

    def __getattr__(self, name: str) -> Any:
        """Forward unknown attributes to the inner store.

        Allows BufferedMetadataStore to be a drop-in replacement for
        RaftMetadataStore — methods like is_leader(), zone_id, etc.
        are transparently proxied.
        """
        return getattr(self._inner, name)
