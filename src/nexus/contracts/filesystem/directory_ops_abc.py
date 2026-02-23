"""Directory operations sub-ABC for filesystem implementations.

Extracted from core/filesystem.py (Issue #2424) following the
``collections.abc`` composition pattern.

Contains: mkdir, rmdir, is_directory, get_top_level_mounts
"""

import builtins
from abc import ABC, abstractmethod

from nexus.contracts.types import OperationContext


class DirectoryOpsABC(ABC):
    """Directory operations: mkdir, rmdir, is_directory, get_top_level_mounts."""

    @abstractmethod
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory.

        Args:
            path: Virtual path to directory
            parents: Create parent directories if needed
            exist_ok: Don't raise if directory exists

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
        """Remove a directory.

        Args:
            path: Virtual path to directory
            recursive: Remove non-empty directory

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
        """Check if path is a directory.

        Args:
            path: Virtual path to check
            context: Optional operation context

        Returns:
            True if path is a directory
        """
        ...

    @abstractmethod
    def get_top_level_mounts(self) -> builtins.list[str]:
        """Get list of top-level mount names.

        Returns:
            List of namespace names (e.g., ['workspace', 'shared', 'external'])
        """
        ...
