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
    resolve_io_profile,
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

    def open(self, path: str, flags: int) -> int:
        """Open a file and return a file descriptor."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)

        # A1-B: Validate namespace visibility at open time
        check_namespace_visible(ctx, original_path)

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
            if not ctx.nexus_fs.sys_access(original_path):
                raise FuseOSError(errno.ENOENT)

        # Generate file descriptor (thread-safe)
        with ctx.files_lock:
            ctx.fd_counter += 1
            fd = ctx.fd_counter

            io_profile_str = resolve_io_profile(ctx, original_path)

            ctx.open_files[fd] = {
                "path": original_path,
                "view_type": view_type,
                "flags": flags,
                "auth_verified": ctx.context is not None,
                "io_profile": io_profile_str,
            }

        # Trigger prefetch-on-open for readahead
        if ctx.readahead and view_type is None and not content_cached:
            try:
                file_size = None
                if hasattr(ctx.nexus_fs, "stat"):
                    stat_result = ctx.nexus_fs.stat(original_path)
                    if stat_result:
                        file_size = stat_result.get("st_size")

                io_profile_arg = None
                try:
                    from nexus.contracts.io_profile import IOProfile

                    io_profile_arg = IOProfile(io_profile_str)
                except (ImportError, ValueError):
                    logger.debug("[FUSE-OPEN] IOProfile unavailable or invalid: %s", io_profile_str)

                ctx.readahead.on_open(fd, original_path, file_size, io_profile=io_profile_arg)
            except Exception as e:
                logger.debug(f"[FUSE-OPEN] Readahead on_open failed (non-critical): {e}")
        elif content_cached:
            logger.debug(f"[FUSE-OPEN] Skipping readahead (L1 cached): {original_path}")

        return fd

    def read(self, _path: str, size: int, offset: int, fh: int) -> bytes:
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
            ok, content = try_rust(ctx, "READ", "read", original_path)
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

        skip_auth = file_info.get("auth_verified", False)

        cache_priority = 0
        io_profile_str = file_info.get("io_profile", "balanced")
        try:
            from nexus.contracts.io_profile import IOProfile

            cache_priority = IOProfile(io_profile_str).config().cache_priority
        except (ImportError, ValueError):
            logger.debug("[FUSE-READ] IOProfile unavailable or invalid: %s", io_profile_str)

        content = get_file_content(
            ctx, original_path, view_type, skip_auth=skip_auth, cache_priority=cache_priority
        )

        return content[offset : offset + size]

    def write(self, path: str, data: bytes, offset: int, fh: int) -> int:
        """Write file content.

        TODO(Issue #13B): Current implementation uses read-modify-write pattern,
        which is not optimal for large files. A write-buffering layer that batches
        writes and flushes on fsync/release would improve performance significantly.
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

        write_ctx = None if file_info.get("auth_verified") else ctx.context

        # Read existing content
        existing_content = b""
        if ctx.nexus_fs.sys_access(original_path):
            raw_content = ctx.nexus_fs.sys_read(original_path, context=write_ctx)
            assert isinstance(raw_content, bytes), "Expected bytes from read()"
            existing_content = raw_content

        # Handle offset writes
        if offset > len(existing_content):
            existing_content += b"\x00" * (offset - len(existing_content))

        new_content = existing_content[:offset] + data + existing_content[offset + len(data) :]

        # Write via Rust or Python
        if not write_ctx:
            ok, _ = try_rust(ctx, "WRITE", "write", original_path, new_content)
            if not ok:
                ctx.nexus_fs.sys_write(original_path, new_content, context=write_ctx)
        else:
            ctx.nexus_fs.sys_write(original_path, new_content, context=write_ctx)

        # Invalidate caches
        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)

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
