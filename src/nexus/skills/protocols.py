"""Narrow filesystem protocol for skills module.

This defines only the filesystem methods that the skills module actually uses,
rather than mirroring the full NexusFilesystem ABC (1,000+ LOC).

Any object implementing these 7 methods can serve as a filesystem for skills:
read, write, list, exists, mkdir, delete, is_directory.

Verification:
- Run: pytest tests/unit/skills/test_protocol_compatibility.py
- Contract test verifies NexusFilesystem ABC satisfies this protocol
"""

from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class NexusFilesystem(Protocol):
    """Narrow filesystem protocol for the skills module.

    Contains only the methods used by skills and MCP code:
    - read: Read file content
    - write: Write file content
    - list: List files in a directory
    - exists: Check if a path exists
    - mkdir: Create a directory
    - delete: Delete a file
    - is_directory: Check if path is a directory
    """

    def read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content.

        Args:
            path: Virtual path to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dict with content and metadata

        Returns:
            File content as bytes (default) or dict with metadata
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
        """Write content to a file.

        Args:
            path: Virtual path to write
            content: File content as bytes
            context: Optional operation context for permission checks
            if_match: Etag for optimistic concurrency
            if_none_match: Fail if file already exists
            force: Skip version check

        Returns:
            Dict with metadata (etag, version, modified_at, size)
        """
        ...

    def list(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> list[str] | list[dict[str, Any]]:
        """List files in a directory.

        Args:
            path: Directory path to list
            recursive: If True, list all files recursively
            details: If True, return detailed metadata
            show_parsed: If True, include virtual parsed views
            context: Optional operation context

        Returns:
            List of file paths or metadata dicts
        """
        ...

    def exists(self, path: str) -> bool:
        """Check if a file or directory exists.

        Args:
            path: Virtual path to check

        Returns:
            True if path exists
        """
        ...

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed
            exist_ok: Don't raise if directory exists
        """
        ...

    def delete(self, path: str) -> None:
        """Delete a file.

        Args:
            path: Virtual path to delete
        """
        ...

    def is_directory(self, path: str, context: Any = None) -> bool:
        """Check if path is a directory.

        Args:
            path: Virtual path to check
            context: Optional operation context

        Returns:
            True if path is a directory
        """
        ...
