"""Abstract base class for Nexus filesystem implementations.

This module defines the common interface that all Nexus filesystem modes
(Embedded, Monolith, Distributed) must implement.
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

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
    - v0.1.0: Initial interface with file ops, discovery ops, directory ops
    - v0.2.0: Will add permission operations (chmod, chown, chgrp)
    - v0.3.0: Monolith mode implementation
    - v0.4.0: Distributed mode implementation
    """

    # ============================================================
    # Core File Operations
    # ============================================================

    @abstractmethod
    def read(self, path: str) -> bytes:
        """
        Read file content as bytes.

        Args:
            path: Virtual path to read

        Returns:
            File content as bytes

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
        """
        ...

    @abstractmethod
    def write(self, path: str, content: bytes) -> None:
        """
        Write content to a file.

        Creates parent directories if needed. Overwrites existing files.

        Args:
            path: Virtual path to write
            content: File content as bytes

        Raises:
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
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
    # File Discovery Operations (v0.1.0)
    # ============================================================

    @abstractmethod
    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        prefix: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]]:
        """
        List files in a directory.

        Args:
            path: Directory path to list (default: "/")
            recursive: If True, list all files recursively; if False, list only direct children
            details: If True, return detailed metadata; if False, return paths only
            prefix: (Deprecated) Path prefix to filter by - for backward compatibility

        Returns:
            List of file paths (if details=False) or list of file metadata dicts (if details=True)

        Examples:
            # List all files recursively (default)
            fs.list()

            # List files in root directory only (non-recursive)
            fs.list("/", recursive=False)

            # List files with metadata
            fs.list(details=True)
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
    ) -> builtins.list[dict[str, Any]]:
        """
        Search file contents using regex patterns.

        Args:
            pattern: Regex pattern to search for in file contents
            path: Base path to search from (default: "/")
            file_pattern: Optional glob pattern to filter files (e.g., "*.py")
            ignore_case: If True, perform case-insensitive search (default: False)
            max_results: Maximum number of results to return (default: 1000)

        Returns:
            List of match dicts, each containing:
            - file: File path
            - line: Line number (1-indexed)
            - content: Matched line content
            - match: The matched text

        Examples:
            # Search for "TODO" in all files
            fs.grep("TODO")

            # Search for function definitions in Python files
            fs.grep(r"def \\w+", file_pattern="**/*.py")

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
    # Lifecycle Management
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
