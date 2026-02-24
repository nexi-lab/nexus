"""Dict-backed MetastoreABC for testing (Issue #844).

Lightweight in-memory metastore for unit/integration tests. This is NOT
production code -- it lives in test infrastructure only.

Replaces the former ``InMemoryMetastore`` that was in
``src/nexus/storage/in_memory_metastore.py``.
"""

from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import CasResult, MetastoreABC, PaginatedResult


class DictMetastore(MetastoreABC):
    """Dict-backed metastore for tests.

    All operations are O(1) for point lookups, O(n) for scans.
    Thread-safe under Python GIL.
    """

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}
        self._file_metadata: dict[str, dict[str, Any]] = {}

    # -- abstract methods --------------------------------------------------

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

    def list(self, prefix: str = "", recursive: bool = True, **_kw: Any) -> list[FileMetadata]:
        results = [m for p, m in self._store.items() if p.startswith(prefix)]
        if not recursive:
            depth = prefix.rstrip("/").count("/") + 1
            results = [m for m in results if m.path.rstrip("/").count("/") == depth]
        return results

    def close(self) -> None:
        self._store.clear()
        self._file_metadata.clear()

    # -- concrete overrides ------------------------------------------------

    def list_iter(
        self, prefix: str = "", recursive: bool = True, **_kw: Any
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
        return {p: self._store.get(p) for p in paths}

    def delete_batch(self, paths: Sequence[str]) -> None:
        for p in paths:
            self._store.pop(p, None)
            self._file_metadata.pop(p, None)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        for m in metadata_list:
            self._store[m.path] = m

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        return {p: (m.etag if (m := self._store.get(p)) else None) for p in paths}

    def rename_path(self, old_path: str, new_path: str) -> None:
        meta = self._store.pop(old_path, None)
        if meta is not None:
            from dataclasses import replace

            self._store[new_path] = replace(meta, path=new_path)
            if old_path in self._file_metadata:
                self._file_metadata[new_path] = self._file_metadata.pop(old_path)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if path not in self._file_metadata:
            self._file_metadata[path] = {}
        self._file_metadata[path][key] = value

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._file_metadata.get(path, {}).get(key)

    def is_implicit_directory(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)
