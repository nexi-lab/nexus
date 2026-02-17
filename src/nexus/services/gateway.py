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
    from nexus.core._metadata_generated import FileMetadata
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.permissions import OperationContext, Permission
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
        self._fs.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

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
        self._fs.write(path, content, context=context)

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
        result = self._fs.read(path, context=context)
        # Normalize to bytes or str
        if isinstance(result, (bytes, str)):
            return result
        # Handle dict results (parsed content) by returning empty bytes
        return b""

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
        result = self._fs.list(path, context=context)
        # Handle PaginatedResult vs raw list
        items = result.items if hasattr(result, "items") else result
        # Convert to list of strings
        return [str(item) for item in items]

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
        return self._fs.exists(path, context=context)

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
        if hasattr(self._fs.metadata, "get"):
            return self._fs.metadata.get(path)
        return None

    def metadata_put(self, meta: FileMetadata) -> None:
        """Store metadata.

        Args:
            meta: FileMetadata to store
        """
        if hasattr(self._fs.metadata, "put"):
            self._fs.metadata.put(meta)

    def metadata_list(self, prefix: str, recursive: bool = False) -> builtins.list[FileMetadata]:
        """List metadata entries under prefix.

        Args:
            prefix: Path prefix to search
            recursive: If True, include nested entries

        Returns:
            List of FileMetadata entries
        """
        if hasattr(self._fs.metadata, "list"):
            return list(self._fs.metadata.list(prefix=prefix, recursive=recursive))
        return []

    def metadata_delete(self, path: str) -> None:
        """Delete metadata for path.

        Args:
            path: Virtual path to delete
        """
        if hasattr(self._fs.metadata, "delete"):
            self._fs.metadata.delete(path)

    def metadata_delete_batch(self, paths: builtins.list[str]) -> None:
        """Delete metadata for multiple paths in a single transaction.

        Args:
            paths: List of virtual paths to delete
        """
        if hasattr(self._fs.metadata, "delete_batch"):
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
        if hasattr(self._fs.metadata, "delete_directory_entries_recursive"):
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
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Create ReBAC permission tuple.

        Args:
            subject: (subject_type, subject_id) tuple (or 3-tuple for userset)
            relation: Relation name (e.g., "direct_owner", "direct_viewer")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy
            context: Operation context (passed through to NexusFS)

        Returns:
            Dict with tuple_id/revision/consistency_token if created, None otherwise
        """
        kwargs: dict[str, Any] = {
            "subject": subject,
            "relation": relation,
            "object": object,
            "zone_id": zone_id,
        }
        if context is not None:
            kwargs["context"] = context
        return self._fs.rebac_create(**kwargs)

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
        return self._fs.rebac_check(
            subject=subject,
            permission=permission,
            object=object,
            zone_id=zone_id,
        )

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
        result: int = self._fs.rebac_delete_object_tuples(
            object=object,
            zone_id=zone_id,
        )
        return result

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: builtins.list[str] | None = None,
    ) -> builtins.list[dict[str, Any]]:
        """List ReBAC relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter
            object: Optional (object_type, object_id) filter
            relation_in: Optional list of relation types to filter

        Returns:
            List of tuple dictionaries
        """
        return self._fs.rebac_list_tuples(
            subject=subject,
            relation=relation,
            object=object,
            relation_in=relation_in,
        )

    def rebac_delete(self, tuple_id: str) -> bool:
        """Delete a single ReBAC tuple by ID.

        Args:
            tuple_id: The tuple ID to delete

        Returns:
            True if deleted, False otherwise
        """
        if self._fs._rebac_manager is not None:
            self._fs._rebac_manager.rebac_delete(tuple_id)
            return True
        return False

    @property
    def rebac_manager(self) -> Any:
        """Get the ReBAC manager instance.

        Returns:
            EnhancedReBACManager if available, None otherwise
        """
        return self._fs._rebac_manager

    def invalidate_metadata_cache(self, *paths: str) -> None:
        """Invalidate metadata cache entries for given paths.

        Args:
            paths: Virtual paths to invalidate
        """
        metadata_cache = getattr(self._fs, "metadata_cache", None)
        if metadata_cache is not None:
            for path in paths:
                metadata_cache.invalidate_path(path)

    # =========================================================================
    # Hierarchy Operations
    # =========================================================================

    @property
    def hierarchy_enabled(self) -> bool:
        """Check if hierarchy manager is enabled.

        Returns:
            True if hierarchy manager exists and inheritance is enabled
        """
        if self._fs._hierarchy_manager is not None:
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
        if self._fs._hierarchy_manager is not None and hasattr(
            self._fs._hierarchy_manager, "ensure_parent_tuples_batch"
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
        if self._fs._hierarchy_manager is not None and hasattr(
            self._fs._hierarchy_manager, "remove_parent_tuples"
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
                    "conflict_strategy": mount_info.conflict_strategy,
                }
            )
        return mounts

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None:
        """Get mount info and backend-relative path for a virtual path.

        Used by WriteBackService to resolve which backend to write back to.

        Args:
            path: Virtual file path

        Returns:
            Dict with mount_point, backend, backend_path, readonly keys,
            or None if no mount matches.
        """
        for mount in self.router.list_mounts():
            mp = mount.mount_point
            if path == mp or path.startswith(mp + "/") or mp == "/":
                # Strip mount prefix to get backend-relative path
                backend_path = path.lstrip("/") if mp == "/" else path[len(mp) :].lstrip("/")
                return {
                    "mount_point": mp,
                    "backend": mount.backend,
                    "backend_path": backend_path,
                    "readonly": mount.readonly,
                    "backend_name": getattr(mount.backend, "name", type(mount.backend).__name__),
                    "conflict_strategy": mount.conflict_strategy,
                }
        return None

    # =========================================================================
    # Search Operations (Issue #1287: replaces 8 Callable[..., Any] params)
    # =========================================================================

    def read_file(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | str | dict[str, Any]:
        """Read file content with optional metadata return.

        Unlike the simpler `read()` method, this supports the full NexusFS
        read interface including metadata return for search indexing.

        Args:
            path: Virtual path for file
            context: Operation context for permissions
            return_metadata: If True, return metadata dict instead of raw content

        Returns:
            File content as bytes/str, or metadata dict if return_metadata=True
        """
        return self._fs.read(path, context=context, return_metadata=return_metadata)

    def read_bulk(
        self,
        paths: builtins.list[str],
        *,
        context: OperationContext | None = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """Bulk read multiple files.

        Args:
            paths: List of virtual paths to read
            context: Operation context for permissions
            return_metadata: If True, return metadata dicts
            skip_errors: If True, skip files that fail to read

        Returns:
            Dict mapping path -> content (or None on error if skip_errors)
        """
        return self._fs.read_bulk(
            paths,
            context=context,
            return_metadata=return_metadata,
            skip_errors=skip_errors,
        )

    def get_routing_params(
        self,
        context: OperationContext | None,
    ) -> tuple[str | None, str | None, bool]:
        """Extract zone_id, agent_id, is_admin from operation context.

        Args:
            context: Operation context

        Returns:
            Tuple of (zone_id, agent_id, is_admin)
        """
        return self._fs._get_routing_params(context)

    def has_descendant_access(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | None,
    ) -> bool:
        """Check if user has access to any descendant of path.

        Used by search to determine whether to include a directory
        in results even when the user lacks direct read access.

        Args:
            path: Virtual directory path
            permission: Permission to check
            context: Operation context

        Returns:
            True if access exists to any descendant
        """
        assert context is not None, "context required for _has_descendant_access"
        return self._fs._has_descendant_access(path, permission, context)

    def get_backend_directory_entries(self, path: str) -> set[str]:
        """Get directory entries directly from backend storage.

        Bypasses metadata store to get raw backend listing.
        Used for merge-listing in search operations.

        Args:
            path: Virtual directory path

        Returns:
            Set of entry names in the directory
        """
        return self._fs._get_backend_directory_entries(path)

    def record_read_if_tracking(
        self,
        context: OperationContext | None,
        resource_type: str,
        resource_id: str,
        access_type: str = "content",
    ) -> None:
        """Record a read operation for dependency tracking (Issue #1166).

        Args:
            context: Operation context (may contain dependency tracker)
            resource_type: Type of resource read (e.g., "file")
            resource_id: Identifier for the resource
            access_type: Type of access (default: "content")
        """
        self._fs._record_read_if_tracking(
            context,
            resource_type,
            resource_id,
            access_type,
        )

    @property
    def backend(self) -> Any:
        """Get the storage backend.

        Used by search for memory path content size lookups.

        Returns:
            Storage backend if available, None otherwise
        """
        return getattr(self._fs, "backend", None)
