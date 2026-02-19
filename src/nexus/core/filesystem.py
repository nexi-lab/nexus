"""Abstract base class for Nexus filesystem implementations.

This module defines the common interface that all Nexus filesystem modes
(Standalone, Remote, Federation) must implement.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext


class NexusFilesystem(ABC):
    """
    Abstract base class for Nexus filesystem implementations.

    All filesystem modes (Standalone, Remote, Federation) must implement
    this interface to ensure consistent behavior across modes.

    This interface provides:
    - Core file operations (read, write, delete, exists)
    - Directory operations (mkdir, rmdir, is_directory)
    - Lifecycle management (close, context manager)

    Version History:
    Initial interface includes file operations, discovery operations, and directory operations.
    Permission operations use ReBAC (Relationship-Based Access Control).
    """

    # Instance attributes (set by implementations)
    # Note: These are implemented as read-only properties by subclasses
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
        """
        Read file content as bytes.

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
        """
        Write content to a file with optional optimistic concurrency control.

        Creates parent directories if needed. Overwrites existing files.

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
        """
        Write multiple files in a single transaction for improved performance.

        This is 4x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            List of metadata dicts for each file (in same order as input):
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If any path is read-only or user doesn't have write permission

        Examples:
            >>> # Write 100 small files in a single batch (4x faster!)
            >>> files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
            >>> results = nx.write_batch(files)
            >>> print(f"Wrote {len(results)} files")

            >>> # Atomic batch write - all or nothing
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
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
        """
        Append content to an existing file or create a new file if it doesn't exist.

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control
            force: If True, skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If append operation fails
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag
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
        """
        Apply surgical search/replace edits to a file.

        This enables precise file modifications without rewriting entire files,
        reducing token cost and errors when used with LLMs.

        Issue #800: Add edit engine with search/replace for surgical file edits.

        Args:
            path: Virtual path to edit
            edits: List of edit operations. Each edit can be:
                - Tuple: (old_str, new_str) - simple search/replace
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool} - full control
                - EditOperation: Direct EditOperation instance
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control.
                If provided, edit fails if file changed since read.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
                Default 0.85. Use 1.0 for exact matching only.
            preview: If True, return preview without writing. Default False.

        Returns:
            Dict containing:
                - success: bool - True if all edits applied
                - diff: str - Unified diff of changes
                - matches: list[dict] - Info about each match (type, line, similarity)
                - applied_count: int - Number of edits applied
                - etag: str - New etag (if not preview)
                - version: int - New version (if not preview)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag
            EditError: If edits cannot be applied (ambiguous match, not found)

        Examples:
            >>> # Simple search/replace
            >>> result = fs.edit("/code/main.py", [
            ...     ("def foo():", "def bar():"),
            ...     ("return x", "return x + 1"),
            ... ])
            >>> print(result['diff'])

            >>> # With optimistic concurrency
            >>> content = fs.read("/code/main.py", return_metadata=True)
            >>> result = fs.edit(
            ...     "/code/main.py",
            ...     [("old_text", "new_text")],
            ...     if_match=content['etag']
            ... )

            >>> # Preview without writing
            >>> result = fs.edit("/code/main.py", edits, preview=True)
            >>> if result['success']:
            ...     print(result['diff'])
        """
        ...

    @abstractmethod
    def delete(self, path: str) -> None:
        """
        Delete a file.

        Args:
            path: Virtual path to delete

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
        """
        ...

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> None:
        """
        Rename/move a file (metadata-only operation).

        This is a metadata-only operation that does NOT copy file content.
        Only the virtual path is updated in metadata.

        Args:
            old_path: Current virtual path
            new_path: New virtual path

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination already exists
            InvalidPathError: If either path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If either path is read-only
        """
        ...

    @abstractmethod
    def exists(self, path: str) -> bool:
        """
        Check if a file exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file exists, False otherwise
        """
        ...

    # ============================================================
    # Directory Operations
    # ============================================================

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """
        Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed (like mkdir -p)
            exist_ok: Don't raise error if directory exists

        Raises:
            FileExistsError: If directory exists and exist_ok=False
            FileNotFoundError: If parent doesn't exist and parents=False
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
        """
        ...

    @abstractmethod
    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory (like rm -rf)

        Raises:
            OSError: If directory not empty and recursive=False
            NexusFileNotFoundError: If directory doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
        """
        ...

    @abstractmethod
    def is_directory(self, path: str, context: OperationContext | None = None) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Virtual path to check
            context: Optional operation context for permission checking

        Returns:
            True if path is a directory, False otherwise
        """
        ...

    # ============================================================
    # Namespace Operations
    # ============================================================

    @abstractmethod
    def get_available_namespaces(self) -> builtins.list[str]:
        """
        Get list of available namespace directories.

        Returns the built-in namespaces that should appear at root level.
        Filters based on zone and admin context.

        Returns:
            List of namespace names (e.g., ['workspace', 'shared', 'external'])

        Examples:
            # Get available namespaces
            namespaces = fs.get_available_namespaces()
            # ['workspace', 'shared', 'external'] for regular users
            # ['workspace', 'shared', 'external', 'system'] for admins
        """
        ...

    # ============================================================
    # Lifecycle Management
    # ============================================================

    # === Workspace Versioning ===

    @abstractmethod
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace
            description: Human-readable description of snapshot
            tags: List of tags for categorization

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If workspace_path not provided
            BackendError: If snapshot cannot be created
        """
        ...

    @abstractmethod
    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace

        Returns:
            Restore operation result

        Raises:
            ValueError: If workspace_path not provided
            NexusFileNotFoundError: If snapshot not found
        """
        ...

    @abstractmethod
    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If workspace_path not provided
        """
        ...

    @abstractmethod
    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If workspace_path not provided
            NexusFileNotFoundError: If either snapshot not found
        """
        ...

    # === Workspace Registry ===

    @abstractmethod
    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,  # v0.5.0: If provided, workspace is session-scoped
        ttl: timedelta | None = None,  # v0.5.0
    ) -> dict[str, Any]:
        """Register a workspace path.

        Args:
            path: Path to register as workspace
            name: Optional workspace name
            description: Optional description
            created_by: User/agent who created the workspace
            tags: Optional tags
            metadata: Optional metadata
            session_id: If provided, workspace is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live for auto-expiry (v0.5.0)

        Returns:
            Workspace registration info
        """
        ...

    @abstractmethod
    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace path.

        Args:
            path: Workspace path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    @abstractmethod
    def list_workspaces(self, context: Any | None = None) -> builtins.list[dict]:
        """List all registered workspaces.

        Args:
            context: Optional operation context for filtering

        Returns:
            List of workspace info dicts
        """
        ...

    @abstractmethod
    def get_workspace_info(self, path: str) -> dict | None:
        """Get workspace information.

        Args:
            path: Workspace path

        Returns:
            Workspace info dict or None if not found
        """
        ...

    # === Memory Registry ===

    @abstractmethod
    def register_memory(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,  # v0.5.0: If provided, memory is session-scoped
        ttl: timedelta | None = None,  # v0.5.0
    ) -> dict[str, Any]:
        """Register a memory path.

        Args:
            path: Path to register as memory
            name: Optional memory name
            description: Optional description
            created_by: User/agent who created the memory
            tags: Optional tags
            metadata: Optional metadata

        Returns:
            Memory registration info
        """
        ...

    @abstractmethod
    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory path.

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    @abstractmethod
    def list_memories(self) -> builtins.list[dict]:
        """List all registered memories.

        Returns:
            List of memory info dicts
        """
        ...

    @abstractmethod
    def get_memory_info(self, path: str) -> dict | None:
        """Get memory information.

        Args:
            path: Memory path

        Returns:
            Memory info dict or None if not found
        """
        ...

    # === Sandbox Operations ===

    @property
    def sandbox_available(self) -> bool:
        """Whether sandbox execution is available.

        Returns True if at least one sandbox provider is configured.
        Subclasses should override this to check their sandbox manager.
        """
        return False

    @abstractmethod
    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Create a new code execution sandbox.

        Args:
            name: User-friendly sandbox name
            ttl_minutes: Idle timeout in minutes
            provider: Sandbox provider ("e2b", "docker", etc.)
            template_id: Provider template ID (optional)
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        ...

    @abstractmethod
    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Get existing active sandbox or create a new one.

        Args:
            name: Sandbox name (e.g., "user_id,agent_id")
            ttl_minutes: Idle timeout in minutes
            provider: Sandbox provider ("docker", "e2b", etc.)
            template_id: Provider template ID (optional)
            verify_status: Whether to verify the sandbox status
            context: Operation context

        Returns:
            Sandbox metadata dict
        """
        ...

    @abstractmethod
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
        """Run code in a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds
            nexus_url: Nexus server URL for credential injection
            nexus_api_key: Nexus API key for credential injection
            context: Operation context
            as_script: If True, run as standalone script (stateless).
                      If False (default), use Jupyter kernel for Python (stateful).

        Returns:
            Execution result dict
        """
        ...

    @abstractmethod
    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Pause a running sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    @abstractmethod
    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    @abstractmethod
    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Stop a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    @abstractmethod
    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        """List all sandboxes for the current user.

        Args:
            context: Operation context
            verify_status: Whether to verify sandbox status
            user_id: Filter by user ID
            zone_id: Filter by zone ID
            agent_id: Filter by agent ID
            status: Filter by status

        Returns:
            List of sandbox metadata dicts
        """
        ...

    @abstractmethod
    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Get sandbox status.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Sandbox status dict
        """
        ...

    @abstractmethod
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
        """Connect to user-managed sandbox (Issue #371).

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider
            sandbox_api_key: Provider API key
            mount_path: Mount path in sandbox
            nexus_url: Nexus server URL for mounting (auto-detected if not provided)
            nexus_api_key: Nexus API key for mounting (auto-detected if not provided)
            agent_id: Agent ID for version attribution (issue #418).
                When set, file modifications will be attributed to this agent.
            context: Operation context

        Returns:
            Connection result dict
        """
        ...

    @abstractmethod
    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        """Disconnect from user-managed sandbox (Issue #371).

        Args:
            sandbox_id: External sandbox ID
            provider: Sandbox provider
            sandbox_api_key: Provider API key
            context: Operation context

        Returns:
            Disconnection result dict
        """
        ...

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
