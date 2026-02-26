"""Narrow filesystem protocol for skills module.

This defines only the filesystem methods that the skills module actually uses,
rather than mirroring the full NexusFilesystemABC (1,000+ LOC).

Any object implementing these 7 sys_ methods can serve as a filesystem for skills:
sys_read, sys_write, sys_readdir, sys_access, sys_mkdir, sys_unlink, sys_is_directory.

Verification:
- Run: pytest tests/unit/skills/test_protocol_compatibility.py
- Contract test verifies NexusFilesystemABC satisfies this protocol
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class NexusFilesystem(Protocol):
    """Narrow filesystem protocol for the skills module.

    Contains only the methods used by skills and MCP code,
    using the sys_ prefix convention matching NexusFilesystemABC:
    - sys_read: Read file content
    - sys_write: Write file content
    - sys_readdir: List files in a directory
    - sys_access: Check if a path exists
    - sys_mkdir: Create a directory
    - sys_unlink: Delete a file
    - sys_is_directory: Check if path is a directory
    """

    def sys_read(
        self, path: str, context: Any = None, return_metadata: bool = False
    ) -> bytes | dict[str, Any]:
        """Read file content (POSIX read).

        Args:
            path: Virtual path to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dict with content and metadata

        Returns:
            File content as bytes (default) or dict with metadata
        """
        ...

    def sys_write(
        self,
        path: str,
        content: bytes,
        context: Any = None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write content to a file (POSIX write).

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

    def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
    ) -> list[str] | list[dict[str, Any]]:
        """List files in a directory (POSIX readdir).

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

    def sys_access(self, path: str) -> bool:
        """Check if a file or directory exists (POSIX access).

        Args:
            path: Virtual path to check

        Returns:
            True if path exists
        """
        ...

    def sys_mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory (POSIX mkdir).

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed
            exist_ok: Don't raise if directory exists
        """
        ...

    def sys_unlink(self, path: str) -> None:
        """Delete a file (POSIX unlink).

        Args:
            path: Virtual path to delete
        """
        ...

    def sys_is_directory(self, path: str, context: Any = None) -> bool:
        """Check if path is a directory.

        Args:
            path: Virtual path to check
            context: Optional operation context

        Returns:
            True if path is a directory
        """
        ...
