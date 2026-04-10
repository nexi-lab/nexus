"""Scoped filesystem wrapper for multi-zone path isolation.

This module provides a ScopedFilesystem wrapper that rebases all paths
to a user's root directory, enabling multi-zone isolation without
modifying existing code that uses hardcoded global paths.

Moved from core/ → services/filesystem/ → bricks/filesystem/ (Issue #2424).

Sync-only.  Async callers should use ``asyncio.to_thread()``::

    result = await asyncio.to_thread(scoped_fs.sys_read, "/workspace/file.txt")

Example:
    # For user at /zones/aquarius_team_12/users/user_12/
    scoped_fs = ScopedFilesystem(nexus_fs, root="/zones/aquarius_team_12/users/user_12")

    # SkillRegistry sees "/workspace/.nexus/skills/"
    # But actually reads from "/zones/aquarius_team_12/users/user_12/workspace/.nexus/skills/"
    registry = SkillRegistry(filesystem=scoped_fs)
"""

import builtins
from typing import Any, cast

from nexus.bricks.filesystem._scoped_base import ScopedPathMixin
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystem
from nexus.contracts.types import OperationContext


class ScopedFilesystem(ScopedPathMixin):
    """Filesystem wrapper that scopes all paths to a base directory.

    This enables multi-zone isolation by transparently rebasing paths.
    Code using hardcoded paths like "/workspace/.nexus/skills/" will
    actually access "/zones/team_X/users/user_Y/workspace/.nexus/skills/".

    The wrapper implements the NexusFilesystem protocol and delegates
    all operations to the underlying filesystem after path translation.

    Service-level methods (workspace, sandbox, mount, memory, agent)
    are forwarded directly via ``__getattr__`` — no path scoping.

    Attributes:
        _fs: The underlying NexusFilesystem instance
        _root: The root path prefix to prepend to all paths
    """

    def __init__(self, fs: NexusFilesystem, root: str) -> None:
        """Initialize ScopedFilesystem.

        Args:
            fs: The underlying filesystem to wrap
            root: Root path prefix (e.g., "/zones/team_12/users/user_1")
                  All paths will be rebased relative to this root.
        """
        super().__init__(root)
        self._fs = fs

    @property
    def wrapped_fs(self) -> NexusFilesystem:
        """The underlying wrapped filesystem."""
        return self._fs

    # ============================================================
    # Properties
    # ============================================================

    # ============================================================
    # Content I/O (path-scoped)
    # ============================================================

    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread)."""
        return self._fs.sys_read(
            self._scope_path(path), count=count, offset=offset, context=context
        )

    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Write content to a file (POSIX pwrite)."""
        return self._fs.sys_write(
            self._scope_path(path), buf, count=count, offset=offset, context=context
        )

    def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
        **kwargs: Any,
    ) -> bytes | dict[str, Any]:
        """Read with optional metadata (VFS convenience)."""
        result = self._fs.read(
            self._scope_path(path),
            count=count,
            offset=offset,
            context=context,
            return_metadata=return_metadata,
            **kwargs,
        )
        if return_metadata and isinstance(result, dict):
            return self._unscope_dict(result, ["path"])
        return result

    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Write with metadata update (VFS convenience)."""
        result = self._fs.write(
            self._scope_path(path), buf, count=count, offset=offset, context=context, **kwargs
        )
        return self._unscope_dict(result, ["path"])

    # ============================================================
    # Metadata I/O (path-scoped)
    # ============================================================

    def sys_stat(
        self, path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any] | None:
        """Read all file metadata."""
        result = self._fs.sys_stat(self._scope_path(path), context=context)
        if result is not None and isinstance(result, dict):
            return self._unscope_dict(result, ["path"])
        return result

    def sys_setattr(
        self, path: str, *, context: OperationContext | None = None, **attrs: Any
    ) -> dict[str, Any]:
        """Update file metadata attributes."""
        result = self._fs.sys_setattr(self._scope_path(path), context=context, **attrs)
        return self._unscope_dict(result, ["path"])

    # ============================================================
    # Namespace (path-scoped)
    # ============================================================

    def sys_unlink(self, path: str, *, context: OperationContext | None = None) -> dict[str, Any]:
        """Remove a directory entry."""
        return self._fs.sys_unlink(self._scope_path(path), context=context)

    def sys_rename(
        self, old_path: str, new_path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """Rename/move a file."""
        return self._fs.sys_rename(
            self._scope_path(old_path), self._scope_path(new_path), context=context
        )

    # ============================================================
    # Query (path-scoped)
    # ============================================================

    def access(self, path: str, *, context: OperationContext | None = None) -> bool:
        """Check if a file exists."""
        return self._fs.access(self._scope_path(path), context=context)

    # ============================================================
    # File Discovery Operations (path-scoped)
    # ============================================================

    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        result = self._fs.sys_readdir(
            self._scope_path(path), recursive, details, show_parsed, context=context
        )
        if details:
            return [
                self._unscope_dict(r, ["path", "virtual_path"])
                for r in cast(builtins.list[dict[str, Any]], result)
            ]
        return self._unscope_paths(cast(builtins.list[str], result))

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern."""
        search = self._fs.service("search")
        if search is None:
            raise NotImplementedError("SearchService not available")
        result = search.glob(pattern, self._scope_path(path), context)
        return self._unscope_paths(result)

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        search = self._fs.service("search")
        if search is None:
            raise NotImplementedError("SearchService not available")
        result = search.grep(
            pattern,
            self._scope_path(path),
            file_pattern,
            ignore_case,
            max_results,
            search_mode,
            context,
            before_context=before_context,
            after_context=after_context,
            invert_match=invert_match,
        )
        return [self._unscope_dict(r, ["file", "path"]) for r in result]

    # ============================================================
    # Directory Operations (path-scoped)
    # ============================================================

    def mkdir(
        self,
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory."""
        self._fs.mkdir(self._scope_path(path), parents, exist_ok, context=context)

    def rmdir(
        self, path: str, recursive: bool = True, *, context: OperationContext | None = None
    ) -> None:
        """Remove a directory."""
        self._fs.rmdir(self._scope_path(path), recursive=recursive, context=context)

    def is_directory(self, path: str, *, context: OperationContext | None = None) -> bool:
        """Check if path is a directory."""
        return self._fs.is_directory(self._scope_path(path), context=context)

    # ============================================================
    # Convenience methods (path-scoped)
    # ============================================================

    def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Append content to an existing file."""
        result = self._fs.append(self._scope_path(path), content, context=context)
        return self._unscope_dict(result, ["path"])

    def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        *,
        context: OperationContext | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file."""
        result = self._fs.edit(
            self._scope_path(path),
            edits,
            context=context,
            fuzzy_threshold=fuzzy_threshold,
            preview=preview,
            **kwargs,
        )
        return self._unscope_dict(result, ["path"])

    def write_batch(
        self,
        files: builtins.list[tuple[str, bytes]],
        *,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction."""
        scoped_files = [(self._scope_path(path), content) for path, content in files]
        results = self._fs.write_batch(scoped_files, context=context)
        return [self._unscope_dict(r, ["path"]) for r in results]

    def read_batch(
        self,
        paths: builtins.list[str],
        *,
        partial: bool = False,
        context: OperationContext | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """Read multiple files in a single round-trip.

        Raises AccessDeniedError if any path resolves to a global namespace
        (e.g. /memory/, /skills/) — cross-scope reads are not permitted in
        batch mode.  Use the underlying filesystem directly for global paths.
        """
        from nexus.bricks.filesystem._scoped_base import GLOBAL_NAMESPACES
        from nexus.contracts.exceptions import AccessDeniedError

        is_admin = getattr(context, "is_admin", False)

        scoped_paths = []
        for path in paths:
            scoped = self._scope_path(path)
            if not is_admin:
                for ns in GLOBAL_NAMESPACES:
                    if scoped.startswith(ns):
                        raise AccessDeniedError(
                            f"Cross-scope read denied: '{path}' resolves to global namespace '{ns}'"
                        )
            scoped_paths.append(scoped)

        results = self._fs.read_batch(scoped_paths, partial=partial, context=context)
        return [self._unscope_dict(r, ["path"]) for r in results]

    # ============================================================
    # Mount Operations
    # ============================================================

    def get_top_level_mounts(self, context: OperationContext | None = None) -> builtins.list[str]:
        """Get list of top-level mount names."""
        return self._fs.get_top_level_mounts(context=context)

    # ============================================================
    # Service method forwarding
    # ============================================================
    # Workspace, sandbox, mount, memory, agent, and all other
    # service-level methods are forwarded directly to _fs via
    # __getattr__.  No path scoping is needed for these — they
    # either don't take paths or handle scoping internally.

    def __getattr__(self, name: str) -> Any:
        """Forward unknown attributes to the underlying filesystem."""
        return getattr(self._fs, name)

    # ============================================================
    # Lifecycle Management
    # ============================================================

    def close(self) -> None:
        """Close the filesystem and release resources."""
        self._fs.close()

    def __enter__(self) -> "ScopedFilesystem":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
