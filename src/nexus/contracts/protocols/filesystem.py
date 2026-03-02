"""Narrow filesystem protocol for MCP and other bricks.

This defines only the filesystem methods that bricks actually use,
rather than mirroring the full NexusFilesystemABC (1,000+ LOC).

Any object implementing these 7 sys_ methods can serve as a filesystem for bricks:
sys_read, sys_write, sys_readdir, sys_access, sys_mkdir, sys_unlink, sys_is_directory.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class NexusFilesystem(Protocol):
    """Narrow filesystem protocol for bricks (MCP, etc.).

    Contains only the methods used by MCP and other brick code,
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
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,
    ) -> bytes:
        """Read file content (POSIX pread(2)).

        Args:
            path: Virtual path to read
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Optional operation context for permission checks

        Returns:
            File content as bytes.
        """
        ...

    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: Any = None,
    ) -> int:
        """Write content to a file (POSIX pwrite(2)).

        Args:
            path: Virtual path to write
            buf: File content as bytes or str
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset to start writing at.
            context: Optional operation context for permission checks

        Returns:
            Number of bytes written.
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

    def sys_unlink(self, path: str) -> Any:
        """Delete a file (POSIX unlink).

        Args:
            path: Virtual path to delete

        Returns:
            Implementation-defined result (may be None or metadata dict).
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
