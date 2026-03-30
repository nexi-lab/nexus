"""Thin NexusFS facade for the slim package.

Exposes ~10 public methods from the kernel NexusFS. Internal methods
(sandbox, workflows, bulk operations, dispatch hooks) are hidden.

The facade also provides optimized implementations where the full kernel
path is unnecessarily heavy for slim-package use (e.g., single-lookup stat).

Usage:
    from nexus.fs._facade import SlimNexusFS

    facade = SlimNexusFS(kernel_fs)
    content = await facade.read("/s3/bucket/file.txt")
"""

from __future__ import annotations

import logging
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

    async def read(self, path: str) -> bytes:
        """Read file content.

        Args:
            path: Virtual file path (e.g., "/s3/my-bucket/file.txt")

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If file does not exist.
        """
        return await self._kernel.sys_read(path, context=self._ctx)

    async def read_range(self, path: str, start: int, end: int) -> bytes:
        """Read a specific byte range from a file.

        Memory-efficient — only fetches the requested range from the backend.

        Args:
            path: Virtual file path.
            start: Start byte offset (inclusive).
            end: End byte offset (exclusive).

        Returns:
            Bytes in the requested range.
        """
        return await self._kernel.read_range(path, start, end, context=self._ctx)

    # -- Write operations --

    async def write(self, path: str, content: bytes) -> dict[str, Any]:
        """Write content to a file (creates or overwrites).

        Args:
            path: Virtual file path.
            content: File content as bytes.

        Returns:
            Dict with path, size, etag, version.
        """
        return await self._kernel.write(path, content, context=self._ctx)

    # -- Directory operations --

    async def ls(
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
        return await self._kernel.sys_readdir(
            path,
            recursive=recursive,
            details=detail,
            context=self._ctx,
        )

    async def mkdir(self, path: str, parents: bool = True) -> None:
        """Create a directory.

        Args:
            path: Directory path to create.
            parents: If True, create parent directories as needed (mkdir -p).
        """
        await self._kernel.mkdir(
            path,
            parents=parents,
            exist_ok=True,
            context=self._ctx,
        )

    async def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory.

        Args:
            path: Directory path to remove.
            recursive: If True, remove contents recursively (rm -rf).
        """
        await self._kernel.rmdir(path, recursive=recursive, context=self._ctx)

    # -- File operations --

    async def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual file path to delete.

        Raises:
            NexusFileNotFoundError: If file does not exist.
        """
        await self._kernel.sys_unlink(path, context=self._ctx)

    async def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file.

        Args:
            old_path: Current file path.
            new_path: New file path.
        """
        await self._kernel.sys_rename(old_path, new_path, context=self._ctx)

    async def exists(self, path: str) -> bool:
        """Check if a path exists.

        Args:
            path: Virtual file path.

        Returns:
            True if the path exists (file or directory).
        """
        return await self._kernel.access(path, context=self._ctx)

    async def copy(self, src: str, dst: str) -> dict[str, Any]:
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
        return await self._kernel.sys_copy(src, dst, context=self._ctx)

    # -- Metadata (optimized single-lookup) --

    async def stat(self, path: str) -> dict[str, Any] | None:
        """Get file/directory metadata with a single metadata lookup.

        Optimized for the slim package — avoids the kernel's double-lookup
        pattern (is_directory + metadata.get) by doing one read and
        deriving directory status from the result.

        Args:
            path: Virtual file path.

        Returns:
            Metadata dict, or None if path does not exist.
        """
        from nexus.lib.path_utils import validate_path

        normalized = validate_path(path, allow_root=True)

        # Single metadata lookup
        meta: FileMetadata | None = self._kernel.metadata.get(normalized)

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

    # -- Mount management (delegated to kernel router) --

    def list_mounts(self) -> list[str]:
        """List all mount points.

        Returns:
            Sorted list of mount point paths.
        """
        return sorted(m.mount_point for m in self._kernel.router.list_mounts())

    # -- Lifecycle --

    async def close(self) -> None:
        """Close the filesystem and release resources.

        Closes the kernel (if it exposes a close method) and then
        closes the metastore's SQLite connection.  Safe to call
        multiple times — subsequent calls are no-ops.
        """
        if self._closed:
            return

        import contextlib

        try:
            # Close the kernel (may be sync or async)
            _close = getattr(self._kernel, "close", None)
            if _close is not None:
                result = _close()
                if result is not None:
                    await result
        finally:
            # Always close the metastore — even if kernel close raises —
            # to release the SQLite/WAL lock.
            with contextlib.suppress(Exception):
                self._kernel.metadata.close()
            self._closed = True

    async def __aenter__(self) -> SlimNexusFS:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
