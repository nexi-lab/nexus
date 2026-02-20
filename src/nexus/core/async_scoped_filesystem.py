"""Async scoped filesystem wrapper for multi-zone path isolation.

This module provides an AsyncScopedFilesystem wrapper that rebases all paths
to a user's root directory, enabling multi-zone isolation without
modifying existing code that uses hardcoded global paths.

Example:
    # For user at /zones/aquarius_team_12/users/user_12/
    scoped_fs = AsyncScopedFilesystem(async_nx, root="/zones/aquarius_team_12/users/user_12")

    # SkillRegistry sees "/workspace/.nexus/skills/"
    # But actually reads from "/zones/aquarius_team_12/users/user_12/workspace/.nexus/skills/"
    files = await scoped_fs.list("/workspace/.nexus/skills/")
"""

from __future__ import annotations

import builtins
from collections.abc import AsyncIterator
from typing import Any

from nexus.core._scoped_base import GLOBAL_NAMESPACES, ScopedPathMixin


class AsyncScopedFilesystem(ScopedPathMixin):
    """Async filesystem wrapper that scopes all paths to a base directory.

    This enables multi-zone isolation by transparently rebasing paths.
    Code using hardcoded paths like "/workspace/.nexus/skills/" will
    actually access "/zones/team_X/users/user_Y/workspace/.nexus/skills/".

    The wrapper delegates all operations to the underlying async filesystem
    after path translation.

    Attributes:
        _fs: The underlying async filesystem instance
        _root: The root path prefix to prepend to all paths
    """

    # Re-export for backward compat
    GLOBAL_NAMESPACES = GLOBAL_NAMESPACES

    def __init__(self, fs: Any, root: str) -> None:
        """Initialize AsyncScopedFilesystem.

        Args:
            fs: The underlying async filesystem to wrap
            root: Root path prefix (e.g., "/zones/team_12/users/user_1")
                  All paths will be rebased relative to this root.
        """
        super().__init__(root)
        self._fs = fs

    @property
    def wrapped_fs(self) -> Any:
        """The underlying wrapped filesystem."""
        return self._fs

    # ============================================================
    # Properties
    # ============================================================

    @property
    def zone_id(self) -> str | None:
        """Zone ID for this filesystem instance."""
        return self._fs.zone_id

    @zone_id.setter
    def zone_id(self, value: str | None) -> None:
        """Set zone ID."""
        self._fs.zone_id = value

    @property
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        return self._fs.agent_id

    @agent_id.setter
    def agent_id(self, value: str | None) -> None:
        """Set agent ID."""
        self._fs.agent_id = value

    # ============================================================
    # Core File Operations (Async)
    # ============================================================

    async def read(
        self,
        path: str,
        context: Any = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read file content as bytes (async)."""
        result = await self._fs.read(self._scope_path(path), context, return_metadata)
        if return_metadata and isinstance(result, dict):
            return self._unscope_dict(result, ["path"])
        return result

    async def read_bulk(
        self,
        paths: builtins.list[str],
        context: Any = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """Read multiple files in a single RPC call (async)."""
        scoped_paths = [self._scope_path(p) for p in paths]
        result = await self._fs.read_bulk(scoped_paths, context, return_metadata, skip_errors)
        # Unscope the keys in the result dict
        unscoped_result: dict[str, bytes | dict[str, Any] | None] = {}
        for scoped_path, content in result.items():
            unscoped_path = self._unscope_path(scoped_path)
            if return_metadata and isinstance(content, dict):
                unscoped_result[unscoped_path] = self._unscope_dict(content, ["path"])
            else:
                unscoped_result[unscoped_path] = content
        return unscoped_result

    async def write(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file (async)."""
        result = await self._fs.write(
            self._scope_path(path), content, context, if_match, if_none_match, force
        )
        return self._unscope_dict(result, ["path"])

    async def write_batch(
        self, files: builtins.list[tuple[str, bytes]], context: Any = None
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction (async)."""
        scoped_files = [(self._scope_path(path), content) for path, content in files]
        results = await self._fs.write_batch(scoped_files, context)
        return [self._unscope_dict(r, ["path"]) for r in results]

    async def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append content to an existing file (async)."""
        result = await self._fs.append(self._scope_path(path), content, context, if_match, force)
        return self._unscope_dict(result, ["path"])

    async def delete(
        self,
        path: str,
        context: Any = None,
    ) -> bool:
        """Delete a file (async)."""
        return await self._fs.delete(self._scope_path(path), context)

    async def rename(
        self,
        old_path: str,
        new_path: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Rename/move a file (async)."""
        result = await self._fs.rename(
            self._scope_path(old_path), self._scope_path(new_path), context
        )
        return self._unscope_dict(result, ["path", "old_path", "new_path"])

    async def exists(
        self,
        path: str,
        context: Any = None,
    ) -> bool:
        """Check if a file exists (async)."""
        return await self._fs.exists(self._scope_path(path), context)

    # ============================================================
    # File Discovery Operations (Async)
    # ============================================================

    async def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory (async)."""
        result = await self._fs.list(
            self._scope_path(path), recursive, details, show_parsed, context
        )
        if details:
            return [self._unscope_dict(r, ["path", "virtual_path"]) for r in result]
        return self._unscope_paths(result)

    async def glob(
        self,
        pattern: str,
        path: str = "/",
        context: Any = None,
    ) -> builtins.list[str]:
        """Find files matching a glob pattern (async)."""
        result = await self._fs.glob(pattern, self._scope_path(path), context)
        return self._unscope_paths(result)

    async def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns (async)."""
        result = await self._fs.grep(
            pattern,
            self._scope_path(path),
            file_pattern,
            ignore_case,
            max_results,
            search_mode,
            context,
        )
        return [self._unscope_dict(r, ["file", "path"]) for r in result]

    # ============================================================
    # Directory Operations (Async)
    # ============================================================

    async def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: Any = None,
    ) -> dict[str, Any]:
        """Create a directory (async)."""
        result = await self._fs.mkdir(self._scope_path(path), parents, exist_ok, context)
        return self._unscope_dict(result, ["path"])

    async def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: Any = None,
    ) -> None:
        """Remove a directory (async)."""
        await self._fs.rmdir(self._scope_path(path), recursive, context)

    async def is_directory(
        self,
        path: str,
        context: Any = None,
    ) -> bool:
        """Check if path is a directory (async)."""
        return await self._fs.is_directory(self._scope_path(path), context)

    # ============================================================
    # Mount Operations (Async)
    # ============================================================

    async def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        io_profile: str = "balanced",
        context: Any = None,
    ) -> str:
        """Add a dynamic backend mount to the filesystem (async)."""
        return await self._fs.add_mount(
            mount_point=self._scope_path(mount_point),
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            io_profile=io_profile,
            context=context,
        )

    async def remove_mount(
        self,
        mount_point: str,
        context: Any = None,
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem (async)."""
        result = await self._fs.remove_mount(self._scope_path(mount_point), context)
        return self._unscope_dict(result, ["mount_point"])

    async def list_mounts(
        self,
        context: Any = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all active backend mounts (async)."""
        result = await self._fs.list_mounts(context)
        return [self._unscope_dict(r, ["mount_point"]) for r in result]

    async def list_connectors(
        self,
        category: str | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List all available connector types (async)."""
        return await self._fs.list_connectors(category)

    async def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
        generate_embeddings: bool = False,
        context: Any = None,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s) (async)."""
        scoped_mount = self._scope_path(mount_point) if mount_point else None
        scoped_path = self._scope_path(path) if path else None
        return await self._fs.sync_mount(
            scoped_mount,
            scoped_path,
            recursive,
            dry_run,
            sync_content,
            include_patterns,
            exclude_patterns,
            generate_embeddings,
            context,
        )

    # ============================================================
    # Memory Registration (Async)
    # ============================================================

    async def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
    ) -> dict[str, Any]:
        """Register a directory as a memory (async)."""
        result = await self._fs.register_memory(
            self._scope_path(path), name, description, created_by, tags, metadata, session_id, ttl
        )
        return self._unscope_dict(result, ["path"])

    async def unregister_memory(self, path: str) -> bool:
        """Unregister a memory (async)."""
        return await self._fs.unregister_memory(self._scope_path(path))

    async def list_registered_memories(self) -> builtins.list[dict[str, Any]]:
        """List all registered memory paths (async)."""
        result = await self._fs.list_registered_memories()
        return [self._unscope_dict(r, ["path"]) for r in result]

    async def get_memory_info(self, path: str) -> dict[str, Any] | None:
        """Get information about a registered memory (async)."""
        result = await self._fs.get_memory_info(self._scope_path(path))
        if result:
            return self._unscope_dict(result, ["path"])
        return None

    # ============================================================
    # Metadata Operations (Async)
    # ============================================================

    async def get_metadata(
        self,
        path: str,
        context: Any = None,
    ) -> dict[str, Any] | None:
        """Get file metadata (async)."""
        result = await self._fs.get_metadata(self._scope_path(path), context)
        if result:
            return self._unscope_dict(result, ["path"])
        return None

    # ============================================================
    # Streaming Operations (Async)
    # ============================================================

    async def stream(
        self,
        path: str,
        chunk_size: int = 8192,
        context: Any = None,
    ) -> AsyncIterator[bytes]:
        """Stream file content in chunks (async generator)."""
        async for chunk in self._fs.stream(self._scope_path(path), chunk_size, context):
            yield chunk

    # ============================================================
    # Lifecycle Management (Async)
    # ============================================================

    async def close(self) -> None:
        """Close the filesystem and release resources."""
        await self._fs.close()

    async def __aenter__(self) -> AsyncScopedFilesystem:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit."""
        await self.close()
