"""JSON-backed MetastoreABC fallback for environments without Rust extensions.

Lightweight single-file metastore used as automatic fallback when the Raft
metastore (Rust/PyO3) is not available. This keeps the local SDK and CLI
quickstart paths working from a plain source checkout without requiring a Rust
build.

State persists to a JSON file for local restarts, but this backend is still
meant for single-process development and quickstarts. For the durable Rust
metastore, build ``maturin develop -m rust/nexus_raft/Cargo.toml --features
python``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC
from nexus.core.pagination import PaginatedResult
from nexus.storage._metadata_mapper_generated import MetadataMapper

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _CasResult:
    """Compare-and-swap result for put_if_version."""

    success: bool
    current_version: int


class DictMetastore(MetastoreABC):
    """JSON-backed metastore fallback with no Rust requirement.

    All operations are O(1) for point lookups and O(n) for scans. Suitable for
    local development, tests, and quickstarts.
    """

    def __init__(self, storage_path: str | Path | None = None) -> None:
        super().__init__()
        self._store: dict[str, FileMetadata] = {}
        self._file_metadata: dict[str, dict[str, Any]] = {}
        self._storage_path = Path(storage_path) if storage_path is not None else None
        if self._storage_path is not None:
            self._load()

    def _load(self) -> None:
        if self._storage_path is None or not self._storage_path.exists():
            return
        with self._storage_path.open(encoding="utf-8") as fh:
            payload = json.load(fh)

        raw_store = payload.get("store", {})
        raw_file_metadata = payload.get("file_metadata", {})

        if isinstance(raw_store, dict):
            self._store = {
                path: MetadataMapper.from_json(meta)
                for path, meta in raw_store.items()
                if isinstance(meta, dict)
            }
        if isinstance(raw_file_metadata, dict):
            self._file_metadata = {
                path: meta for path, meta in raw_file_metadata.items() if isinstance(meta, dict)
            }

    def _flush(self) -> None:
        if self._storage_path is None:
            return

        self._storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "store": {path: MetadataMapper.to_json(meta) for path, meta in self._store.items()},
            "file_metadata": self._file_metadata,
        }
        tmp_path = self._storage_path.with_suffix(f"{self._storage_path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        tmp_path.replace(self._storage_path)

    # -- abstract methods --------------------------------------------------

    def _get_raw(self, path: str) -> FileMetadata | None:
        return self._store.get(path)

    def _put_raw(self, metadata: FileMetadata) -> None:
        self._store[metadata.path] = metadata
        self._flush()

    def put_if_version(
        self,
        metadata: FileMetadata,
        expected_version: int,
        *,
        consistency: str = "sc",
    ) -> _CasResult:
        del consistency
        current = self._store.get(metadata.path)
        current_ver = current.version if current else 0
        if current_ver != expected_version:
            return _CasResult(success=False, current_version=current_ver)
        self._store[metadata.path] = metadata
        self._flush()
        return _CasResult(success=True, current_version=metadata.version)

    def _delete_raw(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        del consistency
        if path in self._store:
            del self._store[path]
            self._file_metadata.pop(path, None)
            self._flush()
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
        self._flush()
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
        next_cursor = str(start + limit) if has_more and page else None
        return PaginatedResult(
            items=page,
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=len(all_items),
        )

    def _get_batch_raw(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        return {p: self._store.get(p) for p in paths}

    def _delete_batch_raw(self, paths: Sequence[str]) -> None:
        for p in paths:
            self._store.pop(p, None)
            self._file_metadata.pop(p, None)
        self._flush()

    def _put_batch_raw(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        consistency: str = "sc",  # noqa: ARG002
        skip_snapshot: bool = False,  # noqa: ARG002
    ) -> None:
        for m in metadata_list:
            self._store[m.path] = m
        self._flush()

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        return {p: (m.etag if (m := self._store.get(p)) else None) for p in paths}

    def set_file_metadata(self, path: str, key: str, value: Any) -> None:
        if path not in self._file_metadata:
            self._file_metadata[path] = {}
        self._file_metadata[path][key] = value
        self._flush()

    def get_file_metadata(self, path: str, key: str) -> Any:
        return self._file_metadata.get(path, {}).get(key)

    def get_file_metadata_bulk(self, paths: Sequence[str], key: str) -> dict[str, Any]:
        """Get custom metadata value for multiple files."""
        return {path: self._file_metadata.get(path, {}).get(key) for path in paths}

    def get_searchable_text_bulk(self, paths: Sequence[str]) -> dict[str, str]:
        """Get cached searchable text for multiple files.

        Returns dict mapping path → text (only includes paths with cached text).
        """
        bulk = self.get_file_metadata_bulk(paths, "parsed_text")
        return {path: text for path, text in bulk.items() if text is not None}

    def is_implicit_directory(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return any(p.startswith(prefix) for p in self._store)
