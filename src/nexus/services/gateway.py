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
    class MountService:
        def __init__(self, gateway: NexusFSGateway):
            self._gw = gateway  # Grep pattern: self._gw.

        async def sync_mount(self, ctx):
            self._gw.mkdir(ctx.mount_point, parents=True)
            meta = self._gw.metadata_get(path)
            self._gw.metadata_put(new_meta)
    ```
"""

import builtins
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata
    from nexus.contracts.types import OperationContext, Permission
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


class NexusFSGateway:
    """Gateway providing NexusFS operations to services.

    AI-Friendly Design:
    - Single object to grep: self._fs or self._gw
    - Explicit method delegation
    - No protocol hunting required

    Dependencies exposed:
    - File ops: mkdir(), sys_write(), sys_read(), sys_readdir(), access()
    - Metadata: metadata_get/put/list/delete
    - Permissions: rebac_create/check/delete_object_tuples
    - Hierarchy: ensure_parent_tuples_batch, hierarchy_enabled
    - Router: router property
    - Session: session_factory property
    """

    def __init__(self, fs: "NexusFS"):
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
        parents: bool = True,
        exist_ok: bool = True,
        context: "OperationContext | None" = None,
    ) -> None:
        """Create directory at path.

        Args:
            path: Virtual path for directory
            parents: If True, create parent directories as needed
            exist_ok: If True, don't raise if directory exists
            context: Operation context for permissions
        """
        self._fs.mkdir(path, parents=parents, exist_ok=exist_ok, context=context)

    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Write content to file (POSIX pwrite(2)).

        Args:
            path: Virtual path for file
            buf: File content (bytes or str, str auto-encoded to UTF-8)
            context: Operation context for permissions

        Returns:
            Dict with path, bytes_written, and created flag.
        """
        return self._fs.sys_write(path, buf, context=context)

    def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Write content to file with create-on-write semantics (Tier 2).

        Unlike sys_write, this creates the file if it doesn't exist.

        Args:
            path: Virtual path for file
            buf: File content (bytes or str, str auto-encoded to UTF-8)
            context: Operation context for permissions

        Returns:
            Dict with path, bytes_written, and created flag.
        """
        return self._fs.write(path, buf, context=context)

    def sys_read(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread(2)).

        Args:
            path: Virtual path for file
            context: Operation context for permissions

        Returns:
            File content as bytes.
        """
        return self._fs.sys_read(path, context=context)

    def sys_readdir(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,
    ) -> builtins.list[str]:
        """List directory contents (POSIX readdir).

        Args:
            path: Virtual path for directory
            context: Operation context for permissions

        Returns:
            List of paths in directory
        """
        result = self._fs.sys_readdir(path, context=context)
        # Handle PaginatedResult vs raw list
        items = result.items if hasattr(result, "items") else result
        # Convert to list of strings
        return [str(item) for item in items]

    def access(
        self,
        path: str,
        *,
        context: "OperationContext | None" = None,
    ) -> bool:
        """Check if path exists (POSIX access).

        Args:
            path: Virtual path to check
            context: Operation context for permissions

        Returns:
            True if path exists, False otherwise
        """
        return self._fs.access(path, context=context)

    # =========================================================================
    # Metadata Operations
    # =========================================================================

    def metadata_get(self, path: str) -> "FileMetadata | None":
        """Get metadata for path.

        Args:
            path: Virtual path to look up

        Returns:
            FileMetadata if found, None otherwise
        """
        if hasattr(self._fs.metadata, "get"):
            return self._fs.metadata.get(path)
        return None

    def metadata_put(self, meta: "FileMetadata") -> None:
        """Store metadata.

        Args:
            meta: FileMetadata to store
        """
        if hasattr(self._fs.metadata, "put"):
            self._fs.metadata.put(meta)

    def metadata_list(self, prefix: str, recursive: bool = False) -> "builtins.list[FileMetadata]":
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

    def delete_directory_entries_recursive(self, path: str, zone_id: str | None = None) -> Any:
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
    # Permission Operations (ReBAC) — delegates to ReBACService (Issue #2033)
    # =========================================================================

    @property
    def _rebac_service(self) -> Any:
        """Get the ReBACService instance from NexusFS."""
        return self._fs.service("rebac")

    def rebac_create(
        self,
        subject: tuple[str, str],
        relation: str,
        object: tuple[str, str],
        zone_id: str | None = None,
        context: "OperationContext | None" = None,
    ) -> Any:
        """Create ReBAC permission tuple.

        Args:
            subject: (subject_type, subject_id) tuple (or 3-tuple for userset)
            relation: Relation name (e.g., "direct_owner", "direct_viewer")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy
            context: Operation context

        Returns:
            Dict with tuple_id/revision/consistency_token if created, None otherwise
        """
        return self._rebac_service.rebac_create_sync(
            subject=subject,
            relation=relation,
            object=object,
            zone_id=zone_id,
            context=context,
        )

    def rebac_check(
        self,
        subject: tuple[str, str],
        permission: str,
        object: tuple[str, str],
        zone_id: str | None = None,
    ) -> Any:
        """Check if subject has permission on object.

        Args:
            subject: (subject_type, subject_id) tuple
            permission: Permission to check (e.g., "read")
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy

        Returns:
            True if permission granted, False otherwise
        """
        return self._rebac_service.rebac_check_sync(
            subject=subject,
            permission=permission,
            object=object,
            zone_id=zone_id,
        )

    def rebac_delete_object_tuples(
        self,
        object: tuple[str, str],
        zone_id: str | None = None,  # noqa: ARG002
    ) -> int:
        """Delete all permission tuples for an object.

        Args:
            object: (object_type, object_id) tuple
            zone_id: Zone ID for multi-tenancy

        Returns:
            Number of tuples deleted
        """
        tuples = self._rebac_service.rebac_list_tuples_sync(object=object)
        deleted = 0
        for t in tuples:
            tid = t.get("tuple_id")
            if tid and self._rebac_service.rebac_delete_sync(tid):
                deleted += 1
        return deleted

    def rebac_list_tuples(
        self,
        subject: tuple[str, str] | None = None,
        relation: str | None = None,
        object: tuple[str, str] | None = None,
        relation_in: builtins.list[str] | None = None,
    ) -> Any:
        """List ReBAC relationship tuples matching filters.

        Args:
            subject: Optional (subject_type, subject_id) filter
            relation: Optional relation type filter
            object: Optional (object_type, object_id) filter
            relation_in: Optional list of relation types to filter

        Returns:
            List of tuple dictionaries
        """
        return self._rebac_service.rebac_list_tuples_sync(
            subject=subject,
            relation=relation,
            object=object,
            relation_in=relation_in,
        )

    def rebac_delete(self, tuple_id: str) -> Any:
        """Delete a single ReBAC tuple by ID.

        Args:
            tuple_id: The tuple ID to delete

        Returns:
            True if deleted, False otherwise
        """
        return self._rebac_service.rebac_delete_sync(tuple_id)

    @property
    def rebac_manager(self) -> Any:
        """Get the ReBAC manager instance.

        Returns:
            ReBACManager if available, None otherwise
        """
        return getattr(self._fs, "_rebac_manager", None)

    # =========================================================================
    # Hierarchy Operations
    # =========================================================================

    @property
    def hierarchy_enabled(self) -> bool:
        """Check if hierarchy manager is enabled.

        Returns:
            True if hierarchy manager exists and inheritance is enabled
        """
        rm = self.rebac_manager
        hm = getattr(rm, "hierarchy_manager", None) if rm is not None else None
        if hm is not None:
            return getattr(hm, "enable_inheritance", False)
        return False

    def ensure_parent_tuples_batch(
        self,
        paths: builtins.list[str],
        zone_id: str | None = None,
    ) -> Any:
        """Create parent tuples for paths in batch.

        Args:
            paths: List of virtual paths
            zone_id: Zone ID for multi-tenancy

        Returns:
            Number of tuples created
        """
        rm = self.rebac_manager
        hm = getattr(rm, "hierarchy_manager", None) if rm is not None else None
        if hm is not None and hasattr(hm, "ensure_parent_tuples_batch"):
            return hm.ensure_parent_tuples_batch(paths, zone_id=zone_id)
        return 0

    def remove_parent_tuples(
        self,
        path: str,
        zone_id: str | None = None,
    ) -> Any:
        """Remove parent tuples for a path."""
        rm = self.rebac_manager
        hm = getattr(rm, "hierarchy_manager", None) if rm is not None else None
        if hm is not None and hasattr(hm, "remove_parent_tuples"):
            return hm.remove_parent_tuples(path, zone_id=zone_id)
        return 0

    # =========================================================================
    # Router Access
    # =========================================================================

    @property
    def router(self) -> Any:
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

    @property
    def record_store(self) -> Any:
        """Get RecordStoreABC instance.

        Returns:
            RecordStoreABC if available, None otherwise
        """
        return getattr(self._fs, "_record_store", None)

    # =========================================================================
    # Database URL
    # =========================================================================

    @property
    def is_postgresql(self) -> bool:
        """Check if the database is PostgreSQL (config-time detection)."""
        try:
            url = self.get_database_url()
            return url.startswith(("postgres", "postgresql"))
        except Exception:
            return False

    def get_database_url(self) -> str:
        """Get database URL for OAuth backends.

        Returns:
            Database URL string

        Raises:
            RuntimeError: If database URL cannot be determined
        """
        from nexus.lib.context_utils import get_database_url

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
                    "backend_type": type(mount_info.backend).__name__,
                    "backend": mount_info.backend,
                    "conflict_strategy": mount_info.conflict_strategy,
                }
            )
        return mounts

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None:
        """Get mount info and backend-relative path for a virtual path.

        Resolve mount info and backend-relative path for a virtual path.
        Mounts are checked longest-prefix-first so that more-specific mounts
        (e.g. ``/mnt/foo``) match before less-specific ones (e.g. ``/``).

        Args:
            path: Virtual file path

        Returns:
            Dict with mount_point, backend, backend_path keys,
            or None if no mount matches.
        """
        # Sort by mount_point length descending so longest prefix matches first
        mounts = sorted(self.router.list_mounts(), key=lambda m: len(m.mount_point), reverse=True)
        for mount in mounts:
            mp = mount.mount_point
            if path == mp or path.startswith(mp + "/") or mp == "/":
                # Strip mount prefix to get backend-relative path
                backend_path = path.lstrip("/") if mp == "/" else path[len(mp) :].lstrip("/")
                return {
                    "mount_point": mp,
                    "backend": mount.backend,
                    "backend_path": backend_path,
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
        context: "OperationContext | None" = None,
        return_metadata: bool = False,
    ) -> bytes | str | dict[str, Any]:
        """Read file content with optional metadata return.

        Unlike the simpler `sys_read()` method, this supports the full NexusFS
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
        context: "OperationContext | None" = None,
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
        context: "OperationContext | None",
    ) -> tuple[str | None, str | None, bool]:
        """Extract zone_id, agent_id, is_admin from operation context.

        Args:
            context: Operation context

        Returns:
            Tuple of (zone_id, agent_id, is_admin)
        """
        return self._fs._get_context_identity(context)

    def has_descendant_access(
        self,
        path: str,
        permission: "Permission",
        context: "OperationContext | None",
    ) -> Any:
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
        assert context is not None, "context required for has_descendant_access"
        return self._fs._descendant_checker.has_access(path, permission, context)

    def record_read_if_tracking(
        self,
        context: "OperationContext | None",
        resource_type: str,
        resource_id: str,
        access_type: str = "content",
    ) -> None:
        """Record a read operation for dependency tracking (Issue #1166).

        Delegates to ``OperationContext.record_read()`` when read-tracking
        is enabled.  The revision is fetched from the kernel zone revision
        counter (§10 A2 — pure Rust AtomicU64, replaces RevisionNotifier).

        Args:
            context: Operation context (may contain dependency tracker)
            resource_type: Type of resource read (e.g., "file")
            resource_id: Identifier for the resource
            access_type: Type of access (default: "content")
        """
        if context is None or not context.track_reads:
            return
        kernel = getattr(self._fs, "_kernel", None)
        zone_id = getattr(context, "zone_id", None) or "root"
        revision = kernel.get_zone_revision(zone_id) if kernel else 0
        context.record_read(resource_type, resource_id, revision, access_type)

    @property
    def backend(self) -> Any:
        """Get the storage backend.

        Used by search for memory path content size lookups.

        Returns:
            Storage backend if available, None otherwise
        """
        return getattr(self._fs, "backend", None)
