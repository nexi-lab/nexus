"""ContentMixin — content I/O operations (sys_read, sys_write, Tier 2 read/write).

Extracts all file content read/write methods from NexusFS. Depends on
InternalMixin (context helpers, .readme overlay, _dispatch_write_events)
and DispatchMixin (resolve_read, resolve_write, resolve_delete) via MRO.

Mixin rules (Phase 6 established):
  • ``from __future__ import annotations`` + TYPE_CHECKING stubs
  • Single stub: ``_kernel: Any`` — other NexusFS attrs accessed via MRO
  • Listed BEFORE NexusFilesystemABC in MRO
  • @rpc_expose decorators stay on mixin methods
  • No new ``type: ignore``
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    ConflictError,
    NexusFileNotFoundError,
)
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext
from nexus.lib.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class ContentMixin:
    """Content I/O: sys_read, sys_write, and Tier 2 convenience methods."""

    _kernel: Any  # Rust Kernel
    _zone_id: str
    metadata: Any
    _driver_coordinator: Any

    # =================================================================
    # Core VFS File Operations (Issue #899)
    # =================================================================

    # =========================================================================
    # VFS I/O Lock — kernel-internal path-level read/write protection
    # =========================================================================

    # VFS I/O locking deleted — Rust kernel LockManager handles all I/O lock
    # acquire/release internally in sys_read/sys_write/sys_copy/sys_unlink/sys_rename.
    #
    # Federation remote content fetch is now handled inside Rust `sys_read`
    # (see `Kernel::try_remote_fetch` in rust/kernel/src/kernel.rs): when
    # metadata exists but the local CAS blob doesn't, Rust parses the origin
    # from `backend_name` and pulls the blob via `ZoneApiService.ReadBlob`
    # (R20.18.7 co-located on the raft port).

    @rpc_expose(description="Read file content")
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread(2)).

        Thin async wrapper around Rust Kernel.sys_read (pure Rust, zero GIL).
        DT_PIPE/DT_STREAM, resolve, and hooks are [TRANSITIONAL] — migrates
        to Rust dispatch middleware in PR 7.
        """
        # DT_PIPE/DT_STREAM: Rust IPC registry handles all backends
        # (memory, SHM, remote) via PipeManager/StreamManager.

        path = self._validate_path(path)
        context = self._parse_context(context)
        _handled, _resolve_hint = self.resolve_read(path, context=context)
        if _handled:
            content = _resolve_hint or b""
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        _is_admin = (
            getattr(context, "is_admin", False)
            if context is not None and not isinstance(context, dict)
            else (context.get("is_admin", False) if isinstance(context, dict) else False)
        )

        # ── KERNEL (Rust — pre-hooks + route + backend read) ──
        # DT_REG: Rust returns data on success or raises NexusFileNotFoundError
        # (federation remote fetch handled internally via try_remote_fetch).
        # External connector mounts are now handled by Rust-registered native
        # backends — no Python re-routing needed.
        # DT_PIPE / DT_STREAM: entry_type signals IPC dispatch below.
        #
        # Slim-package mode: ``nexus-fs`` can ship without ``nexus_kernel``,
        # in which case ``self._kernel`` is None.
        if self._kernel is None:
            raise NexusFileNotFoundError(path)
        _rust_ctx = self._build_rust_ctx(context, _is_admin)
        result = self._kernel.sys_read(path, _rust_ctx)

        # DT_PIPE: result.data is the popped frame when available; None = empty.
        if result.entry_type == 3:  # DT_PIPE
            if result.data is not None:
                data = result.data
                if offset or count is not None:
                    data = data[offset : offset + count] if count is not None else data[offset:]
                return data
            # Empty pipe — try nowait (hot path), then block in Rust (GIL-free)
            _data = self._kernel.pipe_read_nowait(path)
            if _data is not None:
                if offset or count is not None:
                    _data = _data[offset : offset + count] if count is not None else _data[offset:]
                return bytes(_data)
            _data = self._kernel.pipe_read_blocking(path, 5000)
            if offset or count is not None:
                _data = _data[offset : offset + count] if count is not None else _data[offset:]
            return bytes(_data)

        # DT_STREAM: blocking reads with offset tracking
        if result.entry_type == 4:  # DT_STREAM
            _result = self._kernel.stream_read_at(path, offset)
            if _result is not None:
                return bytes(_result[0])
            # Slow path — block in Rust (GIL-free)
            _data, _next = self._kernel.stream_read_at_blocking(path, offset, 30000)
            return bytes(_data)

        # DT_REG: Rust guarantees data is set on success.
        data = result.data or b""

        if offset or count is not None:
            data = data[offset : offset + count] if count is not None else data[offset:]

        # POST-INTERCEPT: hooks dispatched via Rust dispatch_post_hooks
        if result.post_hook_needed:
            zone_id, agent_id, _ = self._get_context_identity(context)
            from nexus.contracts.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                content=data,
                content_id=result.content_id,
            )
            self._kernel.dispatch_post_hooks("read", _read_ctx)
            data = _read_ctx.content or data

        return data

    @rpc_expose(description="Read multiple files in a single RPC call")
    def read_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """
        Read multiple files in a single RPC call for improved performance.

        This method is optimized for bulk operations like grep, where many files
        need to be read. It batches permission checks and reduces RPC overhead.

        Args:
            paths: List of virtual paths to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dicts with content and metadata
            skip_errors: If True, skip files that can't be read and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> content (or None if skip_errors=True and read failed)
            If return_metadata=False: {path: bytes}
            If return_metadata=True: {path: {content, content_id, version, ...}}

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Reduced network round trips
            - Expected speedup: 2-5x for 50+ files

        Examples:
            >>> # Read multiple files at once
            >>> results = nx.read_bulk(["/file1.txt", "/file2.txt", "/file3.txt"])
            >>> print(results["/file1.txt"])  # b'content'
            >>> print(results["/file2.txt"])  # b'content' or None if failed

            >>> # With metadata
            >>> results = nx.read_bulk(["/file1.txt"], return_metadata=True)
            >>> print(results["/file1.txt"]["content"])
            >>> print(results["/file1.txt"]["content_id"])
        """

        bulk_start = time.time()
        results: dict[str, bytes | dict[str, Any] | None] = {}

        # Small-batch fast path: <=4 paths → sequential sys_read (no batch overhead).
        # Avoids permission-check batching, metadata batching, and logging for tiny requests.
        if len(paths) <= 4:
            zone_id, agent_id, is_admin = self._get_context_identity(context)
            _rust_ctx = self._build_rust_ctx(context, is_admin)
            for path in paths:
                try:
                    vpath = self._validate_path(path)
                    result = self._kernel.sys_read(vpath, _rust_ctx)
                    content = result.data or b""
                    if return_metadata:
                        meta = self.metadata.get(vpath)
                        results[path] = {
                            "content": content,
                            "content_id": meta.content_id if meta else None,
                            "version": meta.version if meta else 0,
                            "modified_at": meta.modified_at if meta else None,
                            "size": len(content),
                        }
                    else:
                        results[path] = content
                except NexusFileNotFoundError:
                    if skip_errors:
                        results[path] = None
                    else:
                        raise
                except Exception as e:
                    logger.warning(
                        "[READ-BULK] Failed to read %s: %s: %s", path, type(e).__name__, e
                    )
                    if skip_errors:
                        results[path] = None
                    else:
                        raise
            return results

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in read_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check via shared helper (hook_count fast path).
        perm_start = time.time()
        try:
            allowed_set = self._batch_permission_check(validated_paths, context)
        except Exception as e:
            logger.error("[READ-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
            allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[READ-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Read allowed files via Rust kernel sys_read (single path per call).
        # Rust kernel handles: validate → route → dcache → metastore → backend read.
        read_start = time.time()
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        _rust_ctx = self._build_rust_ctx(context, is_admin)

        # Batch metadata lookup (needed for return_metadata=True)
        batch_meta: dict[str, FileMetadata | None] | None = None
        if return_metadata:
            meta_start = time.time()
            batch_meta = self.metadata.get_batch(list(allowed_set))
            meta_elapsed = (time.time() - meta_start) * 1000
            logger.info(
                f"[READ-BULK] Batch metadata lookup: {len(batch_meta)} paths in {meta_elapsed:.1f}ms"
            )

        for path in allowed_set:
            try:
                bulk_content: bytes | None = None
                try:
                    result = self._kernel.sys_read(path, _rust_ctx)
                    bulk_content = result.data or b""
                except NexusFileNotFoundError:
                    bulk_content = None
                if bulk_content is None:
                    if skip_errors:
                        results[path] = None
                        continue
                    raise NexusFileNotFoundError(path)
                content = bulk_content
                if return_metadata:
                    assert batch_meta is not None
                    meta = batch_meta.get(path)
                    results[path] = {
                        "content": bulk_content,
                        "content_id": meta.content_id if meta else None,
                        "version": meta.version if meta else 0,
                        "modified_at": meta.modified_at if meta else None,
                        "size": len(bulk_content),
                    }
                else:
                    results[path] = bulk_content
            except NexusFileNotFoundError:
                if skip_errors:
                    results[path] = None
                else:
                    raise
            except Exception as e:
                logger.warning("[READ-BULK] Failed to read %s: %s: %s", path, type(e).__name__, e)
                if skip_errors:
                    results[path] = None
                else:
                    raise

        read_elapsed = time.time() - read_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[READ-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, read={read_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Read a byte range from a file")
    def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """
        Read a specific byte range from a file.

        This method enables memory-efficient streaming by allowing clients to
        fetch file content in chunks without loading the entire file into memory.

        Args:
            path: Virtual path to read
            start: Start byte offset (inclusive, 0-indexed)
            end: End byte offset (exclusive)
            context: Optional operation context for permission checks

        Returns:
            bytes: Content from start to end (exclusive)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission
            ValueError: If start/end are invalid (negative, start > end, etc.)

        Example:
            >>> # Read first 1MB of a large file
            >>> chunk = nx.read_range("/workspace/large.bin", 0, 1024 * 1024)

            >>> # Stream a file in chunks
            >>> offset = 0
            >>> chunk_size = 65536
            >>> while True:
            ...     chunk = nx.read_range("/workspace/large.bin", offset, offset + chunk_size)
            ...     if not chunk:
            ...         break
            ...     process(chunk)
            ...     offset += len(chunk)
        """
        # Validate range parameters
        if start < 0:
            raise ValueError(f"start must be non-negative, got {start}")
        if end < start:
            raise ValueError(f"end ({end}) must be >= start ({start})")

        path = self._validate_path(path)
        context = self._parse_context(context)

        # FAST PATH: check virtual path resolvers first
        _handled, _resolve_hint = self.resolve_read(path, context=context)
        if _handled:
            return (_resolve_hint or b"")[start:end]

        # Use Rust sys_read with offset/count for range reads
        content = self.sys_read(path, count=end, offset=0, context=context)
        return content[start:end]

    @rpc_expose(description="Stream file content in chunks")
    def stream(
        self, path: str, chunk_size: int = 65536, context: OperationContext | None = None
    ) -> Any:
        """
        Stream file content in chunks without loading entire file into memory.

        This is a memory-efficient alternative to read() for large files.
        Yields chunks as an iterator, allowing processing of files larger than RAM.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If stream operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> # Stream large file efficiently
            >>> for chunk in nx.stream("/workspace/large_file.bin"):
            ...     process(chunk)  # Memory usage = chunk_size, not file_size

            >>> # Stream to output
            >>> import sys
            >>> for chunk in nx.stream("/workspace/video.mp4", chunk_size=1024*1024):  # 1MB chunks
            ...     sys.stdout.buffer.write(chunk)
        """
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")

        path = self._validate_path(path)

        # Route through sys_read — Rust handles pre-hooks, CAS, federation,
        # external connector dispatch.  Chunked Rust reads are future work.
        data = self.sys_read(path, context=context)
        for pos in range(0, len(data), chunk_size):
            yield data[pos : pos + chunk_size]

    @rpc_expose(description="Stream a byte range of file content")
    def stream_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 65536,
        context: OperationContext | None = None,
    ) -> Any:
        """Stream a byte range [start, end] of file content.

        This is the kernel-level range streaming method.  HTTP routers use
        this (via ``build_range_response``) to implement RFC 9110 Range
        requests without bypassing the ObjectStore abstraction.

        Args:
            path: Virtual path to stream
            start: Start byte offset (inclusive)
            end: End byte offset (inclusive)
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content within the requested range
        """
        if start < 0:
            raise ValueError(f"start must be non-negative, got {start}")
        if end < start:
            raise ValueError(f"end ({end}) must be >= start ({start})")
        if chunk_size <= 0:
            raise ValueError(f"chunk_size must be positive, got {chunk_size}")

        path = self._validate_path(path)

        # Route through sys_read with offset/count — Rust handles hooks,
        # CAS routing, federation, external connectors.
        data = self.sys_read(path, count=end - start + 1, offset=start, context=context)
        for pos in range(0, len(data), chunk_size):
            yield data[pos : pos + chunk_size]

    @rpc_expose(description="Write file content from stream")
    def write_stream(
        self,
        path: str,
        chunks: Iterator[bytes],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Write file content from an iterator of chunks.

        This is a memory-efficient alternative to write() for large files.
        Accepts chunks as an iterator, computing hash incrementally.

        Args:
            path: Virtual path to write
            chunks: Iterator yielding byte chunks
            context: Optional operation context for permission checks

        Returns:
            Dict with metadata about the written file:
                - content_id: Content hash of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only or user doesn't have write permission

        Example:
            >>> # Stream large file without loading into memory
            >>> def file_chunks(path, chunk_size=8192):
            ...     with open(path, 'rb') as f:
            ...         while chunk := f.read(chunk_size):
            ...             yield chunk
            >>> result = nx.write_stream("/workspace/large.bin", file_chunks("/tmp/large.bin"))
        """
        path = self._validate_path(path)

        # Collect chunks and delegate to write() — Rust sys_write handles
        # CAS, metastore, OBSERVE, hooks atomically.
        data = b"".join(chunks)
        return self.write(path, data, context=context)

    @rpc_expose(description="Write file content")
    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Write content to a file (POSIX write(2)).

        Thin async wrapper around Rust Kernel.sys_write (CAS I/O is pure Rust,
        zero GIL). Metastore.put stays in Python [TRANSITIONAL] — migrates to
        Rust metastore in PR 7.
        """
        # Normalize input
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        # [TRANSITIONAL] PRE-DISPATCH: resolve — migrates to Rust dispatch middleware in PR 7
        context = self._parse_context(context)

        _handled, _result = self.resolve_write(path, buf)
        if _handled:
            base: dict[str, Any] = {"path": path, "bytes_written": len(buf)}
            if isinstance(_result, dict):
                base.update(_result)
            return base

        # ── KERNEL (pure Rust — DT_REG via CAS, DT_PIPE/DT_STREAM via IPC, zero GIL) ──
        _is_admin = (
            getattr(context, "is_admin", False)
            if context is not None and not isinstance(context, dict)
            else (context.get("is_admin", False) if isinstance(context, dict) else False)
        )
        _rust_ctx = self._build_rust_ctx(context, _is_admin)
        result = self._kernel.sys_write(path, _rust_ctx, buf, offset)

        # POST-INTERCEPT hooks (Rust handles backend write + metadata + OBSERVE)
        if result.hit and result.post_hook_needed:
            # Rust wrote to backend (CAS or PAS) + built metadata + updated dcache.
            # old_metadata fields come from Rust (dcache/metastore snapshot taken
            # before the write) — no Python metadata.get() round-trip needed.
            zone_id, agent_id, _ = self._get_context_identity(context)
            _old_metadata: FileMetadata | None = None
            if not result.is_new:
                _mod_at = (
                    datetime.fromtimestamp(result.old_modified_at_ms / 1000.0, UTC)
                    if result.old_modified_at_ms is not None
                    else None
                )
                _old_metadata = FileMetadata(
                    path=path,
                    size=result.old_size or 0,
                    content_id=result.old_content_id,
                    version=result.old_version or 1,
                    modified_at=_mod_at,
                )
            from nexus.contracts.vfs_hooks import WriteHookContext

            _ctx = context or OperationContext(user_id="anonymous", groups=[])
            _meta_obj = FileMetadata(
                path=path,
                size=result.size,
                content_id=result.content_id,
                version=result.version,
                zone_id=zone_id,
            )
            self._kernel.dispatch_post_hooks(
                "write",
                WriteHookContext(
                    path=path,
                    content=buf,
                    context=_ctx,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    is_new_file=result.is_new,
                    content_id=result.content_id or "",
                    metadata=_meta_obj,
                    old_metadata=_old_metadata,
                    new_version=result.version,
                ),
            )

        return {"path": path, "bytes_written": len(buf)}

    @rpc_expose(description="Read file with optional metadata")
    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read with optional metadata (VFS convenience).

        Composes sys_stat + sys_read.  POSIX pread semantics.

        Args:
            path: Virtual file path.
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Operation context.
            return_metadata: If True, return dict with content + metadata.

        Returns:
            bytes if return_metadata=False, else dict with content + metadata.
        """
        content = self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Compose with sys_stat for metadata
        meta_dict = self.sys_stat(path, context=context)
        result: dict[str, Any] = {"content": content}
        if meta_dict:
            result.update(
                {
                    "content_id": meta_dict.get("content_id"),
                    "version": meta_dict.get("version"),
                    "modified_at": meta_dict.get("modified_at"),
                    "size": len(content),
                }
            )
        return result

    @rpc_expose(description="Write file with metadata return")
    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        """Write with metadata return (Tier 2 convenience).

        Thin wrapper over ``Kernel::sys_write`` (F2 C4). The kernel owns
        routing, the VFS write lock, backend content write, metadata build,
        per-mount metastore.put, and the OBSERVE dispatch. Python dispatches
        INTERCEPT POST hooks and returns a metadata dict.

        OCC (if_match, if_none_match) is NOT here — use ``lib.occ.occ_write()``
        to compose OCC + write at the caller level (RPC handler, CLI, SDK).

        Distributed locking is NOT here — use ``lock()``/``unlock()`` or
        ``with locked(path)`` to compose locking at the caller level.
        See Issue #1323.

        Args:
            path: Virtual file path.
            buf: File content as bytes or str.
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset for partial write (POSIX pwrite semantics).
                0 (default) is a full-file write. >0 splices ``buf`` at
                ``offset`` within the existing file; gap past EOF is
                zero-filled. Threaded into ``Kernel::sys_write`` (R20.10).
            context: Operation context.
            ttl: TTL in seconds for ephemeral content (Issue #3405). Threaded
                onto the context's ``ttl_seconds`` field; kernel hot path
                picks it up if the mount supports TTL bucketing.

        Returns:
            Dict with metadata (content_id, version, modified_at, size).
        """

        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (e.g. /__sys__ writers).
        _handled, _result = self.resolve_write(path, buf)
        if _handled:
            return _result

        # Thread TTL into context (Issue #3405)
        if ttl is not None and ttl > 0:
            context = self._ensure_context_ttl(context, ttl)

        # Route through Rust sys_write — handles create-on-write, backend I/O,
        # metadata build+put, dcache update, and OBSERVE dispatch.
        context = self._parse_context(context)
        _is_admin = getattr(context, "is_admin", False) if context else False
        _rust_ctx = self._build_rust_ctx(context, _is_admin)

        result = self._kernel.sys_write(path, _rust_ctx, buf, offset)

        # Reconstruct old_metadata from Rust result (atomic snapshot taken
        # during write — no TOCTOU gap, no extra PyO3 round-trip).
        _old_meta: FileMetadata | None = None
        if not result.is_new:
            _mod_at = (
                datetime.fromtimestamp(result.old_modified_at_ms / 1000.0, UTC)
                if result.old_modified_at_ms is not None
                else None
            )
            _old_meta = FileMetadata(
                path=path,
                size=result.old_size or 0,
                content_id=result.old_content_id,
                version=result.old_version or 1,
                modified_at=_mod_at,
            )

        # POST-INTERCEPT hooks
        zone_id, agent_id, _ = self._get_context_identity(context)
        _ctx = context or OperationContext(user_id="anonymous", groups=[])
        _cid = result.content_id or ""
        from nexus.contracts.vfs_hooks import WriteHookContext

        self._kernel.dispatch_post_hooks(
            "write",
            WriteHookContext(
                path=path,
                content=buf,
                context=_ctx,
                zone_id=zone_id,
                agent_id=agent_id,
                is_new_file=result.is_new,
                content_id=_cid,
                metadata=FileMetadata(
                    path=path,
                    size=result.size,
                    content_id=result.content_id,
                    version=result.version,
                    zone_id=zone_id,
                ),
                old_metadata=_old_meta,
                new_version=result.version,
            ),
        )

        return {
            "content_id": _cid,
            "version": result.version,
            "modified_at": None,
            "size": result.size,
        }

    # _write_internal + _write_content deleted — Rust sys_write handles:
    # routing, VFS lock, backend write, metadata build+put, dcache update.
    # write() and sys_write() call Rust kernel directly.

    def atomic_update(
        self,
        path: str,
        update_fn: Callable[[bytes], bytes],
        context: OperationContext | None = None,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> dict[str, Any]:
        """Atomically read-modify-write a file with distributed locking.

        This is the recommended API for concurrent file updates where you need
        to read existing content, modify it, and write back atomically.

        The operation:
        1. Acquires distributed lock on the path
        2. Reads current file content
        3. Applies your update function
        4. Writes modified content
        5. Releases lock (even on failure)

        For multiple operations within one lock, use ``with locked()`` instead.

        Args:
            path: Virtual path to update
            update_fn: Function that transforms content (bytes -> bytes).
                      Receives current file content, returns new content.
            context: Operation context (optional)
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds (default: 30.0)

        Returns:
            Dict with metadata about the written file:
                - content_id: Content hash of the new content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read or write operation fails

        Example:
            >>> # Increment a counter atomically
            >>> import json
            >>> nx.atomic_update(
            ...     "/counters/visits.json",
            ...     lambda c: json.dumps({"count": json.loads(c)["count"] + 1}).encode()
            ... )

            >>> # Append to a log file atomically
            >>> nx.atomic_update(
            ...     "/logs/access.log",
            ...     lambda c: c + b"New log entry\\n"
            ... )

            >>> # Update config safely across multiple agents
            >>> nx.atomic_update(
            ...     "/shared/config.json",
            ...     lambda c: json.dumps({**json.loads(c), "version": 2}).encode()
            ... )
        """
        lock_id = self.lock(path, timeout=timeout, ttl=ttl, context=context)
        if lock_id is None:
            from nexus.contracts.exceptions import LockTimeout

            raise LockTimeout(path=path, timeout=timeout)
        try:
            content = self.sys_read(path, context=context)
            new_content = update_fn(content)
            return self.write(path, new_content, context=context)
        finally:
            self.unlock(lock_id, path, context=context)

    @rpc_expose(description="Append content to an existing file or create if it doesn't exist")
    def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Append content to an existing file or create a new file if it doesn't exist.

        This is an efficient way to add content to files without reading the entire
        file separately, particularly useful for:
        - Writing JSONL (JSON Lines) logs incrementally
        - Appending to log files
        - Building append-only data structures
        - Streaming data collection

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str (str will be UTF-8 encoded)
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional content_id for optimistic concurrency control.
                     If provided, append only succeeds if current file content_id matches this value.
                     Prevents concurrent modification conflicts.
            force: If True, skip version check and append unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - content_id: Content hash (SHA-256) of the final content (after append)
                - version: New version number
                - modified_at: Modification timestamp
                - size: Final file size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If append operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current content_id
            NexusFileNotFoundError: If file doesn't exist during read (should not happen in normal flow)

        Examples:
            >>> # Append to a log file
            >>> nx.append("/workspace/app.log", "New log entry\\n")

            >>> # Build JSONL file incrementally
            >>> import json
            >>> for record in records:
            ...     line = json.dumps(record) + "\\n"
            ...     nx.append("/workspace/data.jsonl", line)

            >>> # Append with optimistic concurrency control
            >>> result = nx.read("/workspace/log.txt", return_metadata=True)
            >>> try:
            ...     nx.append("/workspace/log.txt", "New entry\\n", if_match=result['content_id'])
            ... except ConflictError:
            ...     print("File was modified by another process!")

            >>> # Create new file if doesn't exist
            >>> nx.append("/workspace/new.txt", "First line\\n")
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Try to read existing content if file exists
        # For non-existent files, we'll create them (existing_content stays empty)
        existing_content = b""
        try:
            result = self.read(path, context=context, return_metadata=True)
            # Tier 2 read(return_metadata=True) always returns dict
            assert isinstance(result, dict), "Expected dict when return_metadata=True"

            existing_content = result["content"]

            # If if_match is provided, verify it matches current content_id
            # (the write call will also check, but we check here to fail fast)
            if if_match is not None and not force:
                current_etag = result.get("content_id")
                if current_etag != if_match:
                    from nexus.contracts.exceptions import ConflictError

                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=current_etag or "(no content_id)",
                    )
        except Exception as e:
            # If file doesn't exist, treat as empty (will create new file)
            from nexus.contracts.exceptions import NexusFileNotFoundError

            if not isinstance(e, NexusFileNotFoundError):
                # Re-raise unexpected errors (including PermissionError)
                raise
            # For FileNotFoundError, continue with empty content
            # write() will check if user has permission to create the file

        # Combine existing content with new content
        final_content = existing_content + content

        # Use the existing write method to handle all the complexity:
        # - Permission checking
        # - Version management
        # - Audit logging
        # - Workflow triggers
        # - Parent tuple creation
        # OCC check already done above (line 2985-2996), so just write.
        return self.write(
            path,
            final_content,
            context=context,
        )

    @rpc_expose(description="Apply surgical search/replace edits to a file")
    def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]] | list[Any],
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """
        Apply surgical search/replace edits to a file.

        This enables precise file modifications without rewriting entire files,
        reducing token cost and errors when used with LLMs.

        Issue #800: Add edit engine with search/replace for surgical file edits.

        Uses a layered matching strategy:
        1. Exact match (fast path)
        2. Whitespace-normalized match
        3. Fuzzy match (Levenshtein similarity)

        Args:
            path: Virtual path to edit
            edits: List of edit operations. Each edit can be:
                - Tuple: (old_str, new_str) - simple search/replace
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool} - full control
                - EditOperation: Direct EditOperation instance
            context: Optional operation context for permission checks
            if_match: Optional content_id for optimistic concurrency control.
                If provided, edit fails if file changed since read.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
                Default 0.85. Use 1.0 for exact matching only.
            preview: If True, return preview without writing. Default False.

        Returns:
            Dict containing:
                - success: bool - True if all edits applied
                - diff: str - Unified diff of changes
                - matches: list[dict] - Info about each match (type, line, similarity)
                - applied_count: int - Number of edits applied
                - content_id: str - New content_id (if not preview)
                - version: int - New version (if not preview)
                - errors: list[str] - Error messages if any edits failed

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current content_id

        Examples:
            >>> # Simple search/replace
            >>> result = nx.edit("/code/main.py", [
            ...     ("def foo():", "def bar():"),
            ...     ("return x", "return x + 1"),
            ... ])
            >>> print(result['diff'])

            >>> # With optimistic concurrency
            >>> content = nx.read("/code/main.py", return_metadata=True)
            >>> result = nx.edit(
            ...     "/code/main.py",
            ...     [("old_text", "new_text")],
            ...     if_match=content['content_id']
            ... )

            >>> # Preview without writing
            >>> result = nx.edit("/code/main.py", edits, preview=True)
            >>> if result['success']:
            ...     print(result['diff'])

            >>> # With fuzzy matching
            >>> result = nx.edit("/code/main.py", [
            ...     {"old_str": "def foo():", "new_str": "def bar():", "hint_line": 42}
            ... ], fuzzy_threshold=0.8)
        """
        from nexus.utils.edit_engine import EditEngine
        from nexus.utils.edit_engine import EditOperation as EditOp

        path = self._validate_path(path)

        # Read current content with metadata (via Tier 2 convenience)
        result = self.read(path, context=context, return_metadata=True)
        assert isinstance(result, dict), "Expected dict when return_metadata=True"

        content_bytes: bytes = result["content"]
        current_etag = result.get("content_id")

        # Check content_id if provided (optimistic concurrency control)
        if if_match is not None and current_etag != if_match:
            raise ConflictError(
                path=path,
                expected_etag=if_match,
                current_etag=current_etag or "(no content_id)",
            )

        # Decode content to string for editing
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return {
                "success": False,
                "diff": "",
                "matches": [],
                "applied_count": 0,
                "errors": [f"File is not valid UTF-8 text: {e}"],
            }

        # Convert edits to EditOperation instances
        edit_operations: list[EditOp] = []
        for edit in edits:
            if isinstance(edit, EditOp):
                edit_operations.append(edit)
            elif isinstance(edit, tuple | list) and len(edit) >= 2:
                # Handle both tuple and list (JSON deserializes tuples as lists)
                edit_operations.append(EditOp(old_str=edit[0], new_str=edit[1]))
            elif isinstance(edit, dict):
                edit_operations.append(
                    EditOp(
                        old_str=edit["old_str"],
                        new_str=edit["new_str"],
                        hint_line=edit.get("hint_line"),
                        allow_multiple=edit.get("allow_multiple", False),
                    )
                )
            else:
                return {
                    "success": False,
                    "diff": "",
                    "matches": [],
                    "applied_count": 0,
                    "errors": [
                        f"Invalid edit format: expected tuple (old, new), dict, or EditOperation, got {type(edit)}"
                    ],
                }

        # Apply edits
        engine = EditEngine(
            fuzzy_threshold=fuzzy_threshold,
            enable_fuzzy=fuzzy_threshold < 1.0,
        )
        edit_result = engine.apply_edits(content, edit_operations)

        # Convert matches to serializable dicts
        matches_list = [
            {
                "edit_index": m.edit_index,
                "match_type": m.match_type,
                "similarity": m.similarity,
                "line_start": m.line_start,
                "line_end": m.line_end,
                "original_text": m.original_text[:200] if m.original_text else "",
                "search_strategy": m.search_strategy,
                "match_count": m.match_count,
            }
            for m in edit_result.matches
        ]

        # If edits failed, return error without writing
        if not edit_result.success:
            return {
                "success": False,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "errors": edit_result.errors,
            }

        # If preview mode, return without writing
        if preview:
            return {
                "success": True,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "preview": True,
                "new_content": edit_result.content,
            }

        # Write the edited content. OCC check already done above (line 3117-3123).
        new_content_bytes = edit_result.content.encode("utf-8")
        write_result = self.write(
            path,
            new_content_bytes,
            context=context,
        )

        return {
            "success": True,
            "diff": edit_result.diff,
            "matches": matches_list,
            "applied_count": edit_result.applied_count,
            "content_id": write_result.get("content_id"),
            "version": write_result.get("version"),
            "size": write_result.get("size"),
            "modified_at": write_result.get("modified_at"),
        }

    @rpc_expose(description="Write multiple files in a single transaction")
    def write_batch(
        self, files: list[tuple[str, bytes]], context: OperationContext | None = None
    ) -> list[dict[str, Any]]:
        """
        Write multiple files in a single round-trip for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        **Atomicity**: best-effort. For CAS backends (the common case) each file
        is written independently via content-addressed storage, so a mid-batch
        failure leaves already-written files on disk. No rollback or compensation
        is performed. Callers that need true all-or-nothing semantics should use
        separate write() calls inside an explicit transaction (if supported) or
        implement idempotent retries using the returned etags.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            List of metadata dicts for each file (in same order as input):
                - content_id: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If any path is read-only or user doesn't have write permission

        Examples:
            >>> # Write 100 small files in a single batch (13x faster!)
            >>> files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
            >>> results = nx.write_batch(files)
            >>> print(f"Wrote {len(results)} files")

            >>> # Best-effort batch write (not all-or-nothing; see docstring)
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
        """
        if not files:
            return []

        # Validate paths
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            validated_files.append((validated_path, content))

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        paths = [p for p, _ in validated_files]

        # Get existing metadata for pre-hooks and is_new detection
        existing_metadata = self.metadata.get_batch(paths)

        # PRE-INTERCEPT: pre-write hooks per file in batch
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        for path in paths:
            meta = existing_metadata.get(path)
            self._kernel.dispatch_pre_hooks(
                "write",
                _WHC(
                    path=path,
                    content=b"",
                    context=context,
                    old_metadata=meta,
                ),
            )

        # ── KERNEL: Rust batch write (validate + route + lock + write + metastore + dcache) ──
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        rust_results = self._kernel._write_batch(validated_files, _rust_ctx)

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        for i, (path, content) in enumerate(validated_files):
            r = rust_results[i]
            if r.hit:
                results.append(
                    {
                        "content_id": r.content_id,
                        "version": r.version,
                        "modified_at": now,
                        "size": r.size,
                    }
                )
                metadata_list.append(
                    FileMetadata(
                        path=path,
                        size=r.size,
                        content_id=r.content_id,
                        version=r.version,
                        zone_id=zone_id or ROOT_ZONE_ID,
                    )
                )
            else:
                # Fallback: Rust batch missed — use sys_write for single file
                wr = self.sys_write(path, content, context=context)
                results.append(
                    {
                        "content_id": wr.get("content_id", ""),
                        "version": wr.get("version", 1),
                        "modified_at": now,
                        "size": len(content),
                    }
                )
                metadata_list.append(
                    FileMetadata(
                        path=path,
                        size=len(content),
                        content_id=wr.get("content_id", ""),
                        version=wr.get("version", 1),
                        zone_id=zone_id or ROOT_ZONE_ID,
                    )
                )

        # Rust _write_batch already persisted metadata (commit_metadata per-mount
        # + ms.put_batch for global items) and updated dcache. No Python put needed.

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        items = [
            (metadata, existing_metadata.get(metadata.path) is None) for metadata in metadata_list
        ]
        from nexus.contracts.vfs_hooks import WriteBatchHookContext

        self._dispatch_batch_post_hook(
            "write_batch",
            WriteBatchHookContext(items=items, context=context, zone_id=zone_id, agent_id=agent_id),
        )

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        for metadata in metadata_list:
            old_meta = existing_metadata.get(metadata.path)
            _ = old_meta is None  # is_new removed with notify

        # Issue #1682: Hierarchy tuples + owner grants moved to post_write_batch hooks.

        return results

    def _dispatch_batch_post_hook(self, event_name: str, ctx: Any) -> None:
        """Dispatch a post-batch hook if any listeners are registered.

        Shared by write_batch and read_batch to avoid duplicating the
        hook_count guard + dispatch_post_hooks call.
        """
        if self._kernel.hook_count(event_name) > 0:
            self._kernel.dispatch_post_hooks(event_name, ctx)

    @rpc_expose(description="Read multiple files atomically in a single round-trip")
    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """
        Read multiple files in a single round-trip for improved performance.

        Uses the Rust kernel's parallel _read_batch (rayon par_iter) for all
        paths, then a single metadata.get_batch() call — no N+1 queries.

        Args:
            paths:   List of virtual paths to read.
            partial: If False (default), raises NexusFileNotFoundError on
                     the first path that is missing or inaccessible.
                     If True, returns a per-item result for every path
                     (successful reads and errors alike).
            context: Optional operation context for permission checks.

        Returns:
            List of dicts in the same order as *paths*.

            Successful item::

                {
                    "path":        str,
                    "content":     bytes,
                    "content_id":  str | None,   # from actual read bytes (r.content_id)
                    "version":     int,           # from pre-read metadata snapshot
                    "modified_at": datetime | None,  # from pre-read metadata snapshot
                    "size":        int,
                }

            **Note on consistency**: ``content_id`` reflects the actual bytes returned
            (authoritative). ``version`` and ``modified_at`` come from a metadata
            snapshot taken *before* the reads, so under concurrent writes they
            may not match the returned content. Use ``content_id`` for cache validation
            or optimistic concurrency; do not rely on ``version``/``modified_at``
            being coherent with the content under concurrent updates.

            Failed item (only possible when partial=True)::

                {
                    "path":  str,
                    "error": "not_found",
                }

        Raises:
            InvalidPathError:       If any path is invalid (always, even in partial mode).
            NexusFileNotFoundError: If any path is missing and partial=False.
            NexusPermissionError:   If access is denied and partial=False.
        """
        if not paths:
            return []

        # Validate all paths up-front — invalid paths always raise, even in partial mode.
        validated_paths: list[str] = [self._validate_path(p) for p in paths]

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        _rust_ctx = self._build_rust_ctx(context, is_admin)

        # PRE-INTERCEPT: batch permission check via shared helper.
        allowed_set = self._batch_permission_check(validated_paths, context)
        denied_paths = set(validated_paths) - allowed_set
        if denied_paths and not partial:
            from nexus.contracts.exceptions import NexusPermissionError

            _first = next(iter(denied_paths))
            raise NexusPermissionError(f"Permission denied: {_first}")
        allowed_paths: list[str] = [p for p in validated_paths if p in allowed_set]

        # Batch metadata fetch — one query for all allowed paths.
        batch_meta = self.metadata.get_batch(allowed_paths) if allowed_paths else {}

        # Finding #3 — DoS guard: reject batches whose declared metadata size exceeds
        # the per-request ceiling.  Uses metadata sizes already fetched, so no extra
        # round-trip is needed.  External-mount / virtual paths that lack metadata
        # entries contribute 0 to the total; their own backends enforce their limits.
        #
        # IMPORTANT: iterate over allowed_paths (with duplicates), NOT over
        # batch_meta.values() (unique keys).  A request repeating the same large file
        # N times would otherwise bypass the cap since the dict only stores one entry
        # per unique path.
        _MAX_BATCH_READ_BYTES = 100 * 1024 * 1024  # 100 MB
        if allowed_paths and batch_meta:
            _total_declared = sum(
                batch_meta[p].size
                for p in allowed_paths
                if batch_meta.get(p) is not None  # value may be None for missing files
            )
            if _total_declared > _MAX_BATCH_READ_BYTES:
                raise ValueError(
                    f"Batch read aggregate declared size {_total_declared} bytes exceeds "
                    f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                )

        # KERNEL: parallel Rust read for all allowed paths.
        rust_results = self._kernel._read_batch(allowed_paths, _rust_ctx) if allowed_paths else []

        results: list[dict[str, Any]] = []
        hit_items: list[tuple[str, "FileMetadata | None"]] = []  # for post-hooks

        # Check once whether any per-file "read" post-hooks are registered.
        # These hooks (e.g. DynamicViewerReadHook) may transform or redact content.
        # Finding #1 — we must fire them per-item so batch semantics match single read().
        _has_read_hooks = self._kernel.hook_count("read") > 0

        # Map allowed_paths → rust_results (same order, guaranteed by _read_batch).
        allowed_iter = iter(rust_results)

        # Cumulative byte counter — tracks actual bytes loaded across both the
        # CAS fast path and the fallback read() path.  External/virtual paths have
        # no metadata entry so they contribute 0 to the upfront declared-size check;
        # their actual content is captured here to close that gap.
        _loaded_bytes = 0

        for path in validated_paths:
            if path in denied_paths:
                results.append({"path": path, "error": "permission_denied"})
                continue

            r = next(allowed_iter)
            meta = batch_meta.get(path)

            if r.data is None:
                # Finding #2 — _read_batch returns data=None not only for missing CAS
                # files but also for: DT_PIPE / DT_STREAM entries, backend read errors,
                # lock timeouts, route misses, and external connector paths.  A bare
                # data=None must not be treated as "file not found" for all of these.
                #
                # Delegate to the full single-file read() path, which correctly handles:
                #   • virtual resolver paths (resolve_read)
                #   • external connector mounts (ExternalRouteResult)
                #   • DT_PIPE / DT_STREAM entry types
                #   • standard per-file read hooks (DynamicViewerReadHook, etc.)
                #
                # Only NexusFileNotFoundError from read() is classified as "not found";
                # any other exception is a real failure and either propagates (strict
                # mode) or surfaces as a per-item "read_error" (partial mode).
                #
                # Resolver permission errors and parser failures are NOT caught here —
                # they propagate through read() just as they would via the single-file
                # endpoint.
                try:
                    content = self.read(path, context=context)
                    _loaded_bytes += len(content)
                    if _loaded_bytes > _MAX_BATCH_READ_BYTES:
                        raise ValueError(
                            f"Batch read aggregate size exceeded "
                            f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                        )
                    results.append(
                        {
                            "path": path,
                            "content": content,
                            "content_id": meta.content_id if meta else None,
                            "version": meta.version if meta else 0,
                            "modified_at": meta.modified_at if meta else None,
                            "size": len(content),
                        }
                    )
                    hit_items.append((path, meta))
                    continue
                except NexusFileNotFoundError:
                    pass  # Confirmed missing — fall through to not_found handling.
                except Exception:
                    # Real failure (backend error, permission denied, lock timeout…).
                    # In partial mode return a per-item error so the rest of the batch
                    # is not aborted.  In strict mode re-raise so the caller sees the
                    # actual failure.
                    if not partial:
                        raise
                    results.append({"path": path, "error": "read_error"})
                    continue

                if not partial:
                    raise NexusFileNotFoundError(path)
                results.append({"path": path, "error": "not_found"})
                continue

            content = bytes(r.data) if r.data else b""
            _loaded_bytes += len(content)
            if _loaded_bytes > _MAX_BATCH_READ_BYTES:
                raise ValueError(
                    f"Batch read aggregate size exceeded "
                    f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                )

            # Finding #1 — per-item "read" post-hook (mirrors read() at line ~1285).
            # Ensures content-transforming hooks such as DynamicViewerReadHook fire
            # for every successfully read item, preventing authorization bypass via
            # the batch endpoint.
            if _has_read_hooks:
                from nexus.contracts.vfs_hooks import ReadHookContext

                _read_ctx = ReadHookContext(
                    path=path,
                    context=context,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    content=content,
                    content_id=r.content_id,
                )
                self._kernel.dispatch_post_hooks("read", _read_ctx)
                content = _read_ctx.content or content

            # Use r.content_id as the primary content_id — it reflects the actual bytes
            # returned by this read, not the pre-read metadata snapshot (which can be
            # stale under concurrent writes).  Fall back to meta.content_id only when the
            # Rust result has no content_id (older backends / degenerate path).
            _etag = r.content_id or (meta.content_id if meta else None)
            results.append(
                {
                    "path": path,
                    "content": content,
                    "content_id": _etag,
                    "version": meta.version if meta else 0,
                    "modified_at": meta.modified_at if meta else None,
                    "size": len(content),
                }
            )
            hit_items.append((path, meta))

        # POST-INTERCEPT: batch post-hook (only if listeners registered).
        from nexus.contracts.vfs_hooks import ReadBatchHookContext

        self._dispatch_batch_post_hook(
            "read_batch",
            ReadBatchHookContext(
                items=hit_items, context=context, zone_id=zone_id, agent_id=agent_id
            ),
        )

        return results
