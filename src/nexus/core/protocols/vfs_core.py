"""VFS core file operations protocol (Issue #1287, Decision 3A).

Defines the contract for core file system operations that the kernel
exposes. Implementation: ``NexusFS`` (sync, methods merged from dissolved mixin).

This protocol is a *roadmap* — it captures the target interface for future
extraction of VFS ops into focused services. No implementation
is required yet (Phase F in the architecture doc).

References:
    - docs/design/NEXUS-LEGO-ARCHITECTURE.md §3 (Kernel tier)
    - Issue #1287: Extract NexusFS Domain Services from God Object
"""

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

@runtime_checkable
class VFSCoreProtocol(Protocol):
    """Core file operations — read, write, delete, stat, exists, mkdir.

    These are the fundamental VFS operations that every storage backend
    must support. The kernel wraps them with permission checks, caching,
    virtual views, and event emission.
    """

    def read(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,
    ) -> bytes | str:
        """Read file content at path.

        Args:
            path: Virtual path to read.
            context: Operation context for permission checks.

        Returns:
            File content as bytes or string.
        """
        ...

    def write(
        self,
        path: str,
        content: bytes | str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Write content to file at path.

        Args:
            path: Virtual path to write.
            content: File content.
            context: Operation context for permission checks.

        Returns:
            Operation result dict with path, size, etc.
        """
        ...

    def delete(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Delete file or directory at path.

        Args:
            path: Virtual path to delete.
            context: Operation context for permission checks.

        Returns:
            Operation result dict.
        """
        ...

    def mkdir(
        self,
        path: str,
        *,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory at path.

        Args:
            path: Virtual path for directory.
            parents: If True, create parent directories as needed.
            exist_ok: If True, don't raise if directory exists.
            context: Operation context for permission checks.
        """
        ...

    def stat(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Get file/directory metadata.

        Args:
            path: Virtual path to stat.
            context: Operation context for permission checks.

        Returns:
            Metadata dict with size, modified, type, etc.
        """
        ...

    def exists(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if path exists.

        Args:
            path: Virtual path to check.
            context: Operation context for permission checks.

        Returns:
            True if path exists.
        """
        ...

    def rename(
        self,
        source: str,
        destination: str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Rename/move a file or directory.

        Args:
            source: Current virtual path.
            destination: New virtual path.
            context: Operation context for permission checks.

        Returns:
            Operation result dict.
        """
        ...
