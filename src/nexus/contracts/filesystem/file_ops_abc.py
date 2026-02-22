"""File operations sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: read, write, write_batch, append, edit, delete, rename, exists
"""

import builtins
from abc import ABC, abstractmethod
from typing import Any


class FileOpsABC(ABC):
    """Core file operations: read, write, write_batch, append, edit, delete, rename, exists."""

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
        lock: bool = False,
        lock_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Write content to a file with optional optimistic concurrency control.

        Creates parent directories if needed. Overwrites existing files.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control
            if_none_match: If True, create-only mode
            force: If True, skip version check
            lock: If True, acquire distributed lock before writing
            lock_timeout: Timeout in seconds for lock acquisition

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
        """Write multiple files in a single transaction for improved performance.

        All files are written atomically — either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks

        Returns:
            List of metadata dicts for each file (in same order as input)

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied
            PermissionError: If any path is read-only
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
        """Append content to an existing file or create a new file if it doesn't exist.

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
        """Apply surgical search/replace edits to a file.

        Args:
            path: Virtual path to edit
            edits: List of edit operations (tuples, dicts, or EditOperation instances)
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching
            preview: If True, return preview without writing

        Returns:
            Dict with success, diff, matches, applied_count, etag, version

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag
            EditError: If edits cannot be applied
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
            PermissionError: If path is read-only
        """
        ...

    @abstractmethod
    def rename(self, old_path: str, new_path: str) -> None:
        """Rename/move a file (metadata-only operation).

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
        """Check if a file exists.

        Args:
            path: Virtual path to check

        Returns:
            True if file exists, False otherwise
        """
        ...
