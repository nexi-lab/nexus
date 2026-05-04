"""``RustMetastoreProxy`` — Python facade over the kernel-internal MetaStore.

After the post-DCache cleanup the Three Storage Pillars are:
  - MetaStore     (Rust kernel-internal — ``LocalMetaStore`` /
                   ``ZoneMetaStore`` / ``RemoteMetaStore``)
  - ObjectStore   (mounted post-init — ``backends/backend.py``)
  - RecordStore   (services-only — ``storage/record_store.py``)
  - CacheStore    (optional — ``core/cache_store.py``)

The kernel's metastore is the inode layer: the typed contract between
VFS and the underlying ordered KV store. The Rust kernel cannot describe
files without it. Linux analogue: ``struct inode_operations``.

Python no longer carries a separate ``MetastoreABC`` parallel hierarchy
— commit V deleted it. ``RustMetastoreProxy`` is a thin pass-through to
``kernel.metastore_*`` (no inheritance, no abstract base) plus five
non-trivial wrappers (JSON-encoded ``set_file_metadata``, recursive=False
post-filter on ``list``/``list_iter``, ``PaginatedResult`` wrap on
``list_paginated``, None-filter on ``get_searchable_text_bulk``). Those
wrappers are queued for extraction to ``nexus.kernel_helpers`` in the
following commit (Y), after which the proxy itself is deleted (W3).

SSOT: ``proto/nexus/core/metadata.proto`` defines the FileMetadata fields.
``rust/kernel/src/abc/meta_store.rs`` defines the *operations*; this
file is purely a Python boundary.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Iterator, Sequence
from typing import Any

from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)


def _is_direct_child(path: str, prefix: str) -> bool:
    """Return True when ``path`` is an immediate child of ``prefix``.

    Strip the prefix, then drop any entry whose remainder still contains
    a ``/`` (i.e. sits in a deeper subdirectory). Used to honour
    ``recursive=False`` on the ``list``/``list_iter`` proxy methods.
    """
    rel = path[len(prefix) :].lstrip("/") if path.startswith(prefix) else path
    return "/" not in rel


class RustMetastoreProxy:
    """Python facade backed by the Rust kernel's MetaStore.

    Delegates every operation to ``kernel.metastore_*`` PyO3 methods.
    Cache management lives entirely inside the Rust ``MetaStore`` impl
    (each impl owns its own internal ``DashMap`` projection); this
    Python class is purely a calling-convention adapter.

    Usage::

        kernel.set_metastore_path(str(redb_path))
        metadata_store = RustMetastoreProxy(kernel)
    """

    def __init__(self, kernel: Any, redb_path: str | None = None, /) -> None:
        self._rust_kernel = kernel
        # Federation mode: kernel has no global redb — every call routes
        # via ``mount_table.route(path, ROOT_ZONE_ID, ...)`` and hits a
        # per-mount ZoneMetastore installed by ``kernel.add_mount()``
        # (via ``py_zone_handle``). Skipping ``set_metastore_path`` keeps
        # the global fallback unset so an accidental route miss blows up
        # loudly instead of silently returning empty.
        if redb_path is not None:
            kernel.set_metastore_path(redb_path)

    # ── Public API ───────────────────────────────────────────────────────

    def get(self, path: str) -> FileMetadata | None:
        result: FileMetadata | None = self._rust_kernel.metastore_get(path)
        return result

    def put(self, metadata: FileMetadata) -> None:
        self._rust_kernel.metastore_put(metadata)

    def delete(self, path: str) -> dict[str, Any] | None:
        """Delete metadata via Rust metastore.

        The Rust impl invalidates its own internal cache before the
        store delete (see commit_delete in rust/kernel/src/kernel/mod.rs).
        """
        deleted = self._rust_kernel.metastore_delete(path)
        return {"deleted": deleted}

    def exists(self, path: str) -> bool:
        result: bool = self._rust_kernel.metastore_exists(path)
        return result

    def list(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,  # noqa: ARG002
    ) -> builtins.list[FileMetadata]:
        """List metadata from Rust metastore.

        The Rust ``metastore_list`` is prefix-only — it returns every
        entry whose path starts with ``prefix``. When the caller asks
        for ``recursive=False`` we post-filter in Python to keep
        entries that live directly under the prefix (no further ``/``
        separator).
        """
        result: builtins.list[FileMetadata] = self._rust_kernel.metastore_list(prefix)
        if recursive:
            return result
        return [e for e in result if _is_direct_child(e.path, prefix)]

    def list_iter(
        self,
        prefix: str = "",
        recursive: bool = True,
        **kwargs: Any,  # noqa: ARG002
    ) -> Iterator[FileMetadata]:
        """Iterate metadata from Rust metastore.

        Honours ``recursive=False`` via the same post-filter as
        ``list()``: the Rust call returns everything under ``prefix``
        and we drop deeper entries here.
        """
        for e in self._rust_kernel.metastore_list(prefix):
            if recursive or _is_direct_child(e.path, prefix):
                yield e

    # ── Batch operations ─────────────────────────────────────────────────

    def get_batch(self, paths: Sequence[str]) -> dict[str, FileMetadata | None]:
        results = self._rust_kernel.metastore_get_batch(list(paths))
        return dict(zip(paths, results, strict=True))

    def put_batch(
        self,
        metadata_list: Sequence[FileMetadata],
        *,
        skip_snapshot: bool = False,  # noqa: ARG002
    ) -> None:
        self._rust_kernel.metastore_put_batch(list(metadata_list))

    def delete_batch(self, paths: Sequence[str]) -> None:
        for p in paths:
            self._rust_kernel.metastore_delete(p)

    def batch_get_content_ids(self, paths: Sequence[str]) -> dict[str, str | None]:
        """Return ``{path: content_id_or_None}`` for the given paths."""
        result: dict[str, str | None] = {}
        for path in paths:
            metadata = self.get(path)
            result[path] = metadata.content_id if metadata else None
        return result

    # ── Implicit directory check ─────────────────────────────────────────

    def is_implicit_directory(self, path: str) -> bool:
        return bool(self._rust_kernel.metastore_is_implicit_directory(path))

    # ── Auxiliary per-path metadata (F3 C2 kernel bindings) ───────────────
    #
    # These route through ``kernel.metastore_set/get_file_metadata``;
    # tag blobs (``parsed_text`` / ``parser_name`` / etc.) live in the
    # kernel's DashMap side-car. The kernel boundary stores strings —
    # callers that want to persist structured data JSON-encode themselves.

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


# ``MetastoreABC`` was deleted in commit V. Type hints across the factory
# now reference ``RustMetastoreProxy`` directly.
