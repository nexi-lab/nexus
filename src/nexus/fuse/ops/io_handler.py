"""File I/O operations: open, read, write, release."""

import errno
import logging
from typing import cast

from fuse import FuseOSError

from nexus.fuse.filters import is_os_metadata_file
from nexus.fuse.ops._shared import (
    FUSESharedContext,
    check_namespace_visible,
    get_file_content,
    parse_virtual_path_for_fuse,
    try_rust,
)

# Import event types
try:
    from nexus.core.file_events import FileEventType

    HAS_EVENT_BUS = True
except ImportError:
    HAS_EVENT_BUS = False
    FileEventType = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class IOHandler:
    """Handles open, read, write, release operations."""

    def __init__(self, ctx: FUSESharedContext) -> None:
        self._ctx = ctx

    async def open(self, path: str, flags: int) -> int:
        """Open a file and return a file descriptor."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)

        # A1-B: Validate namespace visibility at open time
        await check_namespace_visible(ctx, original_path)

        # Check if file exists - use cache first
        content_cached = ctx.cache.get_content(original_path) is not None
        attr_cached = ctx.cache.get_attr(original_path) is not None
        file_exists = content_cached or attr_cached

        if file_exists:
            logger.debug(
                f"[FUSE-OPEN] Cache HIT for {original_path} "
                f"(content={content_cached}, attr={attr_cached})"
            )
        else:
            logger.debug(f"[FUSE-OPEN] Cache MISS for {original_path}, checking remote")
            if not ctx.nexus_fs.access(original_path):
                raise FuseOSError(errno.ENOENT)

        # Generate file descriptor (thread-safe)
        with ctx.files_lock:
            ctx.fd_counter += 1
            fd = ctx.fd_counter

            ctx.open_files[fd] = {
                "path": original_path,
                "view_type": view_type,
                "flags": flags,
                "auth_verified": ctx.context is not None,
            }

        # Trigger prefetch-on-open for readahead
        if ctx.readahead and view_type is None and not content_cached:
            try:
                file_size = None
                if hasattr(ctx.nexus_fs, "stat"):
                    stat_result = ctx.nexus_fs.stat(original_path)
                    if stat_result:
                        file_size = stat_result.get("st_size")

                ctx.readahead.on_open(fd, original_path, file_size)
            except Exception as e:
                logger.debug(f"[FUSE-OPEN] Readahead on_open failed (non-critical): {e}")
        elif content_cached:
            logger.debug(f"[FUSE-OPEN] Skipping readahead (L1 cached): {original_path}")

        return fd

    async def read(self, _path: str, size: int, offset: int, fh: int) -> bytes:
        """Read file content."""
        ctx = self._ctx

        with ctx.files_lock:
            file_info = ctx.open_files.get(fh)
        if not file_info:
            raise FuseOSError(errno.EBADF)

        original_path = file_info["path"]
        view_type = file_info["view_type"]

        # Rust delegation for raw reads
        if view_type is None:
            ok, content = try_rust(ctx, "READ", "sys_read", original_path)
            if ok:
                return bytes(content)[offset : offset + size]

        # Readahead buffer check
        if ctx.readahead and view_type is None:
            prefetched = ctx.readahead.on_read(fh, original_path, offset, size)
            if prefetched is not None:
                logger.debug(
                    f"[FUSE-READ] READAHEAD HIT: {original_path}[{offset}:{offset + size}]"
                )
                return cast("bytes", prefetched)

        content = await get_file_content(ctx, original_path, view_type, cache_priority=0)

        return content[offset : offset + size]

    async def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """Write file content.

        Uses per-path locking to serialize concurrent writes and prevent
        data loss from interleaved read-modify-write cycles (H13).
        """
        ctx = self._ctx

        with ctx.files_lock:
            file_info = ctx.open_files.get(fh)
        if not file_info:
            raise FuseOSError(errno.EBADF)

        if file_info["view_type"]:
            raise FuseOSError(errno.EROFS)

        original_path = file_info["path"]

        basename = original_path.split("/")[-1]
        if is_os_metadata_file(basename):
            logger.debug(f"Blocked write to OS metadata file: {original_path}")
            raise FuseOSError(errno.EPERM)

        # Per-path lock serializes concurrent writes to the same file (H13)
        import threading
        from typing import Any

        _ctx_any: Any = ctx  # Dynamic attrs not on FUSESharedContext
        if not hasattr(_ctx_any, "_write_locks"):
            _ctx_any._write_locks = {}
            _ctx_any._write_locks_guard = threading.Lock()
        with _ctx_any._write_locks_guard:
            lock = _ctx_any._write_locks.setdefault(original_path, threading.Lock())

        with lock:
            # Read existing content
            existing_content = b""
            if ctx.nexus_fs.access(original_path):
                raw_content = ctx.nexus_fs.sys_read(original_path, context=ctx.context)
                assert isinstance(raw_content, bytes), "Expected bytes from read()"
                existing_content = raw_content

            # Handle offset writes
            if offset > len(existing_content):
                existing_content += b"\x00" * (offset - len(existing_content))

            new_content = existing_content[:offset] + data + existing_content[offset + len(data) :]

            # Write via Rust or Python
            ok, _ = try_rust(ctx, "WRITE", "sys_write", original_path, new_content)
            if not ok:
                ctx.nexus_fs.write(original_path, new_content, context=ctx.context)

        # Invalidate caches + fire-and-forget lease revocation (Issue #3397)
        invalidation_paths = [original_path]
        if path != original_path:
            invalidation_paths.append(path)
        ctx.cache.invalidate_and_revoke(invalidation_paths)

        if ctx.readahead:
            ctx.readahead.invalidate_path(original_path)

        # Fire write event
        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.FILE_WRITE, original_path, size=len(new_content))

        return len(data)

    def release(self, _path: str, fh: int) -> None:
        """Release (close) a file."""
        ctx = self._ctx

        if ctx.readahead:
            ctx.readahead.on_release(fh)

        with ctx.files_lock:
            ctx.open_files.pop(fh, None)
