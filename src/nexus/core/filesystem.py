"""Abstract base class for Nexus filesystem implementations.

This module defines the common interface that all Nexus filesystem modes
(Embedded, Monolith, Distributed) must implement.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from datetime import timedelta

# Import List to avoid name conflict with list() method
from typing import Any


class NexusFilesystem(ABC):
    """
    Abstract base class for Nexus filesystem implementations.

    All filesystem modes (Embedded, Monolith, Distributed) must implement
    this interface to ensure consistent behavior across modes.

    This interface provides:
    - Core file operations (read, write, delete, exists)
    - File discovery operations (list, glob, grep)
    - Directory operations (mkdir, rmdir, is_directory)
    - Lifecycle management (close, context manager)

    Version History:
    Initial interface includes file operations, discovery operations, and directory operations.
    Permission operations use ReBAC (Relationship-Based Access Control).
    """

    # Instance attributes (set by implementations)
    agent_id: str | None
    tenant_id: str | None

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
    # File Discovery Operations
    # ============================================================

    @abstractmethod
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
        show_parsed: bool = True,
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

    @abstractmethod
    def glob(self, pattern: str, path: str = "/") -> builtins.list[str]:
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

    @abstractmethod
    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
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
    def is_directory(self, path: str) -> bool:
        """
        Check if path is a directory.

        Args:
            path: Virtual path to check

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

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
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
    # Lifecycle Management
    # ============================================================

    # === Workspace Versioning ===

    @abstractmethod
    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        agent_id: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot of a registered workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
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
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Restore workspace to a previous snapshot.

        Args:
            snapshot_number: Snapshot version number to restore
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead

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
        agent_id: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        """List snapshot history for workspace.

        Args:
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead
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
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """Compare two workspace snapshots.

        Args:
            snapshot_1: First snapshot number
            snapshot_2: Second snapshot number
            workspace_path: Path to registered workspace
            agent_id: DEPRECATED - Use workspace_path instead

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
    def list_workspaces(self) -> builtins.list[dict]:
        """List all registered workspaces.

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
