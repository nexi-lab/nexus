"""Scoped filesystem wrapper for multi-zone path isolation.

This module provides a ScopedFilesystem wrapper that rebases all paths
to a user's root directory, enabling multi-zone isolation without
modifying existing code that uses hardcoded global paths.

Example:
    # For user at /zones/aquarius_team_12/users/user_12/
    scoped_fs = ScopedFilesystem(nexus_fs, root="/zones/aquarius_team_12/users/user_12")

    # SkillRegistry sees "/workspace/.nexus/skills/"
    # But actually reads from "/zones/aquarius_team_12/users/user_12/workspace/.nexus/skills/"
    registry = SkillRegistry(filesystem=scoped_fs)
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

from nexus.core.filesystem import NexusFilesystem


class ScopedFilesystem:
    """Filesystem wrapper that scopes all paths to a base directory.

    This enables multi-zone isolation by transparently rebasing paths.
    Code using hardcoded paths like "/workspace/.nexus/skills/" will
    actually access "/zones/team_X/users/user_Y/workspace/.nexus/skills/".

    The wrapper implements the NexusFilesystem protocol and delegates
    all operations to the underlying filesystem after path translation.

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
        self._fs = fs
        # Normalize root: remove trailing slash, ensure leading slash
        self._root = "/" + root.strip("/") if root.strip("/") else ""

    # Global namespaces that should not be scoped - these are shared resources
    # with their own ownership/permission structures
    GLOBAL_NAMESPACES = (
        "/skills/",  # Shared skills namespace
        "/system/",  # System-wide resources
        "/mnt/",  # Mount points (shared connectors)
        "/memory/",  # Memory router paths (/memory/by-user/, etc.)
        "/objs/",  # Object references (/objs/memory/, etc.)
    )

    def _scope_path(self, path: str) -> str:
        """Rebase a path to the scoped root.

        Args:
            path: Virtual path (e.g., "/workspace/file.txt")

        Returns:
            Scoped path (e.g., "/zones/team_12/users/user_1/workspace/file.txt")
        """
        if not path.startswith("/"):
            path = "/" + path

        # Global namespaces - don't scope, pass through as-is
        for ns in self.GLOBAL_NAMESPACES:
            if path.startswith(ns):
                return path

        return f"{self._root}{path}"

    def _unscope_path(self, path: str) -> str:
        """Remove the root prefix from a path.

        Args:
            path: Scoped path (e.g., "/zones/team_12/users/user_1/workspace/file.txt")

        Returns:
            Virtual path (e.g., "/workspace/file.txt")
        """
        # Global namespaces - don't unscope, return as-is
        for ns in self.GLOBAL_NAMESPACES:
            if path.startswith(ns):
                return path

        if self._root and path.startswith(self._root):
            result = path[len(self._root) :]
            return result if result else "/"
        return path

    def _unscope_paths(self, paths: builtins.list[str]) -> builtins.list[str]:
        """Remove the root prefix from a list of paths."""
        return [self._unscope_path(p) for p in paths]

    def _unscope_dict(self, d: dict[str, Any], path_keys: builtins.list[str]) -> dict[str, Any]:
        """Remove the root prefix from path values in a dict."""
        result = d.copy()
        for key in path_keys:
            if key in result and isinstance(result[key], str):
                result[key] = self._unscope_path(result[key])
        return result

    @property
    def root(self) -> str:
        """The root path prefix for this scoped filesystem."""
        return self._root

    @property
    def wrapped_fs(self) -> NexusFilesystem:
        """The underlying wrapped filesystem."""
        return self._fs

    # ============================================================
    # Properties
    # ============================================================

    @property
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        return self._fs.agent_id

    @property
    def zone_id(self) -> str | None:
        """Zone ID for this filesystem instance."""
        return self._fs.zone_id

    # ============================================================
    # Core File Operations
    # ============================================================

    def read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content as bytes."""
        result = self._fs.read(self._scope_path(path), context, return_metadata)
        if return_metadata and isinstance(result, dict):
            return self._unscope_dict(result, ["path"])
        return result

    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        lock: bool = False,
        lock_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Write content to a file.

        Args:
            lock: If True, acquire distributed lock before writing.
                Adds ~2-10ms latency for lock acquire/release.
                For read-modify-write patterns, use locked() context manager instead.
            lock_timeout: Max time to wait for lock in seconds (only used if lock=True).
        """
        kwargs: dict[str, Any] = {}
        if lock:
            kwargs["lock"] = lock
        if lock_timeout != 30.0:
            kwargs["lock_timeout"] = lock_timeout
        result = self._fs.write(
            self._scope_path(path),
            content,
            context,
            if_match,
            if_none_match,
            force,
            **kwargs,
        )
        return self._unscope_dict(result, ["path"])

    def write_batch(
        self, files: builtins.list[tuple[str, bytes]], context: Any = None
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction."""
        scoped_files = [(self._scope_path(path), content) for path, content in files]
        results = self._fs.write_batch(scoped_files, context)
        return [self._unscope_dict(r, ["path"]) for r in results]

    def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append content to an existing file."""
        result = self._fs.append(self._scope_path(path), content, context, if_match, force)
        return self._unscope_dict(result, ["path"])

    def delete(self, path: str) -> None:
        """Delete a file."""
        self._fs.delete(self._scope_path(path))

    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file."""
        self._fs.rename(self._scope_path(old_path), self._scope_path(new_path))

    def exists(self, path: str) -> bool:
        """Check if a file exists."""
        return self._fs.exists(self._scope_path(path))

    # ============================================================
    # File Discovery Operations
    # ============================================================

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        result = self._fs.list(self._scope_path(path), recursive, details, show_parsed, context)
        if details:
            return [
                self._unscope_dict(r, ["path", "virtual_path"])
                for r in cast(builtins.list[dict[str, Any]], result)
            ]
        return self._unscope_paths(cast(builtins.list[str], result))

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern."""
        result = self._fs.glob(pattern, self._scope_path(path), context)
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
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns."""
        result = self._fs.grep(
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
    # Directory Operations
    # ============================================================

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        self._fs.mkdir(self._scope_path(path), parents, exist_ok)

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory."""
        self._fs.rmdir(self._scope_path(path), recursive)

    def is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if path is a directory."""
        return self._fs.is_directory(self._scope_path(path), context)

    # ============================================================
    # Namespace Operations
    # ============================================================

    def get_available_namespaces(self) -> builtins.list[str]:
        """Get list of available namespace directories."""
        return self._fs.get_available_namespaces()

    # ============================================================
    # Sandbox Operations
    # ============================================================

    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Create a new code execution sandbox."""
        return self._fs.sandbox_create(name, ttl_minutes, provider, template_id, context)

    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Get existing active sandbox or create a new one."""
        return self._fs.sandbox_get_or_create(
            name, ttl_minutes, provider, template_id, verify_status, context
        )

    def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict[Any, Any]:
        """Run code in a sandbox."""
        return self._fs.sandbox_run(
            sandbox_id=sandbox_id,
            language=language,
            code=code,
            timeout=timeout,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            context=context,
            as_script=as_script,
        )

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Pause a running sandbox."""
        return self._fs.sandbox_pause(sandbox_id, context)

    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Resume a paused sandbox."""
        return self._fs.sandbox_resume(sandbox_id, context)

    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Stop a sandbox."""
        return self._fs.sandbox_stop(sandbox_id, context)

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        """List all sandboxes for the current user."""
        return self._fs.sandbox_list(context, verify_status, user_id, zone_id, agent_id, status)

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Get sandbox status."""
        return self._fs.sandbox_status(sandbox_id, context)

    def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Connect to user-managed sandbox."""
        return self._fs.sandbox_connect(
            sandbox_id,
            provider,
            sandbox_api_key,
            mount_path,
            nexus_url,
            nexus_api_key,
            agent_id,
            context,
        )

    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Disconnect from user-managed sandbox."""
        return self._fs.sandbox_disconnect(sandbox_id, provider, sandbox_api_key, context)

    # ============================================================
    # Lifecycle Management
    # ============================================================

    def close(self) -> None:
        """Close the filesystem and release resources."""
        self._fs.close()

    def __enter__(self) -> ScopedFilesystem:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()
