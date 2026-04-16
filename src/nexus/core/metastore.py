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
import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)


def _sync_to_rust(kernel: Any, meta: FileMetadata) -> None:
    """Push a FileMetadata into the Rust DashMap (hot-path projection).

    Phase H: added mime_type for sys_stat acceleration.
    """
    if kernel is None:
        return
    kernel.dcache_put(
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

    The Rust DashMap (accessed via ``_kernel.dcache_*``) mirrors the Python
    dict for hot-path fields only (backend_name, physical_path, size, etag,
    version, entry_type, zone_id).  It is dual-written on every mutation and
    consumed by Kernel (#1817) for single-FFI sys_read/sys_write.

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
        self._kernel: Any = None  # late-bound; set after Kernel is created

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
            _sync_to_rust(self._kernel, result)
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
        _sync_to_rust(self._kernel, metadata)
        return self._put_raw(metadata, consistency=consistency)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata (evicts dcache entry)."""
        self._dcache.pop(path, None)
        if self._kernel is not None:
            self._kernel.dcache_evict(path)
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
        if self._kernel is not None:
            self._kernel.dcache_evict_prefix(prefix)
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
        kernel = self._kernel
        for meta in results:
            self._dcache[meta.path] = meta
            _sync_to_rust(kernel, meta)
        return results

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix (populates dcache).

        Memory-efficient alternative to list(). Yields results one at a time.
        Subclasses that define ``_list_iter_raw`` get true streaming;
        otherwise falls back to iterating over ``_list_raw``.
        """
        # Issue #3706: dispatch to _list_iter_raw for true streaming when
        # subclass provides it (e.g. RaftMetadataStore, SQLiteMetastore).
        raw_iter = getattr(self, "_list_iter_raw", None)
        source = (
            raw_iter(prefix, recursive, **kwargs)
            if raw_iter is not None
            else self._list_raw(prefix, recursive, **kwargs)
        )
        kernel = self._kernel
        for meta in source:
            self._dcache[meta.path] = meta
            _sync_to_rust(kernel, meta)
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
            kernel = self._kernel
            for p, meta in raw.items():
                if meta is not None:
                    self._dcache[p] = meta
                    _sync_to_rust(kernel, meta)
                result[p] = meta
        return result

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files (evicts dcache entries)."""
        kernel = self._kernel
        for p in paths:
            self._dcache.pop(p, None)
            if kernel is not None:
                kernel.dcache_evict(p)
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
        kernel = self._kernel
        for meta in metadata_list:
            self._dcache[meta.path] = meta
            _sync_to_rust(kernel, meta)
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
            "rust": self._kernel.dcache_stats() if self._kernel is not None else {},
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


class RustMetastoreProxy(MetastoreABC):
    """MetastoreABC implementation backed by Rust RedbMetastore.

    Delegates all operations to the Rust kernel's metastore via PyKernel
    methods (metastore_get, metastore_put, etc.). Rust kernel exclusively
    owns the redb file — Python no longer opens it directly.

    The Python dcache layer is bypassed — Rust DCache (DashMap ~100ns) is
    the authoritative read cache. All public methods override MetastoreABC
    to skip the Python dict dcache.

    Usage:
        kernel.set_metastore_path(str(redb_path))
        metadata_store = RustMetastoreProxy(kernel)
    """

    def __init__(self, kernel: Any, redb_path: str | None = None, /) -> None:
        super().__init__()
        self._rust_kernel = kernel
        # Federation mode: kernel has no global redb — every call routes
        # via ``mount_table.route(path, ROOT_ZONE_ID, ...)`` and hits a
        # per-mount ZoneMetastore installed by ``kernel.add_mount()``
        # (via ``py_zone_handle``). Skipping
        # ``set_metastore_path`` keeps the global fallback unset so an
        # accidental route miss blows up loudly instead of silently
        # returning empty.
        if redb_path is not None:
            kernel.set_metastore_path(redb_path)

    # ── Public API (bypass Python dcache — Rust DCache is authoritative) ─

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata directly from Rust metastore (no Python dcache)."""
        result: FileMetadata | None = self._rust_kernel.metastore_get(path)
        return result

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:  # noqa: ARG002
        """Store metadata via Rust metastore."""
        self._rust_kernel.metastore_put(metadata)
        return None

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:  # noqa: ARG002
        """Delete metadata via Rust metastore + evict from Rust DCache."""
        deleted = self._rust_kernel.metastore_delete(path)
        self._rust_kernel.dcache_evict(path)
        return {"deleted": deleted}

    def exists(self, path: str) -> bool:
        """Check existence via Rust metastore."""
        result: bool = self._rust_kernel.metastore_exists(path)
        return result

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> builtins.list[FileMetadata]:
        """List metadata from Rust metastore."""
        result: builtins.list[FileMetadata] = self._rust_kernel.metastore_list(prefix)
        return result

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> Iterator[FileMetadata]:
        """Iterate metadata from Rust metastore (bypasses Python dcache).

        Issue #3706: like list(), delegates directly to Rust without populating
        the Python-side _dcache, avoiding unbounded cache growth on repeated
        large directory listings.
        """
        yield from self._rust_kernel.metastore_list(prefix)

    def dcache_evict_prefix(self, prefix: str) -> int:
        """Evict all dcache entries under prefix (Rust DCache only)."""
        n: int = self._rust_kernel.dcache_evict_prefix(prefix)
        return n

    # ── Batch operations ─────────────────────────────────────────────────

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Batch get via Rust metastore (no Python dcache)."""
        results = self._rust_kernel.metastore_get_batch(list(paths))
        return dict(zip(paths, results, strict=True))

    def put_batch(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",  # noqa: ARG002
        skip_snapshot: bool = False,  # noqa: ARG002
    ) -> None:
        """Batch put via Rust metastore."""
        self._rust_kernel.metastore_put_batch(list(metadata_list))

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Batch delete via Rust metastore."""
        for p in paths:
            self._rust_kernel.metastore_delete(p)
            self._rust_kernel.dcache_evict(p)

    # ── Implicit directory check ─────────────────────────────────────────

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit directory (has children but no metadata)."""
        return bool(self._rust_kernel.metastore_is_implicit_directory(path))

    # ── Auxiliary per-path metadata (F3 C2 kernel bindings) ───────────────
    #
    # These route through ``kernel.metastore_set/get_file_metadata`` so
    # tests that previously stored ``parsed_text`` / ``parser_name`` /
    # tag blobs on a Python DictMetastore hit the kernel's DashMap
    # side-car instead. The kernel boundary stores strings — callers
    # that want to persist structured data JSON-encode themselves.

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if value is None:
            # Sentinel used by parser hooks to clear a field — the kernel
            # treats "absent" and "None" identically, so do nothing.
            return
        if not isinstance(value, str):
            import json

            value = json.dumps(value)
        self._rust_kernel.metastore_set_file_metadata(path, key, value)

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._rust_kernel.metastore_get_file_metadata(path, key)

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        return dict(self._rust_kernel.metastore_get_file_metadata_bulk(list(paths), key))

    def rename_path(self, old_path: str, new_path: str) -> None:
        self._rust_kernel.metastore_rename_path(old_path, new_path)

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        _zone_id: str | None = None,  # noqa: ARG002 — API compat
    ) -> Any:
        """Return a page of entries matching ``prefix``.

        Thin wrapper over ``kernel.metastore_list_paginated`` (F3 C2) — the
        kernel returns a ``{items, next_cursor, has_more, total_count}``
        dict; we wrap it in a ``PaginatedResult`` dataclass so callers
        keep using ``.items`` / ``.next_cursor`` attribute access.
        """
        from nexus.core.pagination import PaginatedResult

        page = self._rust_kernel.metastore_list_paginated(prefix, recursive, limit, cursor)
        return PaginatedResult(
            items=page["items"],
            next_cursor=page["next_cursor"],
            has_more=page["has_more"],
            total_count=page["total_count"],
        )

    def put_if_version(
        self,
        metadata: FileMetadata,
        expected_version: int,
        *,
        consistency: str = "sc",  # noqa: ARG002
    ) -> Any:
        return self._rust_kernel.metastore_put_if_version(metadata, expected_version)

    # ── Search service compatibility ─────────────────────────────────────

    def get_searchable_text_bulk(
        self,
        paths: "Sequence[str]",  # noqa: F821 — forward-ref to avoid circular import
    ) -> dict[str, str]:
        """Return cached ``parsed_text`` for the given paths.

        F3 C2 wired ``parsed_text`` storage into the kernel's file_metadata
        side-car; this call fans out to
        ``kernel.metastore_get_file_metadata_bulk`` and drops paths with
        no cached text so search_service grep / pipeline_indexer fall
        through to the raw-content path for un-parsed files.
        """
        bulk = self._rust_kernel.metastore_get_file_metadata_bulk(list(paths), "parsed_text")
        return {p: v for p, v in bulk.items() if v is not None}

    # ── Abstract method implementations (fallback, used by base class) ───

    def _get_raw(self, path: str) -> FileMetadata | None:
        result: FileMetadata | None = self._rust_kernel.metastore_get(path)
        return result

    def _put_raw(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:  # noqa: ARG002
        self._rust_kernel.metastore_put(metadata)
        return None

    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:  # noqa: ARG002
        deleted = self._rust_kernel.metastore_delete(path)
        return {"deleted": deleted}

    def _exists_raw(self, path: str) -> bool:
        result: bool = self._rust_kernel.metastore_exists(path)
        return result

    def _list_raw(
        self,
        prefix: str = "",
        recursive: bool = True,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> builtins.list[FileMetadata]:
        result: builtins.list[FileMetadata] = self._rust_kernel.metastore_list(prefix)
        return result

    def close(self) -> None:
        """No-op — Rust kernel manages redb lifecycle."""

    # Pre-existing gap: RustMetastoreProxy has no xattr surface, so
    # parsed_text (set by the auto_parse hook on other metastores for
    # binary docs like .pdf/.docx) is not available here. grep then
    # falls back to raw-byte reads, which skip non-UTF8 content. The
    # shims below return empty so SearchService can hasattr-check
    # uniformly; a one-time WARNING surfaces the limitation to operators
    # rather than letting the silent-empty behaviour mask the gap.
    _PARSED_TEXT_WARNING_EMITTED = False

    def _warn_parsed_text_unavailable_once(self) -> None:
        if not RustMetastoreProxy._PARSED_TEXT_WARNING_EMITTED:
            RustMetastoreProxy._PARSED_TEXT_WARNING_EMITTED = True
            logger.warning(
                "[RustMetastoreProxy] parsed_text xattr cache is not "
                "available on the Rust metastore — grep/index calls for "
                "parseable binaries (.pdf/.docx/.xlsx) will fall back to "
                "raw bytes and skip non-UTF8 payloads. Tracked as a "
                "SANDBOX follow-up."
            )

    def get_searchable_text(self, path: str) -> str | None:  # noqa: ARG002
        self._warn_parsed_text_unavailable_once()
        return None

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:  # noqa: ARG002
        self._warn_parsed_text_unavailable_once()
        return {}

    @property
    def cache_stats(self) -> dict[str, Any]:
        """Return Rust DCache stats (no Python dcache)."""
        return {
            "hits": 0,
            "misses": 0,
            "size": 0,
            "rust": self._rust_kernel.dcache_stats(),
        }
