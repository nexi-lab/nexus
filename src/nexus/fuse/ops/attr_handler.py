"""File attribute modification operations: chmod, chown, truncate, utimens."""

import errno
import logging

from fuse import FuseOSError

from nexus.fuse.ops._shared import (
    FUSESharedContext,
    check_namespace_visible,
    parse_virtual_path_for_fuse,
)

# Import event types
try:
    from nexus.core.file_events import FileEventType

    HAS_EVENT_BUS = True
except ImportError:
    HAS_EVENT_BUS = False
    FileEventType = None  # type: ignore[misc,assignment]

logger = logging.getLogger(__name__)


class AttrHandler:
    """Handles chmod, chown, truncate, utimens operations."""

    def __init__(self, ctx: FUSESharedContext) -> None:
        self._ctx = ctx

    async def chmod(self, path: str, mode: int) -> None:
        """Change file mode (permissions)."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)
        if view_type:
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, original_path)

        permission_bits = mode & 0o777
        ctx.nexus_fs.sys_setattr(original_path, context=ctx.context, mode=permission_bits)

        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.METADATA_CHANGE, original_path)

    async def chown(self, path: str, uid: int, gid: int) -> None:
        """Change file ownership."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)
        if view_type:
            raise FuseOSError(errno.EROFS)

        await check_namespace_visible(ctx, original_path)

        if not ctx.nexus_fs.access(original_path):
            raise FuseOSError(errno.ENOENT)

        attrs: dict[str, str] = {}

        if uid != -1:
            try:
                import pwd

                owner = pwd.getpwuid(uid).pw_name
            except (KeyError, ModuleNotFoundError):
                owner = str(uid)
            attrs["owner"] = owner

        if gid != -1:
            try:
                import grp

                group = grp.getgrgid(gid).gr_name
            except (KeyError, ModuleNotFoundError):
                group = str(gid)
            attrs["group"] = group

        if attrs:
            ctx.nexus_fs.sys_setattr(original_path, context=ctx.context, **attrs)

        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.METADATA_CHANGE, original_path)

    async def truncate(self, path: str, length: int, _fh: int | None = None) -> None:
        """Truncate file to specified length."""
        ctx = self._ctx

        original_path, view_type = parse_virtual_path_for_fuse(ctx, path)
        if view_type:
            raise FuseOSError(errno.EROFS)

        if ctx.nexus_fs.access(original_path):
            raw_content = ctx.nexus_fs.sys_read(original_path, context=ctx.context)
            assert isinstance(raw_content, bytes), "Expected bytes from read()"
            content = raw_content
        else:
            content = b""

        if length < len(content):
            content = content[:length]
        else:
            content += b"\x00" * (length - len(content))

        ctx.nexus_fs.write(original_path, content, context=ctx.context)

        ctx.cache.invalidate_path(original_path)
        if path != original_path:
            ctx.cache.invalidate_path(path)

        if HAS_EVENT_BUS and FileEventType is not None:
            ctx.events.fire(FileEventType.FILE_WRITE, original_path, size=length)

    def utimens(self, _path: str, _times: tuple[float, float] | None = None) -> None:
        """Update file access and modification times (no-op)."""
        pass
