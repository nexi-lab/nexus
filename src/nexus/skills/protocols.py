"""Shared protocols for skills module.

⚠️ CRITICAL: This Protocol MUST match nexus.core.filesystem.NexusFilesystem ABC exactly.

When updating:
1. Update core.filesystem.NexusFilesystem ABC first
2. Update this Protocol to match
3. Run: pytest tests/unit/skills/test_protocol_compatibility.py
4. Run: mypy src/nexus

See test_protocol_compatibility.py for automated verification.
"""

from __future__ import annotations

import builtins
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext


@runtime_checkable
class NexusFilesystem(Protocol):
    """Protocol matching core.filesystem.NexusFilesystem ABC.

    ⚠️ MUST stay in sync with nexus.core.filesystem.NexusFilesystem

    Why Protocol instead of ABC import?
    - Allows skills module to work with ANY filesystem-like object
    - Enables dependency injection and testing with mocks
    - Avoids circular dependencies
    - Follows "program to interfaces, not implementations"

    Verification:
    - Run: pytest tests/unit/skills/test_protocol_compatibility.py
    - CI/CD enforces mypy type checking on every commit

    This protocol defines the complete NexusFilesystem interface:
    - Core file operations (read, write, delete, exists)
    - File discovery operations (list, glob, grep)
    - Directory operations (mkdir, rmdir, is_directory)
    - Lifecycle management (close, context manager)
    """

    # Instance attributes (implemented as properties by concrete classes)
    @property
    def agent_id(self) -> str | None:
        """Agent ID for this filesystem instance."""
        ...

    @property
    def tenant_id(self) -> str | None:
        """Tenant ID for this filesystem instance."""
        ...

    # ============================================================
    # Core File Operations
    # ============================================================

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
            AccessDeniedError: If access is denied (tenant isolation or read-only namespace)
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
        """
        List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively; if False, list only direct children
            details: If True, return detailed metadata; if False, return paths only
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility
            show_parsed: If True, include virtual _parsed.{ext}.md views; if False, exclude them (default: True)

        Returns:
            List of file paths (if details=False) or list of file metadata dicts (if details=True)

        Examples:
            # List all files recursively (default)
            fs.list()

            # List files in root directory only (non-recursive)
            fs.list("/", recursive=False)

            # List files with metadata
            fs.list(details=True)

            # List files without virtual parsed views
            fs.list(show_parsed=False)
        """
        ...

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """
        Find files matching a glob pattern.

        Supports standard glob patterns:
        - `*` matches any sequence of characters (except `/`)
        - `**` matches any sequence of characters including `/` (recursive)
        - `?` matches any single character
        - `[...]` matches any character in the brackets

        Args:
            pattern: Glob pattern to match (e.g., "**/*.py", "data/*.csv", "test_*.py")
            path: Base path to search from (default: "/")

        Returns:
            List of matching file paths, sorted by name

        Examples:
            # Find all Python files recursively
            fs.glob("**/*.py")

            # Find all CSV files in data directory
            fs.glob("*.csv", "/data")

            # Find all test files
            fs.glob("test_*.py")
        """
        ...

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
        """
        Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)
            search_mode: Content search mode (default: "auto")
                - "auto": Try parsed text first, fallback to raw
                - "parsed": Only search parsed text
                - "raw": Only search raw file content

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text
            - source: Source type - "parsed" or "raw"

        Examples:
            # Search for "TODO" in all files
            fs.grep("TODO")

            # Search for function definitions in Python files
            fs.grep(r"def \\w+", file_pattern="**/*.py")

            # Search only parsed PDFs
            fs.grep("revenue", file_pattern="**/*.pdf", search_mode="parsed")

            # Case-insensitive search
            fs.grep("error", ignore_case=True)
        """
        ...

    # ============================================================
    # Directory Operations
    # ============================================================

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

    def get_available_namespaces(self) -> builtins.list[str]:
        """
        Get list of available namespace directories.

        Returns the built-in namespaces that should appear at root level.
        Filters based on tenant and admin context.

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
    # Version Tracking Operations
    # ============================================================

    def get_version(self, path: str, version: int) -> bytes:
        """Get a specific version of a file.

        Retrieves the content for a specific version from CAS using the
        version's content hash.

        Args:
            path: Virtual file path
            version: Version number to retrieve

        Returns:
            File content as bytes for the specified version

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
        """
        ...

    def list_versions(self, path: str) -> builtins.list[dict[str, Any]]:
        """List all versions of a file.

        Returns version history with metadata for each version.

        Args:
            path: Virtual file path

        Returns:
            List of version info dicts ordered by version number (newest first)

        Raises:
            InvalidPathError: If path is invalid
        """
        ...

    def rollback(self, path: str, version: int, context: Any = None) -> None:
        """Rollback file to a previous version.

        Updates the file to point to an older version's content from CAS.
        Creates a new version entry marking this as a rollback.

        Args:
            path: Virtual file path
            version: Version number to rollback to
            context: Optional operation context for permission checks

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            PermissionError: If user doesn't have write permission
        """
        ...

    def diff_versions(
        self, path: str, v1: int, v2: int, mode: str = "metadata"
    ) -> dict[str, Any] | str:
        """Compare two versions of a file.

        Args:
            path: Virtual file path
            v1: First version number
            v2: Second version number
            mode: Diff mode - "metadata" (default) or "content"

        Returns:
            For "metadata" mode: Dict with metadata differences
            For "content" mode: Unified diff string

        Raises:
            NexusFileNotFoundError: If file or version doesn't exist
            InvalidPathError: If path is invalid
            ValueError: If mode is invalid
        """
        ...

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
        """Create a snapshot of the current agent's workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)
            description: Human-readable description of snapshot
            tags: List of tags for categorization

        Returns:
            Snapshot metadata dict

        Raises:
            ValueError: If agent_id not provided and no default set
            BackendError: If snapshot cannot be created
        """
        ...

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)

        Returns:
            Restore operation result

        Raises:
            ValueError: If agent_id not provided and no default set
            NexusFileNotFoundError: If snapshot not found
        """
        ...

    def workspace_log(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)
            limit: Maximum number of snapshots to return

        Returns:
            List of snapshot metadata dicts (most recent first)

        Raises:
            ValueError: If agent_id not provided and no default set
        """
        ...

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace
            agent_id: Agent identifier (uses default if not provided)

        Returns:
            Diff dict with added, removed, modified files

        Raises:
            ValueError: If agent_id not provided and no default set
            NexusFileNotFoundError: If either snapshot not found
        """
        ...

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
        session_id: str | None = None,  # v0.5.0: If provided, workspace is session-scoped
        ttl: timedelta | None = None,  # v0.5.0
    ) -> dict[str, Any]:
        """Register a workspace path.

        Args:
            path: Path to register as workspace
            name: Optional workspace name
            description: Optional description
            created_by: User/agent who created the workspace
            tags: Optional list of tags
            metadata: Optional custom metadata
            session_id: If provided, workspace is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live for auto-expiry (v0.5.0)

        Returns:
            Workspace info dict
        """
        ...

    def unregister_workspace(self, path: str) -> bool:
        """Unregister a workspace path.

        Args:
            path: Workspace path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    def list_workspaces(self) -> builtins.list[dict]:
        """List all registered workspaces.

        Returns:
            List of workspace info dicts
        """
        ...

    def get_workspace_info(self, path: str) -> dict | None:
        """Get workspace information.

        Args:
            path: Workspace path

        Returns:
            Workspace info dict or None if not found
        """
        ...

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
            session_id: If provided, memory is session-scoped (temporary). If None, persistent. (v0.5.0)
            ttl: Time-to-live for auto-expiry (v0.5.0)
            tags: Optional list of tags
            metadata: Optional custom metadata

        Returns:
            Memory info dict
        """
        ...

    def unregister_memory(self, path: str) -> bool:
        """Unregister a memory path.

        Args:
            path: Memory path to unregister

        Returns:
            True if unregistered, False if not found
        """
        ...

    def list_memories(self) -> builtins.list[dict]:
        """List all registered memories.

        Returns:
            List of memory info dicts
        """
        ...

    def get_memory_info(self, path: str) -> dict | None:
        """Get memory information.

        Args:
            path: Memory path

        Returns:
            Memory info dict or None if not found
        """
        ...

    # ============================================================
    # Content Operations
    # ============================================================

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

    # ============================================================
    # Sandbox Operations (Issue #372)
    # ============================================================

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
        """Run code in a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            language: Programming language
            code: Code to execute
            timeout: Execution timeout in seconds
            nexus_url: Nexus server URL for credential injection
            nexus_api_key: Nexus API key for credential injection
            context: Operation context

        Returns:
            Execution result dict
        """
        ...

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Pause a running sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Resume a paused sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Stop a sandbox.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Operation result dict
        """
        ...

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        """List all sandboxes for the current user.

        Args:
            context: Operation context
            verify_status: Whether to verify sandbox status
            user_id: Filter by user ID
            tenant_id: Filter by tenant ID
            agent_id: Filter by agent ID
            status: Filter by status

        Returns:
            List of sandbox metadata dicts
        """
        ...

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        """Get sandbox status.

        Args:
            sandbox_id: Sandbox identifier
            context: Operation context

        Returns:
            Sandbox status dict
        """
        ...

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
        """Add a dynamic backend mount to the filesystem.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type (e.g., "local", "gcs", "gcs_connector")
            backend_config: Backend-specific configuration dict
            priority: Mount priority (higher values take precedence)
            readonly: Whether mount is read-only

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            ValueError: If mount_point already exists or configuration is invalid
            RuntimeError: If backend type is not supported
        """
        ...

    def remove_mount(self, mount_point: str) -> dict[str, Any]:
        """Remove a backend mount from the filesystem.

        Args:
            mount_point: Virtual path of mount to remove

        Returns:
            Dictionary with removal details:
            - removed: bool - Whether mount was removed
            - directory_deleted: bool - Whether mount point directory was deleted
            - permissions_cleaned: int - Number of permission tuples removed
            - errors: list[str] - Any errors encountered
        """
        ...

    def list_mounts(self) -> builtins.list[dict[str, Any]]:
        """List all active backend mounts.

        Returns:
            List of mount info dictionaries with mount_point, priority, readonly, backend_type
        """
        ...

    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount

        Returns:
            Mount info dict if found, None otherwise
        """
        ...

    # ============================================================
    # Lifecycle Management
    # ============================================================

    def close(self) -> None:
        """Close the filesystem and release resources."""
        ...

    def __enter__(self) -> NexusFilesystem:
        """Context manager entry."""
        ...

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        ...
