"""Thin NexusFS facade for the slim package.

Exposes ~10 public methods from the kernel NexusFS. Internal methods
(sandbox, workflows, bulk operations, dispatch hooks) are hidden.

The facade also provides optimized implementations where the full kernel
path is unnecessarily heavy for slim-package use (e.g., single-lookup stat).

Usage:
    from nexus.fs._facade import SlimNexusFS

    facade = SlimNexusFS(kernel_fs)
    content = facade.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


def _make_stat_dict(
    *,
    path: str,
    size: int,
    etag: str | None,
    mime_type: str,
    created_at: str | None,
    modified_at: str | None,
    is_directory: bool,
    version: int,
    zone_id: str | None,
    entry_type: int,
) -> dict[str, Any]:
    """Build the stat response dict.  Single source of truth for the shape."""
    return {
        "path": path,
        "size": size,
        "etag": etag,
        "mime_type": mime_type,
        "created_at": created_at,
        "modified_at": modified_at,
        "is_directory": is_directory,
        "version": version,
        "zone_id": zone_id,
        "entry_type": entry_type,
    }


# Default context for slim-mode (single-user, no auth)
_SLIM_CONTEXT = OperationContext(
    user_id="local",
    groups=[],
    zone_id=ROOT_ZONE_ID,
    is_admin=True,
)


class SlimNexusFS:
    """Slim facade over the NexusFS kernel.

    Provides a clean, minimal API surface for the standalone nexus-fs package.
    All methods use a default local context (no auth, single-user).

    Public API (~10 methods):
        read, write, ls, stat, delete, mkdir, rmdir, rename, exists, copy
    """

    def __init__(self, kernel: NexusFS) -> None:
        self._kernel = kernel
        self._ctx = _SLIM_CONTEXT
        self._closed = False

    @property
    def kernel(self) -> NexusFS:
        """Escape hatch: access the underlying kernel for advanced use."""
        return self._kernel

    # -- Read operations --

    def read(self, path: str) -> bytes:
        """Read file content.

        Args:
            path: Virtual file path (e.g., "/s3/my-bucket/file.txt")

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If file does not exist.
        """
        from nexus.contracts.exceptions import NexusFileNotFoundError

        try:
            return self._kernel.sys_read(path, context=self._ctx)
        except NexusFileNotFoundError:
            # Rust sys_read handles all backend types uniformly (§12d):
            # CAS via etag, path-local/external via backend_path fallback.
            # Only slim-mode (no Rust kernel) needs the Python metastore
            # fallback for CAS entries (#3821).
            data = self._slim_metastore_read(path)
            if data is None:
                raise
            return data

    def _slim_metastore_read(self, path: str) -> bytes | None:
        """Read via Python metastore + Rust backend (slim fallback, #3821).

        Only relevant in slim-mode where the Rust kernel cannot see the
        Python SQLiteMetastore.  The etag check naturally gates to CAS
        entries (path-addressed backends don't store etags).
        """
        from nexus.core.path_utils import validate_path

        try:
            normalized = validate_path(path)
        except Exception:
            return None
        meta = self._kernel.metadata.get(normalized)
        if meta is None or not meta.etag:
            return None
        _rust_kernel = getattr(self._kernel, "_kernel", None)
        if _rust_kernel is None:
            return None
        from nexus.contracts.exceptions import NexusFileNotFoundError

        try:
            data = _rust_kernel.sys_read_raw(normalized, self._kernel._zone_id)
        except NexusFileNotFoundError:
            return None
        return bytes(data) if isinstance(data, (bytes, bytearray)) else None

    def read_range(self, path: str, start: int, end: int) -> bytes:
        """Read a specific byte range from a file.

        Memory-efficient — only fetches the requested range from the backend.

        Args:
            path: Virtual file path.
            start: Start byte offset (inclusive).
            end: End byte offset (exclusive).

        Returns:
            Bytes in the requested range.
        """
        return self._kernel.read_range(path, start, end, context=self._ctx)

    # -- Write operations --

    def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Write content to a file (creates or overwrites).

        Args:
            path: Virtual file path.
            content: File content as bytes.

        Returns:
            Dict with path, size, etag, version.
        """
        return self._kernel.write(path, content, context=self._ctx)

    def write_batch(self, files: list[tuple[str, bytes]]) -> list[dict[str, Any]]:
        """Write multiple files atomically in a single transaction.

        All files are written atomically — either all succeed or all fail.
        13× faster than N sequential ``write()`` calls for small files.

        Args:
            files: List of ``(path, content)`` tuples.

        Returns:
            List of result dicts (same order as input), each with
            ``etag``, ``version``, ``modified_at``, and ``size``.

        Raises:
            NexusFileNotFoundError: Never — writes always create.
            InvalidPathError: If any path is invalid.
        """
        return self._kernel.write_batch(files, context=self._ctx)

    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
    ) -> list[dict[str, Any]]:
        """Read multiple files in a single atomic round-trip.

        Uses the Rust kernel's parallel read path — faster and more
        consistent than N sequential ``read()`` calls.

        Args:
            paths:   List of virtual file paths.
            partial: If ``False`` (default), raises ``NexusFileNotFoundError``
                     on the first missing or inaccessible path.
                     If ``True``, returns a per-item result for every path
                     (successes and errors alike).

        Returns:
            List of dicts in the same order as *paths*.

            Successful item::

                {
                    "path":        str,
                    "content":     bytes,
                    "etag":        str | None,
                    "version":     int,
                    "modified_at": datetime | None,
                    "size":        int,
                }

            Failed item (only when ``partial=True``)::

                {"path": str, "error": "not_found"}

        Raises:
            NexusFileNotFoundError: If any path is missing and ``partial=False``.
            InvalidPathError: If any path is invalid (always raised).
        """
        return self._kernel.read_batch(paths, partial=partial, context=self._ctx)

    # -- Directory operations --

    def ls(
        self,
        path: str = "/",
        detail: bool = False,
        recursive: bool = False,
    ) -> list[str] | list[dict[str, Any]]:
        """List directory contents.

        Args:
            path: Directory path to list.
            detail: If True, return dicts with metadata. If False, return paths.
            recursive: If True, list recursively.

        Returns:
            List of paths (detail=False) or list of metadata dicts (detail=True).
        """
        # Rust readdir merges dcache + metastore + backend list_dir
        # uniformly (§12d Phase 2) — no Python-side merge needed.
        return list(
            self._kernel.sys_readdir(
                path,
                recursive=recursive,
                details=detail,
                context=self._ctx,
            )
        )

    def mkdir(self, path: str, parents: bool = True) -> None:
        """Create a directory.

        Args:
            path: Directory path to create.
            parents: If True, create parent directories as needed (mkdir -p).
        """
        self._kernel.mkdir(
            path,
            parents=parents,
            exist_ok=True,
            context=self._ctx,
        )

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory.

        Args:
            path: Directory path to remove.
            recursive: If True, remove contents recursively (rm -rf).
        """
        self._kernel.rmdir(path, recursive=recursive, context=self._ctx)

    # -- File operations --

    def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual file path to delete.

        Raises:
            NexusFileNotFoundError: If file does not exist.
            ValueError: If path is a mount root — use unmount() instead.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(path)
        meta = self._kernel.metadata.get(normalized)
        if meta is not None and meta.is_mount:
            raise ValueError(
                f"Cannot delete mount root '{normalized}' — use unmount() to remove a mount."
            )
        self._kernel.sys_unlink(path, context=self._ctx)

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file.

        Args:
            old_path: Current file path.
            new_path: New file path.
        """
        self._kernel.sys_rename(old_path, new_path, context=self._ctx)

    def exists(self, path: str) -> bool:
        """Check if a path exists.

        Args:
            path: Virtual file path.

        Returns:
            True if the path exists (file or directory).
        """
        return self._kernel.access(path, context=self._ctx)

    def copy(self, src: str, dst: str) -> dict[str, Any]:
        """Copy a file from src to dst.

        Delegates to the kernel's sys_copy which uses backend-native
        server-side copy when available (S3 CopyObject, GCS rewrite),
        CAS metadata duplication for content-addressed backends, or
        chunked streaming as a fallback.

        Args:
            src: Source file path.
            dst: Destination file path.

        Returns:
            Dict with path, size, etag of the new file.
        """
        return self._kernel.sys_copy(src, dst, context=self._ctx)

    def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]],
        *,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file.

        Uses a layered matching strategy (exact -> whitespace-normalized -> fuzzy)
        to find and replace text without rewriting the entire file.

        Args:
            path: Virtual file path.
            edits: List of edit operations. Each can be:
                - Tuple: (old_str, new_str)
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool}
            if_match: Optional etag for optimistic concurrency control.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
            preview: If True, return preview without writing.

        Returns:
            Dict with success, diff, matches, applied_count, etag, version, errors.

        Note:
            The underlying kernel edit is NOT atomic — there is a TOCTOU window
            between the read and the final write.  ``if_match`` catches stale
            reads but cannot prevent a concurrent writer from updating the file
            between the ETag check and the write.  For concurrent-writer safety,
            use an external lock or wait for kernel-level OCC-aware writes.
        """
        return self._kernel.edit(
            path,
            edits,
            context=self._ctx,
            if_match=if_match,
            fuzzy_threshold=fuzzy_threshold,
            preview=preview,
        )

    # -- Metadata (optimized single-lookup) --

    def stat(self, path: str) -> dict[str, Any] | None:
        """Get file/directory metadata with a single metadata lookup.

        Optimized for the slim package — avoids the kernel's double-lookup
        pattern (is_directory + metadata.get) by doing one read and
        deriving directory status from the result.

        Args:
            path: Virtual file path.

        Returns:
            Metadata dict, or None if path does not exist.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(path, allow_root=True)

        # Route through the kernel's sys_stat so zone-relative key translation
        # is handled centrally. Direct ``metadata.get(global_path)`` returns
        # ``None`` after F4 zone-relative key refactor for paths under mounts
        # (the entry lives at the mount's zone-local key).
        _kstat = self._kernel.sys_stat(normalized, context=self._ctx)
        if _kstat is not None:
            return _kstat

        meta: FileMetadata | None = None

        if meta is not None:
            is_dir = meta.is_dir or meta.is_mount or meta.mime_type == "inode/directory"
            return _make_stat_dict(
                path=meta.path,
                size=meta.size or (4096 if is_dir else 0),
                etag=meta.etag,
                mime_type=meta.mime_type
                or ("inode/directory" if is_dir else "application/octet-stream"),
                created_at=meta.created_at.isoformat() if meta.created_at else None,
                modified_at=meta.modified_at.isoformat() if meta.modified_at else None,
                is_directory=is_dir,
                version=meta.version,
                zone_id=meta.zone_id,
                entry_type=meta.entry_type,
            )

        # No explicit entry — check if it's an implicit directory.
        # is_implicit_directory is on concrete metastore classes, not the ABC.
        _meta = self._kernel.metadata
        _is_implicit = getattr(_meta, "is_implicit_directory", None)
        if _is_implicit is not None and _is_implicit(normalized):
            return _make_stat_dict(
                path=normalized,
                size=4096,
                etag=None,
                mime_type="inode/directory",
                created_at=None,
                modified_at=None,
                is_directory=True,
                version=0,
                zone_id=ROOT_ZONE_ID,
                entry_type=1,
            )

        return None

    # -- Search operations --

    # Issue #3711: threshold above which lazy trigram index build is worthwhile.
    _TRIGRAM_LAZY_BUILD_THRESHOLD = 500

    def _trigram_index_path(self) -> str:
        """Return the expected trigram index path for this facade's zone."""
        zone_id = self._ctx.zone_id or ROOT_ZONE_ID
        index_dir = os.path.join(os.path.expanduser("~"), ".nexus", "indexes")
        return os.path.join(index_dir, f"{os.path.basename(zone_id)}.trgm")

    # Per-zone guard: prevents duplicate background builds for the same zone.
    _trigram_build_lock = threading.Lock()
    _trigram_builds_in_progress: set[str] = set()

    # Max file size to include in trigram index (skip large binaries).
    _TRIGRAM_MAX_FILE_SIZE = 1024 * 1024  # 1 MB

    def _ensure_trigram_index(self, file_paths: list[str]) -> str | None:
        """Return the trigram index path if it exists, or kick off a background build.

        Issue #3711: The trigram index was never built because
        ``build_trigram_index_for_zone`` had no callers.

        Design: the first grep is NOT slowed down.  If no index exists,
        we start a background thread that builds it from the file list.
        The *current* grep proceeds without the index (full scan).  The
        *next* grep finds the index on disk and uses the fast path.

        Returns the index path when the index already exists, None otherwise.
        """
        index_path = self._trigram_index_path()
        if os.path.isfile(index_path):
            return index_path

        if len(file_paths) < self._TRIGRAM_LAZY_BUILD_THRESHOLD:
            return None

        # Kick off background build (non-blocking).
        self._maybe_build_trigram_background(file_paths, index_path)
        return None

    def _maybe_build_trigram_background(self, file_paths: list[str], index_path: str) -> None:
        """Start a background thread to build the trigram index if not already running."""
        with SlimNexusFS._trigram_build_lock:
            if index_path in SlimNexusFS._trigram_builds_in_progress:
                return
            SlimNexusFS._trigram_builds_in_progress.add(index_path)

        # Snapshot the kernel + ctx references for the background thread.
        kernel = self._kernel
        ctx = self._ctx
        max_size = self._TRIGRAM_MAX_FILE_SIZE

        def _build() -> None:
            try:
                from nexus_kernel import build_trigram_index_from_entries

                entries: list[tuple[str, bytes]] = []
                for fp in file_paths:
                    try:
                        content = kernel.sys_read(fp, context=ctx)
                        if isinstance(content, bytes) and len(content) <= max_size:
                            entries.append((fp, content))
                    except Exception:
                        continue

                if entries:
                    os.makedirs(os.path.dirname(index_path), exist_ok=True)
                    build_trigram_index_from_entries(entries, index_path)
                    logger.debug(
                        "Issue #3711: Built trigram index at %s (%d files)",
                        index_path,
                        len(entries),
                    )
            except Exception:
                logger.debug("Background trigram build failed", exc_info=True)
            finally:
                with SlimNexusFS._trigram_build_lock:
                    SlimNexusFS._trigram_builds_in_progress.discard(index_path)

        thread = threading.Thread(target=_build, daemon=True)
        thread.start()

    def _trigram_candidates(
        self,
        index_path: str,
        pattern: str,
        path: str,
        ignore_case: bool,
    ) -> list[str] | None:
        """Return candidate file paths from trigram index, or None on error."""
        try:
            from nexus_kernel import trigram_search_candidates
        except (ImportError, OSError):
            return None

        try:
            candidates = trigram_search_candidates(index_path, pattern, ignore_case)
        except (OSError, ValueError, RuntimeError):
            return None

        if candidates is None:
            return None

        # Filter to files under the requested path.
        if path != "/":
            prefix = path if path.endswith("/") else path + "/"
            candidates = [c for c in candidates if c.startswith(prefix) or c == path]

        return candidates

    def grep(
        self,
        pattern: str,
        path: str = "/",
        *,
        ignore_case: bool = False,
        max_results: int = 1000,
    ) -> list[dict[str, Any]]:
        """Search file contents for a regex pattern.

        Recursively lists files under *path*, reads their contents, and
        searches using Rust-accelerated regex (nexus_kernel) when available,
        falling back to Python ``re`` otherwise.

        Args:
            pattern: Regex pattern to search for.
            path: Directory to search under (default root).
            ignore_case: Case-insensitive matching.
            max_results: Cap on returned matches.

        Returns:
            List of match dicts with keys: file, line, content, match.
        """
        import re

        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise ValueError(f"Invalid regex pattern: {exc}") from exc

        # List all files first (needed for both lazy index build and fallback).
        entries = self._kernel.sys_readdir(
            path,
            recursive=True,
            details=True,
            context=self._ctx,
        )
        all_files = [
            e["path"] for e in entries if isinstance(e, dict) and not e.get("is_directory", False)
        ]

        # Issue #3711: Lazy-build trigram index on first grep above threshold,
        # then use it to narrow candidates.  Falls back to full scan on miss.
        file_paths = all_files
        index_path = self._ensure_trigram_index(all_files)
        if index_path is not None:
            narrowed = self._trigram_candidates(index_path, pattern, path, ignore_case)
            if narrowed is not None:
                file_paths = narrowed

        matches: list[dict[str, Any]] = []
        # Process in bounded batches for Rust bulk grep when available
        _BATCH_SIZE = 64
        _rust_grep: Any = None
        _has_rust_grep = False
        try:
            from nexus_kernel import grep_bulk

            _rust_grep = grep_bulk
            _has_rust_grep = True
        except (ImportError, OSError):
            pass

        for batch_start in range(0, len(file_paths), _BATCH_SIZE):
            if len(matches) >= max_results:
                break
            batch = file_paths[batch_start : batch_start + _BATCH_SIZE]
            batch_contents: dict[str, bytes] = {}
            for fp in batch:
                try:
                    batch_contents[fp] = self._kernel.sys_read(fp, context=self._ctx)
                except Exception:
                    continue

            if not batch_contents:
                continue

            remaining = max_results - len(matches)

            # Try Rust bulk grep on this batch
            if _has_rust_grep:
                try:
                    batch_results = _rust_grep(pattern, batch_contents, ignore_case, remaining)
                    if batch_results is not None:
                        matches.extend(batch_results)
                        continue
                except (ValueError, RuntimeError):
                    pass

            # Python fallback for this batch
            for fp, content in batch_contents.items():
                try:
                    text = content.decode("utf-8", errors="replace")
                except Exception:
                    continue
                for line_no, line in enumerate(text.splitlines(), 1):
                    m = compiled.search(line)
                    if m:
                        matches.append(
                            {
                                "file": fp,
                                "line": line_no,
                                "content": line,
                                "match": m.group(0),
                            }
                        )
                        if len(matches) >= max_results:
                            return matches
        return matches

    def glob(
        self,
        pattern: str,
        path: str = "/",
    ) -> list[str]:
        """Find files matching a glob pattern.

        Recursively lists files under *path* and filters them using
        Rust-accelerated glob matching (nexus_kernel) when available,
        falling back to Python ``fnmatch`` otherwise.

        Args:
            pattern: Glob pattern (e.g., ``"**/*.py"``, ``"*.txt"``).
            path: Directory to search under (default root).

        Returns:
            List of matching file paths.
        """
        entries = self._kernel.sys_readdir(
            path,
            recursive=True,
            details=False,
            context=self._ctx,
        )
        all_paths = [e for e in entries if isinstance(e, str)]
        if not all_paths:
            return []

        # Try Rust-accelerated glob
        try:
            from nexus_kernel import glob_match_bulk as _rust_glob

            results = _rust_glob([pattern], all_paths)
            if results is not None:
                return list(results)
        except (ImportError, OSError, ValueError, RuntimeError):
            pass

        # Python fallback
        import fnmatch

        return [p for p in all_paths if fnmatch.fnmatch(p, pattern)]

    # -- Mount management (delegated to kernel router) --

    def list_mounts(self) -> list[str]:
        """List all mount points.

        Returns:
            Sorted list of mount point paths.
        """
        _py_kernel = getattr(self._kernel, "_kernel", None)
        if _py_kernel is None:
            return []
        from nexus.core.path_utils import extract_zone_id

        return sorted([extract_zone_id(c)[1] for c in _py_kernel.get_mount_points()])

    def unmount(self, mount_point: str) -> None:
        """Remove a mount and clean up all associated state.

        Removes the mount from the runtime router, deletes its metadata entry
        and all cached child metadata, and removes it from the persisted
        mounts.json so it does not reappear on the next process start.

        Args:
            mount_point: Mount point path (e.g. "/gdrive/my-drive").

        Raises:
            ValueError: If mount_point is not a mounted path.
        """
        from nexus.core.path_utils import validate_path

        normalized = validate_path(mount_point, allow_root=False)
        meta = self._kernel.metadata.get(normalized)
        if meta is None or not meta.is_mount:
            raise ValueError(f"'{normalized}' is not a mount point")

        # 1. Remove from runtime mount table
        self._kernel._driver_coordinator.unmount(normalized)

        # 2. Delete mount root metadata row + evict dcache
        self._kernel.metadata.delete(normalized)
        if hasattr(self._kernel.metadata, "dcache_evict_prefix"):
            self._kernel.metadata.dcache_evict_prefix(normalized + "/")

        # 3. Sweep cached child metadata (best-effort — connector mounts may
        #    have populated entries via the sync loop or explicit writes)
        prefix = normalized.rstrip("/") + "/"
        children = list(self._kernel.metadata.list(prefix))
        if children:
            self._kernel.metadata.delete_batch([c.path for c in children])

        # 4. Remove from mounts.json so the mount does not resurrect on restart
        import contextlib

        with contextlib.suppress(OSError):
            from nexus.fs._paths import load_persisted_mounts, save_persisted_mounts

            existing = load_persisted_mounts()
            # Remove any entry whose derived mount point matches
            from nexus.fs._uri import derive_mount_point, parse_uri

            filtered = []
            for entry in existing:
                try:
                    spec = parse_uri(entry["uri"])
                    mp = derive_mount_point(spec, at=entry.get("at"))
                    if mp != normalized:
                        filtered.append(entry)
                except Exception:
                    filtered.append(entry)
            if len(filtered) != len(existing):
                save_persisted_mounts(filtered, merge=False)

    # -- Lifecycle --

    def close(self) -> None:
        """Close the filesystem and release resources.

        Closes the kernel (NexusFS.close is sync) and then closes
        the metastore's SQLite connection.  Safe to call multiple
        times — subsequent calls are no-ops.
        """
        if self._closed:
            return

        import contextlib

        try:
            _close = getattr(self._kernel, "close", None)
            if _close is not None:
                _close()
        finally:
            with contextlib.suppress(Exception):
                self._kernel.metadata.close()
            self._closed = True

    def __enter__(self) -> SlimNexusFS:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
