"""In-memory FileMetadataProtocol implementation for tests.

Shared helper that provides all methods used by the NexusFS kernel,
including rename_path and set_file_metadata which are not part of the
base FileMetadataProtocol ABC but are used via duck-typing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nexus.core._metadata_generated import FileMetadata, FileMetadataProtocol


class InMemoryFileMetadataStore(FileMetadataProtocol):
    """In-memory metadata store for tests that don't need Rust Raft extension."""

    def __init__(self) -> None:
        self._store: dict[str, FileMetadata] = {}
        self._file_metadata: dict[str, dict[str, Any]] = {}  # path -> {key: value}

    def get(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def put(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata

    def delete(self, path: str) -> dict[str, Any] | None:
        if path in self._store:
            del self._store[path]
            self._file_metadata.pop(path, None)
            return {"deleted": path}
        return None

    def exists(self, path: str) -> bool:
        return path in self._store

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        results = [meta for path, meta in self._store.items() if path.startswith(prefix)]
        if not recursive:
            # Filter to direct children only
            depth = prefix.rstrip("/").count("/") + 1
            results = [m for m in results if m.path.rstrip("/").count("/") == depth]
        return results

    def delete_batch(self, paths: Sequence[str]) -> None:
        for path in paths:
            self._store.pop(path, None)
            self._file_metadata.pop(path, None)

    def put_batch(self, metadata_list: Sequence[FileMetadata]) -> None:
        for metadata in metadata_list:
            self._store[metadata.path] = metadata

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a path in the metadata store."""
        meta = self._store.pop(old_path, None)
        if meta is not None:
            from dataclasses import replace

            new_meta = replace(meta, path=new_path)
            self._store[new_path] = new_meta
            # Move file metadata too
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
