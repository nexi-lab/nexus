"""Abstract base class for Nexus filesystem implementations.

This module defines the common interface that all Nexus filesystem modes
(Standalone, Remote, Federation) must implement.

Architecture (Issue #2033 — Strangler Fig decomposition):
  NexusFilesystem    — core VFS ops only (~15 methods)
  SandboxCapable     — sandbox_* methods (Protocol)
  WorkspaceCapable   — workspace/memory registry + snapshots (Protocol)
  MountCapable       — add_mount, remove_mount, list_mounts (Protocol)
  VersionCapable     — get_version, list_versions, rollback, diff (Protocol)
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


# ============================================================
# Core VFS ABC — the ONLY thing the kernel must implement
# ============================================================


class NexusFilesystem(ABC):
    """Abstract base class for Nexus filesystem implementations.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface to ensure consistent behavior across modes.

    This interface provides ONLY core VFS operations:
    - File CRUD (read, write, delete, rename, exists)
    - File discovery (list, glob, grep)
    - Directory ops (mkdir, rmdir, is_directory)
    - Lifecycle (close, context manager)

    Domain-specific operations (sandbox, workspace, mount, versioning)
    are defined in separate Protocol interfaces below.
    """

    @property
    @abstractmethod
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        ...

    @property
    @abstractmethod
    def zone_id(self) -> str | None:
        """Zone ID for this filesystem instance."""
        ...

    # ============================================================
    # Core File Operations
    # ============================================================

    @abstractmethod
    def read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content as bytes.

        Args:
            path: Virtual path to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dict with content and metadata

        Returns:
            If return_metadata=False: File content as bytes
            If return_metadata=True: Dict with content, etag, version, etc.

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
        """
        ...

    @abstractmethod
    def write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file with optional optimistic concurrency control.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control
            if_none_match: If True, create-only mode
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)

        Raises:
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag
        """
        ...

    @abstractmethod
    def write_batch(
        self, files: builtins.list[tuple[str, bytes]], context: Any = None
    ) -> builtins.list[dict[str, Any]]:
        """Write multiple files in a single transaction.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context

        Returns:
            List of metadata dicts for each file (etag, version, modified_at, size)
        """
        ...

    @abstractmethod
    def append(
        self,
        path: str,
        content: bytes | str,
        context: Any = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Append content to an existing file or create if not exists.

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str
            context: Optional operation context
            if_match: Optional etag for optimistic concurrency control
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)
        """
        ...

    @abstractmethod
    def edit(
        self,
        path: str,
        edits: builtins.list[tuple[str, str]] | builtins.list[dict[str, Any]] | builtins.list[Any],
        context: Any = None,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """Apply surgical search/replace edits to a file.

        Args:
            path: Virtual path to edit
            edits: List of edit operations (tuples, dicts, or EditOperation)
            context: Optional operation context
            if_match: Optional etag for optimistic concurrency control
            fuzzy_threshold: Similarity threshold for fuzzy matching (0.0-1.0)
            preview: If True, return preview without writing

        Returns:
            Dict with success, diff, matches, applied_count, etag, version
        """
        ...

    @abstractmethod
    def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual path to delete

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
        """
        ...

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file (metadata-only for CAS backends).

        Args:
            old_path: Current virtual path
            new_path: New virtual path

        Raises:
            NexusFileNotFoundError: If source doesn't exist
            FileExistsError: If destination exists
        """
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """Check if a file exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file exists, False otherwise
        """
        ...

    # ============================================================
    # File Discovery Operations
    # ============================================================

    @abstractmethod
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively
            details: If True, return detailed metadata
            show_parsed: If True, include virtual parsed views
            context: Optional operation context
        """
        ...

    @abstractmethod
    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern.

        Args:
            pattern: Glob pattern (e.g., "**/*.py", "data/*.csv")
            path: Base path to search from (default: "/")
            context: Optional operation context
        """
        ...

    @abstractmethod
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
        """Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for
            path: Base path to search from (default: "/")
            file_pattern: Optional glob to filter files
            ignore_case: Case-insensitive search
            max_results: Maximum results to return
            search_mode: "auto", "parsed", or "raw"
            context: Optional operation context
        """
        ...

    # ============================================================
    # Directory Operations
    # ============================================================

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        ...

    @abstractmethod
    def rmdir(self, path: str, recursive: bool = False) -> None:
        """Remove a directory."""
        ...

    @abstractmethod
    def is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """Check if path is a directory."""
        ...

    # ============================================================
    # Namespace Operations
    # ============================================================

    @abstractmethod
    def get_available_namespaces(self) -> builtins.list[str]:
        """Get list of available namespace directories."""
        ...

    # ============================================================
    # Lifecycle Management
    # ============================================================

    # === Workspace Versioning ===

    # === Workspace Registry ===

    # === Memory Registry ===

    # === Sandbox Operations ===

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox execution is available.

        Returns True if at least one sandbox provider is configured.
        Subclasses should override this to check their sandbox manager.
        """
        return False

    # NOTE: sandbox_validate() removed from kernel ABC — it's a service-level
    # linting/validation pipeline, not a kernel primitive. The implementation
    # remains on NexusFS via @rpc_expose for RPC dispatch.

    # ============================================================
    # Mount Management Operations
    # ============================================================

    @abstractmethod
    def close(self) -> None:
        """Close the filesystem and release resources."""
        ...

    def __enter__(self) -> NexusFilesystem:
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()


# ============================================================
# Role Protocol Interfaces — separate capabilities from VFS core
# ============================================================


@runtime_checkable
class VersionCapable(Protocol):
    """Protocol for filesystems that support file versioning."""

    def get_version(self, path: str, version: int) -> bytes: ...
    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]: ...
    def rollback(self, path: str, version: int, context: Any = None) -> None: ...
    def diff_versions(
        self, path: str, v1: int, v2: int, mode: str = "metadata"
    ) -> dict[str, Any] | str: ...


@runtime_checkable
class WorkspaceCapable(Protocol):
    """Protocol for filesystems that support workspace management."""

    # Workspace snapshots
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]: ...

    def workspace_restore(
        self, snapshot_number: int, workspace_path: str | None = None
    ) -> dict[str, Any]: ...

    def workspace_log(
        self, workspace_path: str | None = None, limit: int = 100
    ) -> builtins.list[dict[str, Any]]: ...

    def workspace_diff(
        self, snapshot_1: int, snapshot_2: int, workspace_path: str | None = None
    ) -> dict[str, Any]: ...

    # Workspace registry
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
    ) -> dict[str, Any]: ...

    def unregister_workspace(self, path: str) -> bool: ...
    def list_workspaces(self, context: Any | None = None) -> builtins.list[dict]: ...
    def get_workspace_info(self, path: str) -> dict | None: ...

    # Memory registry
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
    ) -> dict[str, Any]: ...

    def unregister_memory(self, path: str) -> bool: ...
    def list_memories(self) -> builtins.list[dict]: ...
    def get_memory_info(self, path: str) -> dict | None: ...


@runtime_checkable
class SandboxCapable(Protocol):
    """Protocol for filesystems that support code sandbox execution."""

    @property
    def sandbox_available(self) -> bool: ...

    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]: ...

    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]: ...

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
    ) -> dict[Any, Any]: ...

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]: ...
    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]: ...
    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]: ...

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]: ...

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]: ...

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
    ) -> dict[Any, Any]: ...

    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]: ...


@runtime_checkable
class MountCapable(Protocol):
    """Protocol for filesystems that support dynamic backend mounts."""

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        io_profile: str = "balanced",
    ) -> str: ...

    def remove_mount(self, mount_point: str) -> dict[str, Any]: ...
    def list_mounts(self) -> builtins.list[dict[str, Any]]: ...
    def get_mount(self, mount_point: str) -> dict[str, Any] | None: ...
