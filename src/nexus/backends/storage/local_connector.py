"""Local filesystem connector - reference mode without data duplication.

This module provides LocalConnectorBackend, a connector that mounts an external local
folder into Nexus's virtual filesystem. Unlike CASLocalBackend (which uses CAS),
LocalConnectorBackend keeps files in their original location (SSOT - Single Source of Truth).

Key features:
- Zero data duplication (reference mode)
- Full indexing support (semantic search via connector sync loop)
- Change detection via kernel OBSERVE (KernelDispatch)
- Direct read/write to original files

Example:
    >>> nx.add_mount(
    ...     mount_point="/mnt/local",
    ...     backend_type="local_connector",
    ...     backend_config={"local_path": "C:/projects"},
    ... )
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from nexus.backends.base.backend import Backend
from nexus.backends.base.registry import (
    ArgType,
    ConnectionArg,
    register_connector,
)
from nexus.contracts.backend_features import BackendFeature
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.hash_fast import hash_content
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.core.context import OperationContext

logger = logging.getLogger(__name__)


@register_connector(
    "local_connector",
    description="Mount local folder into Nexus (reference mode, no copy)",
    category="storage",
)
class LocalConnectorBackend(Backend):
    """Local filesystem connector - reference mode without data duplication.

    Mounts an external local folder into Nexus VFS. Files remain in their
    original location (SSOT) - no content duplication to CAS.

    This is different from:
    - CASLocalBackend: Uses CAS for deduplication (copies content)

    LocalConnectorBackend is similar to GDriveConnector but for local filesystem:
    - Both use backend_path (not content_hash) for path-based access
    - Both support full indexing via connector sync loop

    Storage structure:
        mount_point: /mnt/local-projects
        local_path:  C:\\Users\\user\\projects

        /mnt/local-projects/nexus/README.md
            → C:\\Users\\user\\projects\\nexus\\README.md

    Example:
        >>> backend = LocalConnectorBackend("C:/Users/user/projects")
        >>> content = backend.read_content("", context)  # Uses context.backend_path
    """

    _BACKEND_FEATURES = frozenset(
        {
            BackendFeature.DIRECTORY_LISTING,
        }
    )

    CONNECTION_ARGS: dict[str, ConnectionArg] = {
        "local_path": ConnectionArg(
            type=ArgType.PATH,
            description="Local folder path to mount",
            required=True,
        ),
        "readonly": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Mount as read-only",
            required=False,
            default=False,
        ),
        "follow_symlinks": ConnectionArg(
            type=ArgType.BOOLEAN,
            description="Follow symbolic links (default: True)",
            required=False,
            default=True,
        ),
    }

    def __init__(
        self,
        local_path: str | Path,
        readonly: bool = False,
        follow_symlinks: bool = True,
    ) -> None:
        """Initialize LocalConnectorBackend.

        Args:
            local_path: Local folder path to mount
            readonly: If True, write and delete operations will be rejected
            follow_symlinks: If True, follow symbolic links (default: True)

        Raises:
            BackendError: If local_path doesn't exist or is not a directory
        """
        super().__init__()
        self.local_path = Path(local_path).resolve()
        self.readonly = readonly
        self.follow_symlinks = follow_symlinks
        self._validate_path()

    def _validate_path(self) -> None:
        """Validate local path exists and is a directory.

        Raises:
            BackendError: If path doesn't exist or is not a directory
        """
        if not self.local_path.exists():
            raise BackendError(
                f"Local path does not exist: {self.local_path}",
                backend="local_connector",
                path=str(self.local_path),
            )
        if not self.local_path.is_dir():
            raise BackendError(
                f"Local path is not a directory: {self.local_path}",
                backend="local_connector",
                path=str(self.local_path),
            )

    @property
    def name(self) -> str:
        """Return the backend name."""
        return "local_connector"

    # =========================================================================
    # Path Translation
    # =========================================================================

    def _to_physical(self, virtual_path: str) -> Path:
        """Convert virtual path to physical path with symlink safety.

        Args:
            virtual_path: Path relative to mount point (e.g., "nexus/README.md")

        Returns:
            Absolute physical path on local filesystem

        Raises:
            BackendError: If resolved path escapes mount root (symlink attack)
        """
        clean = virtual_path.lstrip("/")
        physical = self.local_path / clean

        # Resolve symlinks if enabled
        if self.follow_symlinks:
            try:
                resolved = physical.resolve()
            except OSError:
                # Path doesn't exist yet, resolve parent
                resolved = physical.parent.resolve() / physical.name
        else:
            resolved = physical

        # Security: ensure path doesn't escape mount root
        try:
            resolved.relative_to(self.local_path)
        except ValueError as e:
            raise BackendError(
                f"Path escapes mount root: {virtual_path}",
                backend="local_connector",
                path=virtual_path,
            ) from e

        return resolved

    # =========================================================================
    # Content Operations (with L1 Caching)
    # =========================================================================

    def read_content(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read file content directly from local disk.

        For LocalConnectorBackend, content_id is ignored - we use context.backend_path.

        Args:
            content_id: Ignored (LocalConnectorBackend uses path-based access)
            context: Operation context with backend_path

        Returns:
            File content as bytes

        Raises:
            BackendError: If context/backend_path missing, permission denied, or OS error
            NexusFileNotFoundError: If file does not exist
        """
        if context is None or not context.backend_path:
            raise BackendError("LocalConnectorBackend requires context with backend_path")

        path = context.backend_path
        physical = self._to_physical(path)

        if not physical.exists():
            raise NexusFileNotFoundError(path)
        if not physical.is_file():
            raise BackendError(f"Not a file: {path}")

        try:
            return physical.read_bytes()
        except PermissionError as e:
            raise BackendError(f"Permission denied: {path} - {e}") from e
        except OSError as e:
            raise BackendError(f"Read error: {e}") from e

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: "OperationContext | None" = None,
    ) -> WriteResult:
        """Write content directly to local path.

        Unlike CAS-based backends, this writes directly to the file.
        Returns WriteResult with content hash and size.

        Args:
            content: Bytes to write
            context: Operation context with backend_path

        Returns:
            WriteResult with content_hash (SHA-256) and size

        Raises:
            BackendError: If read-only, missing path, permission denied, or OS error
        """
        if self.readonly:
            raise BackendError("Backend is read-only")

        # Get path from context
        write_path = context.backend_path if context else None

        if write_path is None:
            raise BackendError("Path required for local_connector backend")

        physical = self._to_physical(write_path)

        # Offset write: read-splice-write (Issue #1395)
        if offset > 0:
            try:
                old_data = physical.read_bytes() if physical.exists() else b""
            except OSError:
                old_data = b""
            if offset > len(old_data):
                old_data = old_data + b"\x00" * (offset - len(old_data))
            content = old_data[:offset] + content + old_data[offset + len(content) :]

        try:
            physical.parent.mkdir(parents=True, exist_ok=True)
            physical.write_bytes(content)

            content_hash = hash_content(content)
            # PAS: content_id = physical path (not hash). Kernel stores this
            # in metastore so sys_stat.physical_path returns the real OS path.
            return WriteResult(content_id=str(physical), version=content_hash, size=len(content))
        except PermissionError as e:
            raise BackendError(f"Permission denied: {write_path} - {e}") from e
        except OSError as e:
            raise BackendError(f"Write error: {e}") from e

    # =========================================================================
    # Directory Operations
    # =========================================================================

    def list_dir(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> list[str]:
        """List directory contents.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused for local connector)

        Returns:
            List of entry names in the directory
        """
        physical = self._to_physical(path)

        if not physical.exists() or not physical.is_dir():
            return []

        try:
            return sorted(item.name for item in physical.iterdir())
        except (PermissionError, OSError):
            return []

    def exists(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if path exists.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Returns:
            True if path exists, False otherwise
        """
        try:
            return self._to_physical(path).exists()
        except BackendError:
            # Path escapes mount root
            return False

    # =========================================================================
    # Backend Interface Methods (for Backend abstract base class)
    # =========================================================================

    def delete_content(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Delete content by hash - not supported for local_connector.

        LocalConnectorBackend uses path-based access, not content-hash based.
        This method exists for Backend interface compatibility.

        Raises:
            BackendError: Always (hash-based deletion not supported)
        """
        raise BackendError("delete_content by hash not supported for local_connector.")

    def content_exists(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if content exists by hash - not supported for local_connector."""
        return False

    def get_content_size(
        self,
        content_id: str,
        context: "OperationContext | None" = None,
    ) -> int:
        """Get content size by hash - not supported for local_connector.

        Raises:
            BackendError: Always (hash-based size lookup not supported)
        """
        raise BackendError("get_content_size by hash not supported for local_connector")

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create a directory.

        Args:
            path: Virtual path relative to mount point
            parents: Create parent directories if needed (always True for local_connector)
            exist_ok: Don't error if directory exists (always True for local_connector)
            context: Operation context (unused)

        Raises:
            BackendError: If read-only, permission denied, or OS error
        """
        if self.readonly:
            raise BackendError("Backend is read-only")

        physical = self._to_physical(path)

        try:
            # Always use parents=True, exist_ok=True for simplicity
            physical.mkdir(parents=True, exist_ok=True)
        except PermissionError as e:
            raise BackendError(f"Permission denied: {path} - {e}") from e
        except OSError as e:
            raise BackendError(f"Mkdir error: {e}") from e

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> None:
        """Remove a directory.

        Args:
            path: Virtual path relative to mount point
            recursive: If True, remove directory and contents (not supported)
            context: Operation context (unused)

        Raises:
            BackendError: If recursive, read-only, permission denied, not a directory, or OS error
            NexusFileNotFoundError: If directory does not exist
        """
        if recursive:
            raise BackendError("Recursive rmdir not supported for safety")
        if self.readonly:
            raise BackendError("Backend is read-only")

        physical = self._to_physical(path)

        try:
            if physical.is_dir():
                physical.rmdir()
            else:
                raise BackendError(f"Not a directory: {path}")
        except FileNotFoundError as e:
            raise NexusFileNotFoundError(path) from e
        except PermissionError as e:
            raise BackendError(f"Permission denied: {path} - {e}") from e
        except BackendError:
            raise
        except OSError as e:
            raise BackendError(f"Rmdir error: {e}") from e

    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if path is a directory."""
        try:
            return self._to_physical(path).is_dir()
        except BackendError:
            return False

    def rename(
        self,
        old_path: str,
        new_path: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Rename/move a file or directory.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            context: Operation context (unused)

        Raises:
            BackendError: If read-only, permission denied, or OS error.
            NexusFileNotFoundError: If source path does not exist.
        """
        if self.readonly:
            raise BackendError("Backend is read-only", backend="local_connector", path=old_path)

        old_physical = self._to_physical(old_path)
        new_physical = self._to_physical(new_path)

        if not old_physical.exists():
            raise NexusFileNotFoundError(old_path, message=f"Source not found: {old_path}")

        try:
            # Create parent directories for destination if needed
            new_physical.parent.mkdir(parents=True, exist_ok=True)
            old_physical.rename(new_physical)
        except PermissionError as e:
            raise BackendError(f"Permission denied: {e}") from e
        except OSError as e:
            raise BackendError(f"Rename error: {e}") from e

    def delete(
        self,
        path: str,
        context: "OperationContext | None" = None,
    ) -> None:
        """Delete a file from the host filesystem.

        Called by kernel sys_unlink for PAS backend propagation —
        ensures the physical file is removed when metadata is deleted.

        Args:
            path: Virtual path relative to mount point
            context: Operation context (unused)

        Raises:
            BackendError: If read-only or OS error.
        """
        if self.readonly:
            raise BackendError("Backend is read-only", backend="local_connector", path=path)

        physical = self._to_physical(path)
        if not physical.exists():
            return  # Already gone — idempotent

        try:
            if physical.is_file() or physical.is_symlink():
                physical.unlink()
            # Directories handled by rmdir, not delete
        except PermissionError as e:
            raise BackendError(f"Permission denied: {e}") from e
        except OSError as e:
            raise BackendError(f"Delete error: {e}") from e
