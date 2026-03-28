"""Dict-backed MetastoreABC for testing (Issue #844).

Lightweight in-memory metastore for unit/integration tests. This is NOT
production code -- it lives in test infrastructure only.

Replaces the former ``InMemoryMetastore`` that was in
``src/nexus/storage/in_memory_metastore.py``.
"""

from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC


class DictMetastore(MetastoreABC):
    """Dict-backed metastore for tests.

    All operations are O(1) for point lookups, O(n) for scans.
    Thread-safe under Python GIL.
    """

    def __init__(self) -> None:
        super().__init__()
        self._store: dict[str, FileMetadata] = {}
        self._file_metadata: dict[str, dict[str, Any]] = {}

    # -- abstract methods --------------------------------------------------

    def _get_raw(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def _put_raw(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        del consistency
        self._store[metadata.path] = metadata
        return None

    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        del consistency
        if path in self._store:
            del self._store[path]
            self._file_metadata.pop(path, None)
            return {"deleted": path}
        return None

    def _exists_raw(self, path: str) -> bool:
        return path in self._store

    def _list_raw(self, prefix: str = "", recursive: bool = True, **_kw: Any) -> list[FileMetadata]:
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

    def _get_batch_raw(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def _delete_batch_raw(self, paths: Sequence[str]) -> None:
        for p in paths:
            self._store.pop(p, None)
            self._file_metadata.pop(p, None)

    def _put_batch_raw(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",  # noqa: ARG002
        skip_snapshot: bool = False,  # noqa: ARG002
    ) -> None:
        for m in metadata_list:
            self._store[m.path] = m

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        return {p: (m.etag if (m := self._store.get(p)) else None) for p in paths}

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Atomically rename a path (and its children) in metadata."""
        from dataclasses import replace

        # 1. Rename the item itself
        meta = self._store.pop(old_path, None)
        if meta is not None:
            self._store[new_path] = replace(meta, path=new_path)
            if old_path in self._file_metadata:
                self._file_metadata[new_path] = self._file_metadata.pop(old_path)

        # 2. Rename all children recursively
        # (This is a simplified mock implementation for tests)
        prefix = old_path + "/"
        child_paths = [p for p in self._store if p.startswith(prefix)]
        for p in sorted(child_paths):  # Sorted to handle depths correctly if needed
            child_meta = self._store.pop(p)
            new_child_path = new_path + p[len(old_path) :]
            self._store[new_child_path] = replace(child_meta, path=new_child_path)
            if p in self._file_metadata:
                self._file_metadata[new_child_path] = self._file_metadata.pop(p)

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if path not in self._file_metadata:
            self._file_metadata[path] = {}
        self._file_metadata[path][key] = value

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._file_metadata.get(path, {}).get(key)

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        """Get custom metadata value for multiple files."""
        return {path: self._file_metadata.get(path, {}).get(key) for path in paths}

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:
        """Get cached searchable text for multiple files."""
        bulk = self.get_file_metadata_bulk(paths, "parsed_text")
        return {path: text for path, text in bulk.items() if text is not None}

    def is_implicit_directory(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)
