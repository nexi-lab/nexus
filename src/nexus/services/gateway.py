"""NexusFS Gateway - AI-friendly interface for services.

This module provides a gateway pattern for accessing NexusFS operations
from extracted services. It centralizes all NexusFS dependencies into
a single, greppable interface.

Phase 2: Mount Mixin Refactoring
- All service access to NexusFS goes through self._gw
- Explicit method delegation for discoverability
- No protocol hunting required

Example:
    ```python
    class SyncService:
        def __init__(self, gateway: NexusFSGateway):
            self._gw = gateway  # Grep pattern: self._gw.

        def sync_mount(self, ctx):
            self._gw.mkdir(ctx.mount_point, parents=True)
            meta = self._gw.metadata_get(path)
            self._gw.metadata_put(new_meta)
    ```
"""

from __future__ import annotations

import builtins
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.metadata import FileMetadata
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter

logger = logging.getLogger(__name__)


class NexusFSGateway:
    """Gateway providing NexusFS operations to services.

    AI-Friendly Design:
    - Single object to grep: self._fs or self._gw
    - Explicit method delegation
    - No protocol hunting required

    Dependencies exposed:
    - File ops: mkdir(), write()
    - Metadata: metadata_get/put/list/delete
    - Permissions: rebac_create/check/delete_object_tuples
    - Hierarchy: ensure_parent_tuples_batch, hierarchy_enabled
    - Router: router property
    - Session: session_factory property
    """

    def __init__(self, fs: NexusFS):
        """Initialize gateway with NexusFS instance.

        Args:
            fs: NexusFS instance to wrap
        """
        self._fs = fs

    # =========================================================================
    # File Operations
    # =========================================================================

    def mkdir(
        self,
        path: str,
        *,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Create directory at path.

        Args:
            path: Virtual path for directory
            parents: If True, create parent directories as needed
            exist_ok: If True, don't raise if directory exists
            context: Operation context for permissions
        """
        if hasattr(self._fs, "mkdir"):
            self._fs.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)
        else:
            logger.warning(f"[Gateway] mkdir not available, skipping: {path}")

    def write(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
    ) -> None:
        """Write content to file.

        Args:
            path: Virtual path for file
            content: File content (bytes or str)
            context: Operation context for permissions
        """
        if isinstance(content, str):
            content = content.encode("utf-8")
        if hasattr(self._fs, "write"):
            self._fs.write(path, content, context=context)
        else:
            logger.warning(f"[Gateway] write not available, skipping: {path}")

    def read(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bytes | str:
        """Read content from file.

        Args:
            path: Virtual path for file
            context: Operation context for permissions

        Returns:
            File content as bytes or str
        """
        if hasattr(self._fs, "read"):
            result = self._fs.read(path, context=context)
            # Normalize to bytes or str
            if isinstance(result, (bytes, str)):
                return result
            # Handle dict results (parsed content) by returning empty bytes
            return b""
        raise RuntimeError(f"[Gateway] read not available for: {path}")

    def list(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> builtins.list[str]:
        """List directory contents.

        Args:
            path: Virtual path for directory
            context: Operation context for permissions

        Returns:
            List of paths in directory
        """
        if hasattr(self._fs, "list"):
            result = self._fs.list(path, context=context)
            # Handle PaginatedResult vs raw list
            items = result.items if hasattr(result, "items") else result
            # Convert to list of strings
            return [str(item) for item in items]
        return []

    def exists(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path exists.

        Args:
            path: Virtual path to check
            context: Operation context for permissions

        Returns:
            True if path exists, False otherwise
        """
        if hasattr(self._fs, "exists"):
            return self._fs.exists(path, context=context)
        return False

    # =========================================================================
    # Metadata Operations
    # =========================================================================

    def metadata_get(self, path: str) -> FileMetadata | None:
        """Get metadata for path.

        Args:
            path: Virtual path to look up

        Returns:
            FileMetadata if found, None otherwise
        """
        if hasattr(self._fs, "metadata") and hasattr(self._fs.metadata, "get"):
            return self._fs.metadata.get(path)
        return None

    def metadata_put(self, meta: FileMetadata) -> None:
        """Store metadata.

        Args:
            meta: FileMetadata to store
        """
        if hasattr(self._fs, "metadata") and hasattr(self._fs.metadata, "put"):
            self._fs.metadata.put(meta)

    def metadata_list(self, prefix: str, recursive: bool = False) -> builtins.list[FileMetadata]:
        """List metadata entries under prefix.

        Args:
            prefix: Path prefix to search
            recursive: If True, include nested entries

        Returns:
            List of FileMetadata entries
        """
        if hasattr(self._fs, "metadata") and hasattr(self._fs.metadata, "list"):
            return list(self._fs.metadata.list(prefix=prefix, recursive=recursive))
        return []

    def metadata_delete(self, path: str) -> None:
        """Delete metadata for path.

        Args:
            path: Virtual path to delete
        """
        if hasattr(self._fs, "metadata") and hasattr(self._fs.metadata, "delete"):
            self._fs.metadata.delete(path)

    def metadata_delete_batch(self, paths: builtins.list[str]) -> None:
        """Delete metadata for multiple paths in a single transaction.

        Args:
            paths: List of virtual paths to delete
        """
        if hasattr(self._fs, "metadata") and hasattr(self._fs.metadata, "delete_batch"):
            self._fs.metadata.delete_batch(paths)

    def delete_directory_entries_recursive(self, path: str, zone_id: str | None = None) -> int:
        """Delete all directory entries under a path (recursive).

        Cleans up the sparse directory index for a path and all descendants.
        Used for mount point cleanup.

        Args:
            path: Virtual path to clean up
            zone_id: Zone ID for multi-zone isolation

        Returns:
            Number of entries deleted
        """
        if hasattr(self._fs, "metadata") and hasattr(
            self._fs.metadata, "delete_directory_entries_recursive"
        ):
            return self._fs.metadata.delete_directory_entries_recursive(path, zone_id)
        return 0

    # =========================================================================
    # Permission Operations (ReBAC)
    # =========================================================================

    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Create ReBAC permission tuple.

        Args:
            subject: (subject_type, subject_id) tuple
            relation: Relation name (e.g., "direct_owner")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy

        Returns:
            Tuple ID if created, None otherwise
        """
        if hasattr(self._fs, "rebac_create"):
            return self._fs.rebac_create(
                subject=subject,
                relation=relation,
                object=object,
                zone_id=zone_id,
            )
        return None

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> bool:
        """Check if subject has permission on object.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., "read")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy

        Returns:
            True if permission granted, False otherwise
        """
        if hasattr(self._fs, "rebac_check"):
            return self._fs.rebac_check(
                subject=subject,
                permission=permission,
                object=object,
                zone_id=zone_id,
            )
        return False

    def rebac_delete_object_tuples(
        self,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> int:
        """Delete all permission tuples for an object.

        Args:
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy

        Returns:
            Number of tuples deleted
        """
        if hasattr(self._fs, "rebac_delete_object_tuples"):
            result: int = self._fs.rebac_delete_object_tuples(
                object=object,
                zone_id=zone_id,
            )
            return result
        return 0

    # =========================================================================
    # Hierarchy Operations
    # =========================================================================

    @property
    def hierarchy_enabled(self) -> bool:
        """Check if hierarchy manager is enabled.

        Returns:
            True if hierarchy manager exists and inheritance is enabled
        """
        if hasattr(self._fs, "_hierarchy_manager") and self._fs._hierarchy_manager:
            return getattr(self._fs._hierarchy_manager, "enable_inheritance", False)
        return False

    def ensure_parent_tuples_batch(
        self,
        paths: builtins.list[str],
        zone_id: str | None = None,
    ) -> int:
        """Create parent tuples for paths in batch.

        Args:
            paths: List of virtual paths
            zone_id: Zone ID for multi-tenancy

        Returns:
            Number of tuples created
        """
        if (
            hasattr(self._fs, "_hierarchy_manager")
            and self._fs._hierarchy_manager
            and hasattr(self._fs._hierarchy_manager, "ensure_parent_tuples_batch")
        ):
            return self._fs._hierarchy_manager.ensure_parent_tuples_batch(paths, zone_id=zone_id)
        return 0

    def remove_parent_tuples(
        self,
        path: str,
        zone_id: str | None = None,
    ) -> int:
        """Remove parent tuples for a path.

        Args:
            path: Virtual path
            zone_id: Zone ID for multi-tenancy

        Returns:
            Number of tuples removed
        """
        if (
            hasattr(self._fs, "_hierarchy_manager")
            and self._fs._hierarchy_manager
            and hasattr(self._fs._hierarchy_manager, "remove_parent_tuples")
        ):
            return self._fs._hierarchy_manager.remove_parent_tuples(path, zone_id=zone_id)
        return 0

    # =========================================================================
    # Router Access
    # =========================================================================

    @property
    def router(self) -> PathRouter:
        """Get the path router.

        Returns:
            PathRouter instance
        """
        return self._fs.router

    # =========================================================================
    # Session Factory
    # =========================================================================

    @property
    def session_factory(self) -> Any:
        """Get SQLAlchemy session factory.

        Returns:
            SessionLocal factory if available, None otherwise
        """
        if hasattr(self._fs, "SessionLocal"):
            return self._fs.SessionLocal
        return None

    # =========================================================================
    # Database URL
    # =========================================================================

    def get_database_url(self) -> str:
        """Get database URL for OAuth backends.

        Returns:
            Database URL string

        Raises:
            RuntimeError: If database URL cannot be determined
        """
        from nexus.core.context_utils import get_database_url

        return get_database_url(self._fs)

    # =========================================================================
    # Mount Listing (for sync_all_mounts)
    # =========================================================================

    def list_mounts(self) -> builtins.list[dict[str, Any]]:
        """List all active mounts.

        Returns:
            List of mount info dictionaries
        """
        mounts = []
        for mount_info in self.router.list_mounts():
            mounts.append(
                {
                    "mount_point": mount_info.mount_point,
                    "priority": mount_info.priority,
                    "readonly": mount_info.readonly,
                    "backend_type": type(mount_info.backend).__name__,
                    "backend": mount_info.backend,
                }
            )
        return mounts
