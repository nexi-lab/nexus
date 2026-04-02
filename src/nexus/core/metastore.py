"""MetastoreABC — the Metastore storage pillar.

One of the Four Storage Pillars (NEXUS-LEGO-ARCHITECTURE.md §2.0):
  - MetastoreABC  (required at boot — this file)
  - ObjectStoreABC (mounted post-init — backends/backend.py)
  - RecordStoreABC (services-only — storage/record_store.py)
  - CacheStoreABC  (optional — core/cache_store.py)

MetastoreABC is the kernel's inode layer: the typed contract between
VFS and the underlying ordered KV store.  The kernel cannot describe
files without it.  Linux analogue: ``struct inode_operations``.

Includes a built-in dcache (dentry cache): an in-process dict that
caches deserialized FileMetadata objects. Point lookups via ``get()``
hit the dict (~50ns) instead of the storage backend (~6μs for redb
FFI + protobuf decode). The cache is write-through and authoritative
(single-process, single-writer — no TTL or LRU needed).

Implementations:
  - RaftMetadataStore  (storage/raft_metadata_store.py)

SSOT: proto/nexus/core/metadata.proto defines the FileMetadata fields.
This ABC defines the *operations* over those fields.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata

# RUST_FALLBACK: RustDCache — DashMap dentry cache (Issue #1838)
_RustDCache: type | None
try:
    from nexus_fast import RustDCache as _RustDCache
except ImportError:
    _RustDCache = None


def _sync_to_rust(rust_dc: Any, meta: FileMetadata) -> None:
    """Push a FileMetadata into the Rust DashMap (hot-path projection).

    No-op when RustDCache is unavailable (nexus_fast not installed).
    Phase H: added mime_type for sys_stat acceleration.
    """
    if rust_dc is None:
        return
    rust_dc.put(
        meta.path,
        meta.backend_name,
        meta.physical_path,
        meta.size,
        meta.entry_type,
        meta.version,
        meta.etag,
        meta.zone_id,
        meta.mime_type,
    )


class MetastoreABC(ABC):
    """Abstract base class for metadata storage (the "Metastore" pillar).

    Stores mapping between virtual paths and backend physical locations.
    All metastore backends (Raft, Federated, etc.) implement this interface.

    Subclasses implement ``_get_raw``, ``_put_raw``, ``_delete_raw``,
    ``_exists_raw``, ``_list_raw``, and ``close``.  The public API
    (``get``, ``put``, ``delete``, etc.) adds an in-process dcache layer
    that eliminates repeated deserialization overhead.

    The Rust DashMap (``_rust_dcache``) mirrors the Python dict for hot-path
    fields only (backend_name, physical_path, size, etag, version, entry_type,
    zone_id).  It is dual-written on every mutation and consumed by
    SyscallEngine (#1817) for single-FFI sys_read/sys_write.

    Abstract methods (must override):
        _get_raw, _put_raw, _delete_raw, _exists_raw, _list_raw, close

    Concrete methods (may override for performance):
        is_committed, list_iter,
        _get_batch_raw, _delete_batch_raw, _put_batch_raw
    """

    def __init__(self) -> None:
        self._dcache: dict[str, FileMetadata] = {}
        self._dcache_hits: int = 0
        self._dcache_misses: int = 0
        self._rust_dcache: Any = _RustDCache() if _RustDCache is not None else None

    # ── Cached public API (signatures unchanged) ──────────────────────

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file (dcache-accelerated)."""
        cached = self._dcache.get(path)
        if cached is not None:
            self._dcache_hits += 1
            return cached
        self._dcache_misses += 1
        result = self._get_raw(path)
        if result is not None:
            self._dcache[path] = result
            _sync_to_rust(self._rust_dcache, result)
        return result

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store or update file metadata (write-through dcache).

        Args:
            metadata: File metadata to store.
            consistency: Consistency mode for the write:
                - ``"sc"`` — blocks until Raft commit. Returns None.
                - ``"ec"`` — fire-and-forget. Returns write token (int).
                  Use for low-latency writes where immediate durability
                  is not required.  Raft replicates in background.

        Returns:
            EC mode: write token (int) for polling via is_committed().
            SC mode: None (write is already committed when this returns).

        Note:
            Raft natively batches consecutive proposals into a single
            AppendEntries RPC (tikv/raft-rs), so application-level
            batching is unnecessary.  Use ``"ec"`` for throughput,
            ``"sc"`` for durability.
        """
        self._dcache[metadata.path] = metadata
        _sync_to_rust(self._rust_dcache, metadata)
        return self._put_raw(metadata, consistency=consistency)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata (evicts dcache entry)."""
        self._dcache.pop(path, None)
        if self._rust_dcache is not None:
            self._rust_dcache.evict(path)
        return self._delete_raw(path, consistency=consistency)

    def dcache_evict_prefix(self, prefix: str) -> int:
        """Evict all dcache entries whose path starts with *prefix*.

        Used by mount/unmount operations to invalidate stale cross-zone
        cache entries that were resolved through a now-changed mount point.

        Returns the number of entries evicted.
        """
        keys = [k for k in self._dcache if k.startswith(prefix)]
        for k in keys:
            del self._dcache[k]
        if self._rust_dcache is not None:
            self._rust_dcache.evict_prefix(prefix)
        return len(keys)

    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path (dcache-accelerated)."""
        if path in self._dcache:
            return True
        return self._exists_raw(path)

    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> builtins.list[FileMetadata]:
        """List all files with given path prefix (populates dcache)."""
        results = self._list_raw(prefix, recursive, **kwargs)
        rust_dc = self._rust_dcache
        for meta in results:
            self._dcache[meta.path] = meta
            _sync_to_rust(rust_dc, meta)
        return results

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix (populates dcache).

        Memory-efficient alternative to list(). Yields results one at a time.
        Subclasses may override ``_list_raw`` for true streaming.
        """
        rust_dc = self._rust_dcache
        for meta in self._list_raw(prefix, recursive, **kwargs):
            self._dcache[meta.path] = meta
            _sync_to_rust(rust_dc, meta)
            yield meta

    # ── Batch operations (dcache-aware) ───────────────────────────────

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files (partial dcache hits)."""
        result: dict[str, FileMetadata | None] = {}
        misses: list[str] = []
        for p in paths:
            cached = self._dcache.get(p)
            if cached is not None:
                result[p] = cached
                self._dcache_hits += 1
            else:
                misses.append(p)
                self._dcache_misses += 1
        if misses:
            raw = self._get_batch_raw(misses)
            rust_dc = self._rust_dcache
            for p, meta in raw.items():
                if meta is not None:
                    self._dcache[p] = meta
                    _sync_to_rust(rust_dc, meta)
                result[p] = meta
        return result

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files (evicts dcache entries)."""
        rust_dc = self._rust_dcache
        for p in paths:
            self._dcache.pop(p, None)
            if rust_dc is not None:
                rust_dc.evict(p)
        self._delete_batch_raw(paths)

    def put_batch(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",
        skip_snapshot: bool = False,
    ) -> None:
        """Store or update multiple file metadata (write-through dcache).

        Args:
            metadata_list: List of file metadata to store.
            consistency: Consistency mode (see put() for details).
            skip_snapshot: Skip pre-write snapshot for rollback. Use when
                the caller has its own retry logic (e.g., deferred buffer).
        """
        rust_dc = self._rust_dcache
        for meta in metadata_list:
            self._dcache[meta.path] = meta
            _sync_to_rust(rust_dc, meta)
        self._put_batch_raw(metadata_list, consistency=consistency, skip_snapshot=skip_snapshot)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths (dcache-accelerated)."""
        result: dict[str, str | None] = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.etag if metadata else None
        return result

    # ── Consistency (no cache interaction) ────────────────────────────

    def is_committed(self, _token: int) -> str | None:
        """Check if an EC write token has been replicated to a majority.

        Args:
            token: Write token returned by put() with consistency="ec".

        Returns:
            "committed" — replicated to majority.
            "pending" — local only, awaiting replication.
            None — invalid token or no replication log.
        """
        return None

    # ── Observability ─────────────────────────────────────────────────

    @property
    def cache_stats(self) -> dict[str, Any]:
        """Return dcache hit/miss/size statistics."""
        return {
            "hits": self._dcache_hits,
            "misses": self._dcache_misses,
            "size": len(self._dcache),
            "rust": self._rust_dcache.stats() if self._rust_dcache is not None else {},
        }

    # ── Abstract raw methods (subclasses implement these) ─────────────

    @abstractmethod
    def _get_raw(self, path: str) -> FileMetadata | None:
        """Get metadata from the underlying store (no cache)."""

    @abstractmethod
    def _put_raw(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store metadata in the underlying store (no cache)."""

    @abstractmethod
    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete metadata from the underlying store (no cache)."""

    @abstractmethod
    def _exists_raw(self, path: str) -> bool:
        """Check existence in the underlying store (no cache)."""

    @abstractmethod
    def _list_raw(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> builtins.list[FileMetadata]:
        """List metadata from the underlying store (no cache)."""

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""

    # ── Batch raw (concrete defaults, override for performance) ───────

    def _get_batch_raw(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple paths from the underlying store."""
        return {p: self._get_raw(p) for p in paths}

    def _delete_batch_raw(self, paths: Sequence[str]) -> None:
        """Delete multiple paths from the underlying store."""
        for p in paths:
            self._delete_raw(p)

    def _put_batch_raw(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",  # noqa: ARG002
        skip_snapshot: bool = False,  # noqa: ARG002
    ) -> None:
        """Store multiple metadata entries in the underlying store."""
        for meta in metadata_list:
            self._put_raw(meta)
