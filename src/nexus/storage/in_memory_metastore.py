"""In-memory MetastoreABC implementation.

Lightweight metastore backed by a Python dict. Production use cases:

- **REMOTE profile**: Client-side metadata cache for NexusFS(profile=REMOTE).
  Caches server metadata locally for fast path resolution (~0μs vs network RTT).
  Acts as a read-through cache — populated on first access, TTL-invalidated.

- **Testing**: Drop-in replacement for redb-backed metastore in unit tests.

This is NOT a toy — it implements the full MetastoreABC contract including
CAS (put_if_version), batch ops, and rename. Thread-safe under Python GIL.

Issue #844: Promoted from tests/helpers/ to storage/ (metastore driver layer).
"""

from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import CasResult, MetastoreABC, PaginatedResult


class InMemoryMetastore(MetastoreABC):
    """In-memory Metastore pillar implementation.

    Suitable for REMOTE profile (metadata cache) and testing.
    All operations are O(1) for point lookups, O(n) for scans.
    """

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}
        self._file_metadata: dict[str, dict[str, Any]] = {}  # path -> {key: value}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        del consistency
        self._store[metadata.path] = metadata
        return None

    def put_if_version(
        self,
        metadata: FileMetadata,
        expected_version: int,
        *,
        consistency: str = "sc",
    ) -> CasResult:
        """Atomic CAS — trivially safe under Python GIL."""
        del consistency
        current = self._store.get(metadata.path)
        current_ver = current.version if current else 0
        if current_ver != expected_version:
            return CasResult(success=False, current_version=current_ver)
        self._store[metadata.path] = metadata
        return CasResult(success=True, current_version=metadata.version)

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        del consistency
        if path in self._store:
            del self._store[path]
            self._file_metadata.pop(path, None)
            return {"deleted": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **_kwargs: Any) -> list[FileMetadata]:
        results = [meta for path, meta in self._store.items() if path.startswith(prefix)]
        if not recursive:
            # Filter to direct children only
            depth = prefix.rstrip("/").count("/") + 1
            results = [m for m in results if m.path.rstrip("/").count("/") == depth]
        return results

    def list_iter(
        self, prefix: str = "", recursive: bool = True, **_kwargs: Any
    ) -> Iterator[FileMetadata]:
        yield from self.list(prefix, recursive)

    def list_paginated(
        self,
        prefix: str = "",
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
        _zone_id: str | None = None,
    ) -> PaginatedResult:
        del _zone_id
        all_items = self.list(prefix, recursive)
        start = int(cursor) if cursor else 0
        page = all_items[start : start + limit]
        has_more = start + limit < len(all_items)
        next_cursor = page[-1].path if has_more and page else None
        return PaginatedResult(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=len(all_items),
        )

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {path: self._store.get(path) for path in paths}

    def delete_batch(self, paths: Sequence[str]) -> None:
        for path in paths:
            self._store.pop(path, None)
            self._file_metadata.pop(path, None)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        for metadata in metadata_list:
            self._store[metadata.path] = metadata

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        result: dict[str, str | None] = {}
        for path in paths:
            meta = self._store.get(path)
            result[path] = meta.etag if meta else None
        return result

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a path in the metadata store."""
        meta = self._store.pop(old_path, None)
        if meta is not None:
            from dataclasses import replace

            new_meta = replace(meta, path=new_path)
            self._store[new_path] = new_meta
            if old_path in self._file_metadata:
                self._file_metadata[new_path] = self._file_metadata.pop(old_path)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        """Set arbitrary key-value metadata on a file."""
        if path not in self._file_metadata:
            self._file_metadata[path] = {}
        self._file_metadata[path][key] = value

    def get_file_metadata(self, path: str, key: str) -> Any:
        """Get arbitrary key-value metadata for a file."""
        return self._file_metadata.get(path, {}).get(key)

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit directory (has children but no explicit entry)."""
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)

    def close(self) -> None:
        self._store.clear()
        self._file_metadata.clear()
