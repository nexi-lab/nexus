"""File/directory mutation operations: create, unlink, mkdir, rmdir, rename."""

import errno
import logging
import os
from typing import Any

from fuse import FuseOSError

from nexus.fuse.filters import is_os_metadata_file
from nexus.fuse.ops._shared import (
    FUSESharedContext,
    check_namespace_visible,
    invalidate_dir_cache,
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


class MutationHandler:
    """Handles create, unlink, mkdir, rmdir, rename operations."""

    def __init__(self, ctx: FUSESharedContext) -> None:
        self._ctx = ctx

    async def create(self, path: str, _mode: int, _fi: Any = None) -> int:
        """Create a new file."""
        ctx = self._ctx

        # Block OS metadata files
        basename = path.split("/")[-1]
        if is_os_metadata_file(basename):
            logger.debug(f"Blocked creation of OS metadata file: {path}")
            raise FuseOSError(errno.EPERM)

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)
        if view_type:
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, original_path)

        await ctx.nexus_fs.write(original_path, b"", context=ctx.context)

        # Invalidate caches
        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)
        invalidate_dir_cache(ctx, original_path)

        # Generate file descriptor
        with ctx.files_lock:
            ctx.fd_counter += 1
            fd = ctx.fd_counter

            ctx.open_files[fd] = {
                "path": original_path,
                "view_type": None,
                "flags": os.O_RDWR,
                "auth_verified": ctx.context is not None,
            }

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.FILE_WRITE, original_path, size=0)

        return fd

    async def unlink(self, path: str) -> None:
        """Delete a file."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)
        if view_type:
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, original_path)

        ok, _ = try_rust(ctx, "UNLINK", "sys_unlink", original_path)
        if not ok:
            await ctx.nexus_fs.sys_unlink(original_path, context=ctx.context)

        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)
        invalidate_dir_cache(ctx, original_path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.FILE_DELETE, original_path)

    async def mkdir(self, path: str, _mode: int) -> None:
        """Create a directory."""
        ctx = self._ctx

        if path.startswith("/.raw/"):
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, path)

        ok, _ = try_rust(ctx, "MKDIR", "sys_mkdir", path)
        if not ok:
            await ctx.nexus_fs.sys_mkdir(path, parents=True, exist_ok=True, context=ctx.context)

        invalidate_dir_cache(ctx, path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.DIR_CREATE, path)

    async def rmdir(self, path: str) -> None:
        """Remove a directory."""
        ctx = self._ctx

        if path == "/.raw":
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, path)

        await ctx.nexus_fs.sys_rmdir(path, recursive=False, context=ctx.context)

        invalidate_dir_cache(ctx, path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.DIR_DELETE, path)

    async def rename(self, old: str, new: str) -> None:
        """Rename/move a file or directory."""
        ctx = self._ctx

        old_path, old_view = parse_virtual_path_for_fuse(ctx, old)
        new_path, new_view = parse_virtual_path_for_fuse(ctx, new)

        if old_view or new_view:
            raise FuseOSError(errno.EROFS)

        if old.startswith("/.raw/") or new.startswith("/.raw/"):
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, old_path)
        await check_namespace_visible(ctx, new_path)

        if await ctx.nexus_fs.sys_access(new_path):
            logger.error(f"Destination {new_path} already exists")
            raise FuseOSError(errno.EEXIST)

        if await ctx.nexus_fs.sys_is_directory(old_path, context=ctx.context):
            await self._rename_directory(old_path, new_path)
        else:
            await self._rename_file(old_path, new_path)

        # Invalidate caches for both paths
        ctx.cache.invalidate_path(old_path)
        ctx.cache.invalidate_path(new_path)
        invalidate_dir_cache(ctx, old_path)
        invalidate_dir_cache(ctx, new_path)

        old_parent = old_path.rsplit("/", 1)[0] or "/"
        new_parent = new_path.rsplit("/", 1)[0] or "/"
        ctx.cache.invalidate_path(old_parent)
        if old_parent != new_parent:
            ctx.cache.invalidate_path(new_parent)
            new_grandparent = new_parent.rsplit("/", 1)[0] or "/"
            if new_grandparent != new_parent:
                ctx.cache.invalidate_path(new_grandparent)
        if old != old_path:
            ctx.cache.invalidate_path(old)
        if new != new_path:
            ctx.cache.invalidate_path(new)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.FILE_RENAME, new_path, old_path=old_path)

    async def _rename_file(self, old_path: str, new_path: str) -> None:
        """Metadata-only file rename."""
        ctx = self._ctx
        logger.debug(f"Renaming file {old_path} to {new_path}")

        ok, _ = try_rust(ctx, "RENAME", "sys_rename", old_path, new_path)
        if not ok:
            await ctx.nexus_fs.sys_rename(old_path, new_path, context=ctx.context)

    async def _rename_directory(self, old_path: str, new_path: str) -> None:
        """Recursive directory rename: list + move files + rmdir source."""
        ctx = self._ctx
        logger.debug(f"Renaming directory {old_path} to {new_path}")

        try:
            await ctx.nexus_fs.sys_mkdir(new_path, parents=True, exist_ok=True, context=ctx.context)
        except Exception as e:
            logger.debug(f"mkdir {new_path} failed (may already exist): {e}")

        files = await ctx.nexus_fs.sys_readdir(
            old_path, recursive=True, details=True, context=ctx.context
        )

        for file_info in files:
            if not isinstance(file_info, dict):
                continue
            if not file_info.get("is_directory", False):
                src_file = file_info["path"]
                dest_file = src_file.replace(old_path, new_path, 1)
                logger.debug(f"  Moving file {src_file} to {dest_file}")
                await ctx.nexus_fs.sys_rename(src_file, dest_file, context=ctx.context)

        logger.debug(f"Removing source directory {old_path}")
        await ctx.nexus_fs.sys_rmdir(old_path, recursive=True, context=ctx.context)
