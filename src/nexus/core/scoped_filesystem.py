"""Scoped filesystem wrapper for multi-tenant path isolation.

This module provides a ScopedFilesystem wrapper that rebases all paths
to a user's root directory, enabling multi-tenant isolation without
modifying existing code that uses hardcoded global paths.

Example:
    # For user at /tenants/aquarius_team_12/users/user_12/
    scoped_fs = ScopedFilesystem(nexus_fs, root="/tenants/aquarius_team_12/users/user_12")

    # SkillRegistry sees "/workspace/.nexus/skills/"
    # But actually reads from "/tenants/aquarius_team_12/users/user_12/workspace/.nexus/skills/"
    registry = SkillRegistry(filesystem=scoped_fs)
"""

from __future__ import annotations

import builtins
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext

from nexus.skills.protocols import NexusFilesystem


class ScopedFilesystem:
    """Filesystem wrapper that scopes all paths to a base directory.

    This enables multi-tenant isolation by transparently rebasing paths.
    Code using hardcoded paths like "/workspace/.nexus/skills/" will
    actually access "/tenants/team_X/users/user_Y/workspace/.nexus/skills/".

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
            root: Root path prefix (e.g., "/tenants/team_12/users/user_1")
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
            Scoped path (e.g., "/tenants/team_12/users/user_1/workspace/file.txt")
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
            path: Scoped path (e.g., "/tenants/team_12/users/user_1/workspace/file.txt")

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
    def tenant_id(self) -> str | None:
        """Tenant ID for this filesystem instance."""
        return self._fs.tenant_id

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
    ) -> dict[str, Any]:
        """Write content to a file."""
        result = self._fs.write(
            self._scope_path(path), content, context, if_match, if_none_match, force
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
        prefix: str | None = None,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory."""
        scoped_prefix = self._scope_path(prefix) if prefix else None
        result = self._fs.list(
            self._scope_path(path), recursive, details, scoped_prefix, show_parsed, context
        )
        if details:
            return [self._unscope_dict(r, ["path", "virtual_path"]) for r in result]  # type: ignore
        return self._unscope_paths(result)  # type: ignore

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
    # Version Tracking Operations
    # ============================================================

    def get_version(self, path: str, version: int) -> bytes:
        """Get a specific version of a file."""
        return self._fs.get_version(self._scope_path(path), version)

    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file."""
        result = self._fs.list_versions(self._scope_path(path))
        return [self._unscope_dict(r, ["path"]) for r in result]

    def rollback(self, path: str, version: int, context: Any = None) -> None:
        """Rollback file to a previous version."""
        self._fs.rollback(self._scope_path(path), version, context)

    def diff_versions(
        self, path: str, v1: int, v2: int, mode: str = "metadata"
    ) -> dict[str, Any] | str:
        """Compare two versions of a file."""
        return self._fs.diff_versions(self._scope_path(path), v1, v2, mode)

    # ============================================================
    # Workspace Versioning
    # ============================================================

    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace."""
        scoped_path = self._scope_path(workspace_path) if workspace_path else None
        result = self._fs.workspace_snapshot(scoped_path, agent_id, description, tags)
        return self._unscope_dict(result, ["workspace_path", "path"])

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot."""
        scoped_path = self._scope_path(workspace_path) if workspace_path else None
        result = self._fs.workspace_restore(snapshot_number, scoped_path, agent_id)
        return self._unscope_dict(result, ["workspace_path", "path"])

    def workspace_log(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace."""
        scoped_path = self._scope_path(workspace_path) if workspace_path else None
        result = self._fs.workspace_log(scoped_path, agent_id, limit)
        return [self._unscope_dict(r, ["workspace_path", "path"]) for r in result]

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots."""
        scoped_path = self._scope_path(workspace_path) if workspace_path else None
        return self._fs.workspace_diff(snapshot_1, snapshot_2, scoped_path, agent_id)

    # ============================================================
    # Workspace & Memory Registry
    # ============================================================

    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> dict[str, Any]:
        """Register a workspace path."""
        result = self._fs.register_workspace(
            self._scope_path(path), name, description, created_by, tags, metadata, session_id, ttl
        )
        return self._unscope_dict(result, ["path"])

    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace path."""
        return self._fs.unregister_workspace(self._scope_path(path))

    def list_workspaces(self) -> builtins.list[dict]:
        """List all registered workspaces."""
        result = self._fs.list_workspaces()
        return [self._unscope_dict(r, ["path"]) for r in result]

    def get_workspace_info(self, path: str) -> dict | None:
        """Get workspace information."""
        result = self._fs.get_workspace_info(self._scope_path(path))
        if result:
            return self._unscope_dict(result, ["path"])
        return None

    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: timedelta | None = None,
    ) -> dict[str, Any]:
        """Register a memory path."""
        result = self._fs.register_memory(
            self._scope_path(path), name, description, created_by, tags, metadata, session_id, ttl
        )
        return self._unscope_dict(result, ["path"])

    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory path."""
        return self._fs.unregister_memory(self._scope_path(path))

    def list_memories(self) -> builtins.list[dict]:
        """List all registered memories."""
        result = self._fs.list_memories()
        return [self._unscope_dict(r, ["path"]) for r in result]

    def get_memory_info(self, path: str) -> dict | None:
        """Get memory information."""
        result = self._fs.get_memory_info(self._scope_path(path))
        if result:
            return self._unscope_dict(result, ["path"])
        return None

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
    ) -> dict[Any, Any]:
        """Run code in a sandbox."""
        return self._fs.sandbox_run(
            sandbox_id, language, code, timeout, nexus_url, nexus_api_key, context
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
        tenant_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        """List all sandboxes for the current user."""
        return self._fs.sandbox_list(context, verify_status, user_id, tenant_id, agent_id, status)

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
    # Mount Operations
    # ============================================================

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
    ) -> str:
        """Add a dynamic backend mount to the filesystem."""
        return self._fs.add_mount(
            self._scope_path(mount_point), backend_type, backend_config, priority, readonly
        )

    def remove_mount(self, mount_point: str) -> dict[str, Any]:
        """Remove a backend mount from the filesystem."""
        result = self._fs.remove_mount(self._scope_path(mount_point))
        return self._unscope_dict(result, ["mount_point"])

    def list_mounts(self) -> builtins.list[dict[str, Any]]:
        """List all active backend mounts."""
        result = self._fs.list_mounts()
        return [self._unscope_dict(r, ["mount_point"]) for r in result]

    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount."""
        result = self._fs.get_mount(self._scope_path(mount_point))
        if result:
            return self._unscope_dict(result, ["mount_point"])
        return None

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
