"""WALStreamBackend — durable DT_STREAM backed by EC WAL.

Each append is an EC metadata write; replication happens via the existing
Raft transport loop.  Follows the RemoteStreamBackend pattern (adapter
over transport) and the StreamBackend protocol (§4.2).

Usage:
    Mount a backend with ``stream_backend_factory=wal_stream_factory``:

    >>> def wal_stream_factory(path: str, capacity: int) -> WALStreamBackend:
    ...     return WALStreamBackend(metastore, stream_id=path)
    >>> mount_table.add("/durable-ipc", backend, stream_backend_factory=wal_stream_factory)
    >>> # sys_setattr("/durable-ipc/channel", entry_type=DT_STREAM) → WALStreamBackend
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.metastore import MetastoreABC


class WALStreamBackend:
    """Durable StreamBackend backed by EC WAL for cross-node at-least-once delivery.

    Each ``write_nowait(data)`` stores data as an EC metadata entry under
    ``/__wal_stream__/<stream_id>/<seq>``.  The EC WAL replicates entries
    to peers via the existing transport loop (no new Rust code needed).

    ``read_at(offset)`` reads back by sequence number.

    Limitations:
        - Each entry is a ``Command::SetMetadata`` — IPC payloads stored as
          "metadata".  A dedicated IPC entry type in the Rust WAL would be
          better long-term.
        - Sequential read only (no random-access within a message).
    """

    __slots__ = (
        "_metastore",
        "_stream_id",
        "_prefix",
        "_next_seq",
        "_closed",
        "_lock",
        "_write_event",
    )

    def __init__(self, metastore: "MetastoreABC", stream_id: str) -> None:
        self._metastore = metastore
        self._stream_id = stream_id
        self._prefix = f"/__wal_stream__/{stream_id}/"
        self._next_seq = 0
        self._closed = False
        self._lock = threading.Lock()
        self._write_event = asyncio.Event()

    # ── Write ──────────────────────────────────────────────────────

    def write_nowait(self, data: bytes) -> int:
        """Append data to WAL stream.  Returns sequence number as offset."""
        if self._closed:
            raise RuntimeError(f"WAL stream {self._stream_id} is closed")
        with self._lock:
            seq = self._next_seq
            key = f"{self._prefix}{seq}"
            self._metastore.put_raw(key, data) if hasattr(
                self._metastore, "put_raw"
            ) else self._metastore.put_kv(key, data) if hasattr(
                self._metastore, "put_kv"
            ) else self._set_metadata_ec(key, data)
            self._next_seq = seq + 1
        # Wake blocked readers
        self._write_event.set()
        self._write_event.clear()
        return seq

    async def write(self, data: bytes, *, blocking: bool = True) -> int:
        """Async write — delegates to write_nowait (WAL append is always sync)."""
        _ = blocking  # WAL append is sync; param kept for StreamBackend protocol
        return self.write_nowait(data)

    # ── Read ───────────────────────────────────────────────────────

    def read_at(self, byte_offset: int = 0) -> tuple[bytes, int]:
        """Read entry at sequence *byte_offset*.  Non-destructive."""
        key = f"{self._prefix}{byte_offset}"
        data = self._get_metadata(key)
        if data is None:
            if self._closed:
                raise StopIteration(f"WAL stream {self._stream_id} closed at seq {byte_offset}")
            return (b"", byte_offset)  # No data yet
        return (data, byte_offset + 1)

    async def read(self, byte_offset: int = 0, *, blocking: bool = True) -> tuple[bytes, int]:
        """Async read — blocks until data available if *blocking*."""
        data, next_off = self.read_at(byte_offset)
        if data or not blocking:
            return (data, next_off)
        # Block until writer appends
        while not self._closed:
            self._write_event.clear()
            data, next_off = self.read_at(byte_offset)
            if data:
                return (data, next_off)
            try:
                await asyncio.wait_for(self._write_event.wait(), timeout=1.0)
            except TimeoutError:
                continue
        return self.read_at(byte_offset)

    def read_batch(self, byte_offset: int = 0, count: int = 10) -> tuple[list[bytes], int]:
        """Read up to *count* entries starting at *byte_offset*."""
        items: list[bytes] = []
        off = byte_offset
        for _ in range(count):
            data, next_off = self.read_at(off)
            if not data:
                break
            items.append(data)
            off = next_off
        return (items, off)

    async def read_batch_blocking(
        self, byte_offset: int = 0, count: int = 10, *, blocking: bool = True
    ) -> tuple[list[bytes], int]:
        """Async batch read."""
        items, off = self.read_batch(byte_offset, count)
        if items or not blocking:
            return (items, off)
        # Block for first item
        data, next_off = await self.read(byte_offset, blocking=True)
        if data:
            return ([data], next_off)
        return ([], byte_offset)

    # ── Lifecycle ──────────────────────────────────────────────────

    def close(self) -> None:
        self._closed = True
        self._write_event.set()  # Wake blocked readers

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "stream_id": self._stream_id,
            "next_seq": self._next_seq,
            "closed": self._closed,
            "backend": "wal",
        }

    @property
    def tail(self) -> int:
        return self._next_seq

    # ── Internal ───────────────────────────────────────────────────

    def _set_metadata_ec(self, key: str, data: bytes) -> None:
        """Store via metastore set_metadata with EC consistency."""
        from nexus.contracts.metadata import FileMetadata

        meta = FileMetadata(
            path=key,
            backend_name="wal_stream",
            physical_path="",
            size=len(data),
            etag=key,
        )
        # Store data as physical_path (small messages) or via custom KV
        # For now, use the metadata path with data encoded in physical_path
        meta = FileMetadata(
            path=key,
            backend_name="wal_stream",
            physical_path=data.hex(),  # Hex-encode bytes into physical_path
            size=len(data),
            etag=key,
        )
        self._metastore.put(meta, consistency="ec")

    def _get_metadata(self, key: str) -> bytes | None:
        """Read back data from metastore."""
        meta = self._metastore.get(key)
        if meta is None:
            return None
        # Decode hex from physical_path
        try:
            return bytes.fromhex(meta.physical_path)
        except (ValueError, AttributeError):
            return None
