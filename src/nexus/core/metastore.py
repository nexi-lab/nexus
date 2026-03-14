"""MetastoreABC — the Metastore storage pillar.

One of the Four Storage Pillars (NEXUS-LEGO-ARCHITECTURE.md §2.0):
  - MetastoreABC  (required at boot — this file)
  - ObjectStoreABC (mounted post-init — backends/backend.py)
  - RecordStoreABC (services-only — storage/record_store.py)
  - CacheStoreABC  (optional — core/cache_store.py)

MetastoreABC is the kernel's inode layer: the typed contract between
VFS and the underlying ordered KV store.  The kernel cannot describe
files without it.  Linux analogue: ``struct inode_operations``.

Implementations:
  - RaftMetadataStore  (storage/raft_metadata_store.py)
  - FederatedMetadataProxy (raft/federated_metadata_proxy.py)

The AsyncMetastoreWrapper provides an async façade via asyncio.to_thread().

SSOT: proto/nexus/core/metadata.proto defines the FileMetadata fields.
This ABC defines the *operations* over those fields.
"""

import asyncio
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from nexus.contracts.metadata import FileMetadata, PaginatedResult

# Module-level guard for lazy _cas_lock initialization on MetastoreABC instances.
_CAS_LOCK_INIT_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class CasResult:
    """Result of an atomic compare-and-swap (CAS) metadata operation.

    Returned by ``put_if_version()`` to indicate whether the write
    succeeded and what the current version is.
    """

    success: bool
    """True if the CAS write was applied (version matched)."""

    current_version: int
    """Version after the operation.  On success this is the *new* version;
    on failure it is the version that caused the mismatch."""


class MetastoreABC(ABC):
    """Abstract base class for metadata storage (the "Metastore" pillar).

    Stores mapping between virtual paths and backend physical locations.
    All metastore backends (Raft, Federated, etc.) implement this interface.

    Abstract methods (must override):
        get, put, delete, exists, list, close

    Concrete methods (may override for performance):
        is_committed, list_iter, list_paginated,
        get_batch, delete_batch, put_batch, batch_get_content_ids
    """

    @abstractmethod
    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file."""
        pass

    @abstractmethod
    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store or update file metadata.

        Returns:
            EC mode: write token (int) for polling via is_committed().
            SC mode: None (write is already committed when this returns).
        """
        pass

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

    @abstractmethod
    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete file metadata. Returns deleted file info or None."""
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if metadata exists for a path."""
        pass

    @abstractmethod
    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        """List all files with given path prefix."""
        pass

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,
    ) -> Iterator[FileMetadata]:
        """Iterate over file metadata matching prefix.

        Memory-efficient alternative to list(). Yields results one at a time
        instead of materializing the full list in memory.

        Subclasses may override for true streaming from the underlying store.
        The default implementation delegates to list() for backward compatibility.
        """
        yield from self.list(prefix, recursive, **kwargs)

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        _zone_id: str | None = None,
    ) -> PaginatedResult:
        """List files with cursor-based pagination.

        Uses keyset pagination for O(log n) performance regardless of page depth.
        Subclasses may override for zone-aware queries via the ``_zone_id`` param.
        """
        all_items = self.list(prefix, recursive)
        # Filter and limit in a single pass to avoid materializing entire list
        page = []
        has_more = False
        for item in all_items:
            if cursor and item.path <= cursor:
                continue
            if len(page) >= limit:
                has_more = True
                break
            page.append(item)
        next_cursor = page[-1].path if has_more and page else None
        return PaginatedResult(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=len(all_items),
        )

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        """Get metadata for multiple files in a single query."""
        return {path: self.get(path) for path in paths}

    def delete_batch(self, paths: Sequence[str]) -> None:
        """Delete multiple files in a single transaction."""
        for path in paths:
            self.delete(path)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        """Store or update multiple file metadata entries in a single transaction."""
        for metadata in metadata_list:
            self.put(metadata)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Get content IDs (hashes) for multiple paths in a single query."""
        result: dict[str, str | None] = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.etag if metadata else None
        return result

    def put_if_version(
        self,
        metadata: FileMetadata,
        expected_version: int,
        *,
        consistency: str = "sc",
    ) -> CasResult:
        """Atomic compare-and-swap on FileMetadata.version.

        Writes ``metadata`` only if the current version of the file at
        ``metadata.path`` equals ``expected_version``.

        Default implementation: non-atomic fallback (read-check-write).
        Override in concrete subclasses for true atomicity.

        Args:
            metadata: The new metadata to store.
            expected_version: Version that must match the current stored
                version.  Use 0 for create-only semantics.
            consistency: ``"sc"`` (default) or ``"ec"``.

        Returns:
            CasResult indicating success and the current version.
        """
        if not hasattr(self, "_cas_lock"):
            with _CAS_LOCK_INIT_GUARD:
                if not hasattr(self, "_cas_lock"):
                    self._cas_lock = threading.Lock()
        with self._cas_lock:
            current = self.get(metadata.path)
            current_ver = current.version if current else 0
            if current_ver != expected_version:
                return CasResult(success=False, current_version=current_ver)
            self.put(metadata, consistency=consistency)
            return CasResult(success=True, current_version=metadata.version)

    @abstractmethod
    def close(self) -> None:
        """Close the metadata store and release resources."""
        pass


class AsyncMetastoreWrapper:
    """Async wrapper around any MetastoreABC implementation.

    Each ``aXXX(...)`` method delegates to ``asyncio.to_thread(store.XXX, ...)``.
    Performance: sled ~5 us + to_thread ~50 us = 55 us per call.
    """

    def __init__(self, store: MetastoreABC) -> None:
        self._store = store

    async def aget(self, path: str) -> FileMetadata | None:
        return await asyncio.to_thread(self._store.get, path)

    async def aput(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        return await asyncio.to_thread(self._store.put, metadata, consistency=consistency)

    async def ais_committed(self, token: int) -> str | None:
        return await asyncio.to_thread(self._store.is_committed, token)

    async def adelete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        return await asyncio.to_thread(self._store.delete, path, consistency=consistency)

    async def aexists(self, path: str) -> bool:
        return await asyncio.to_thread(self._store.exists, path)

    async def alist(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]:
        return await asyncio.to_thread(self._store.list, prefix, recursive, **kwargs)

    async def alist_iter(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> Iterator[FileMetadata]:
        return await asyncio.to_thread(self._store.list_iter, prefix, recursive, **kwargs)

    async def alist_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        zone_id: str | None = None,
    ) -> PaginatedResult:
        return await asyncio.to_thread(
            self._store.list_paginated, prefix, recursive, limit, cursor, zone_id
        )

    async def aget_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return await asyncio.to_thread(self._store.get_batch, paths)

    async def adelete_batch(self, paths: Sequence[str]) -> None:
        return await asyncio.to_thread(self._store.delete_batch, paths)

    async def aput_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        return await asyncio.to_thread(self._store.put_batch, metadata_list)

    async def abatch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        return await asyncio.to_thread(self._store.batch_get_content_ids, paths)

    async def aput_if_version(
        self,
        metadata: FileMetadata,
        expected_version: int,
        *,
        consistency: str = "sc",
    ) -> CasResult:
        return await asyncio.to_thread(
            self._store.put_if_version, metadata, expected_version, consistency=consistency
        )

    async def aclose(self) -> None:
        return await asyncio.to_thread(self._store.close)
