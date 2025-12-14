"""Mount management operations for NexusFS.

This module contains mount management operations:
- add_mount: Add dynamic backend mount
- remove_mount: Remove backend mount
- list_mounts: List all active mounts
- get_mount: Get mount details
- save_mount: Persist mount to database
- load_mounts: Load persisted mounts from database
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

from nexus.backends.backend import Backend
from nexus.core.context_utils import get_database_url, get_tenant_id, get_user_identity
from nexus.core.rpc_decorator import rpc_expose

if TYPE_CHECKING:
    from nexus.core.mount_manager import MountManager
    from nexus.core.permissions import OperationContext
    from nexus.core.router import PathRouter

# Module-level logger
logger = logging.getLogger(__name__)

# Type alias for progress callback: (files_scanned: int, current_path: str) -> None
ProgressCallback = Callable[[int, str], None]


@dataclass
class SyncMountContext:
    """Context object for sync_mount operations.

    Groups all parameters needed for syncing a mount to reduce parameter passing.
    """

    mount_point: str
    path: str | None
    recursive: bool
    dry_run: bool
    sync_content: bool
    include_patterns: list[str] | None
    exclude_patterns: list[str] | None
    generate_embeddings: bool
    context: OperationContext | None
    # Progress callback for async sync (Issue #609)
    progress_callback: ProgressCallback | None = None
    # Additional fields populated during sync
    backend: Any | None = None
    created_by: str | None = None
    has_hierarchy: bool = False


class MetadataSyncResult(NamedTuple):
    """Result from metadata sync operation."""

    stats: dict[str, int | list[str]]
    files_found_in_backend: set[str]


class NexusFSMountsMixin:
    """Mixin providing mount management operations for NexusFS."""

    # Type hints for attributes that will be provided by NexusFS parent class
    if TYPE_CHECKING:
        router: PathRouter
        mount_manager: MountManager | None

    def _grant_mount_owner_permission(
        self, mount_point: str, context: OperationContext | None
    ) -> None:
        """Grant direct_owner permission to the user who created the mount.

        This helper function is called after successfully creating a mount to
        automatically grant the creator full access to the mounted backend.
        It also creates a directory entry for the mount point.

        Args:
            mount_point: The virtual path of the mount
            context: Operation context containing user/subject information
        """
        logger.info(f"Setting up mount point: {mount_point}")

        # Create directory entry for the mount point
        try:
            if hasattr(self, "mkdir"):
                self.mkdir(mount_point, parents=True, exist_ok=True, context=context)
                logger.info(f"✓ Created directory entry for mount point: {mount_point}")
            else:
                logger.warning(
                    "[MOUNT-DIR] mkdir method not available, skipping directory creation"
                )
        except Exception as e:
            # Log but don't fail the mount operation if directory creation fails
            logger.warning(f"Failed to create directory entry for mount {mount_point}: {e}")

        # Grant direct_owner permission
        if not context or not hasattr(context, "subject_id") or not context.subject_id:
            logger.warning("[MOUNT-PERM] Skipping permission grant - no context or subject_id")
            return

        try:
            # Get tenant and subject info from context
            tenant_id = get_tenant_id(context)
            subject_type, subject_id = get_user_identity(context)

            # Create permission tuple using rebac_create method
            if hasattr(self, "rebac_create") and subject_id:
                tuple_id = self.rebac_create(
                    subject=(subject_type, subject_id),
                    relation="direct_owner",
                    object=("file", mount_point),
                    tenant_id=tenant_id,
                )

                logger.info(
                    f"✓ Granted direct_owner permission to {subject_type}:{context.subject_id} "
                    f"for mount {mount_point} (tenant={tenant_id}, tuple_id={tuple_id})"
                )
            else:
                logger.warning(
                    "[MOUNT-PERM] rebac_create method not available, skipping permission grant"
                )
        except Exception as e:
            # Log but don't fail the mount operation if permission grant fails
            logger.warning(f"Failed to grant direct_owner permission for mount {mount_point}: {e}")

    def _generate_connector_skill(
        self, mount_point: str, backend_type: str, context: OperationContext | None
    ) -> bool:
        """Generate SKILL.md for a connector mount.

        Creates a skill document describing the connector's capabilities,
        folder structure, and operations. Uses pre-defined templates from
        configs/connector-skills/ when available.

        Args:
            mount_point: The virtual path of the mount (e.g., "/mnt/gdrive")
            backend_type: Backend type (e.g., "gdrive_connector", "s3_connector")
            context: Operation context containing user/subject information

        Returns:
            True if skill was generated, False otherwise
        """
        try:
            from nexus.backends.service_map import ServiceMap
            from nexus.skills.skill_generator import generate_skill_md

            # Get unified service name from backend type
            service_name = ServiceMap.get_service_name(connector=backend_type)
            if not service_name:
                # Use backend type as service name if not in registry
                service_name = backend_type.replace("_connector", "").replace("_", "-")
                logger.debug(f"No service mapping for {backend_type}, using: {service_name}")

            # Determine skill path based on context
            # Default to system tier, but could use tenant/user tiers based on context
            if context and hasattr(context, "user_id") and context.user_id:
                skill_base_path = f"/skills/users/{context.user_id}/"
            elif context and hasattr(context, "tenant_id") and context.tenant_id:
                skill_base_path = f"/skills/tenants/{context.tenant_id}/"
            else:
                skill_base_path = "/skills/system/"

            skill_path = f"{skill_base_path}{service_name}/"
            skill_md_path = f"{skill_path}SKILL.md"

            # Generate skill content with connector template
            # Note: If MCP was connected first, user can re-run mcp_connect to regenerate
            # merged SKILL.md (mcp_connect uses ServiceMap to include connector template)
            skill_md = generate_skill_md(
                service_name=service_name,
                mount_path=mount_point,
            )

            # Create skill directory and write SKILL.md
            if hasattr(self, "mkdir"):
                try:
                    self.mkdir(skill_path, parents=True, exist_ok=True, context=context)
                except Exception as mkdir_e:
                    logger.warning(f"Failed to create skill directory {skill_path}: {mkdir_e}")

            if hasattr(self, "write"):
                self.write(
                    skill_md_path,
                    skill_md.encode("utf-8") if isinstance(skill_md, str) else skill_md,
                    context=context,
                )
                logger.info(f"Generated connector skill: {skill_md_path}")
                return True
            else:
                logger.warning("write method not available, skipping skill generation")
                return False

        except Exception as e:
            # Log but don't fail the mount operation
            logger.warning(f"Failed to generate skill for {backend_type} at {mount_point}: {e}")
            return False

    @rpc_expose(description="Add dynamic backend mount")
    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: OperationContext | None = None,
    ) -> str:
        """Add a dynamic backend mount to the filesystem.

        This adds a backend mount at runtime without requiring server restart.
        Useful for user-specific storage, temporary backends, or multi-tenant scenarios.

        Automatically grants direct_owner permission to the user who creates the mount.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Backend type - "local", "gcs", "gcs_connector", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority - higher values take precedence (default: 0)
            readonly: Whether mount is read-only (default: False)
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            ValueError: If mount_point already exists or configuration is invalid
            RuntimeError: If backend type is not supported

        Examples:
            >>> # Add personal GCS mount (CAS-based)
            >>> mount_id = nx.add_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="gcs",
            ...     backend_config={
            ...         "bucket": "alice-personal-bucket",
            ...         "project_id": "my-project"
            ...     },
            ...     priority=10
            ... )

            >>> # Add GCS connector mount (direct path mapping for external buckets)
            >>> mount_id = nx.add_mount(
            ...     mount_point="/workspace/gdrive",
            ...     backend_type="gcs_connector",
            ...     backend_config={
            ...         "bucket": "my-external-bucket",
            ...         "project_id": "my-project",
            ...         "prefix": "workspace"  # Optional prefix in bucket
            ...     }
            ... )

            >>> # Add local shared mount
            >>> mount_id = nx.add_mount(
            ...     mount_point="/shared/team",
            ...     backend_type="local",
            ...     backend_config={"data_dir": "/mnt/shared"},
            ...     readonly=True
            ... )
        """
        # Auto-inject token_manager_db for OAuth-backed connectors
        if (
            backend_type in ("gdrive_connector", "gmail_connector", "x_connector")
            and "token_manager_db" not in backend_config
        ):
            # Use centralized database URL resolution
            try:
                database_url = get_database_url(self)
                backend_config = {**backend_config, "token_manager_db": database_url}
            except RuntimeError as e:
                raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e

        # Import backend classes dynamically
        backend: Backend
        if backend_type == "local":
            from nexus.backends.local import LocalBackend

            backend = LocalBackend(root_path=backend_config["data_dir"])
        elif backend_type == "gcs":
            from nexus.backends.gcs import GCSBackend

            backend = GCSBackend(
                bucket_name=backend_config["bucket"],
                project_id=backend_config.get("project_id"),
                credentials_path=backend_config.get("credentials_path"),
            )
        elif backend_type == "gcs_connector":
            from nexus.backends.gcs_connector import GCSConnectorBackend

            # Get session factory for caching support if available
            session_factory = None
            if hasattr(self, "metadata") and hasattr(self.metadata, "SessionLocal"):
                session_factory = self.metadata.SessionLocal

            backend = GCSConnectorBackend(
                bucket_name=backend_config["bucket"],
                project_id=backend_config.get("project_id"),
                prefix=backend_config.get("prefix", ""),
                credentials_path=backend_config.get("credentials_path"),
                # OAuth access token (alternative to credentials_path)
                access_token=backend_config.get("access_token"),
                # Session factory for caching support
                session_factory=session_factory,
            )
        elif backend_type == "s3_connector":
            from nexus.backends.s3_connector import S3ConnectorBackend

            # Get session factory for caching support if available
            session_factory = None
            if hasattr(self, "metadata") and hasattr(self.metadata, "SessionLocal"):
                session_factory = self.metadata.SessionLocal

            backend = S3ConnectorBackend(
                bucket_name=backend_config["bucket"],
                region_name=backend_config.get("region_name"),
                prefix=backend_config.get("prefix", ""),
                credentials_path=backend_config.get("credentials_path"),
                access_key_id=backend_config.get("access_key_id"),
                secret_access_key=backend_config.get("secret_access_key"),
                session_token=backend_config.get("session_token"),
                # Session factory for caching support
                session_factory=session_factory,
            )
        elif backend_type == "gdrive_connector":
            from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend

            backend = GoogleDriveConnectorBackend(
                token_manager_db=backend_config["token_manager_db"],
                root_folder=backend_config.get("root_folder", "nexus-data"),
                user_email=backend_config.get(
                    "user_email"
                ),  # Optional - uses context.user_id if None
            )
        elif backend_type == "x_connector":
            from nexus.backends.x_connector import XConnectorBackend

            backend = XConnectorBackend(
                token_manager_db=backend_config["token_manager_db"],
                user_email=backend_config.get("user_email"),
                cache_ttl=backend_config.get("cache_ttl"),
                cache_dir=backend_config.get("cache_dir"),
            )
        elif backend_type == "hn_connector":
            from nexus.backends.hn_connector import HNConnectorBackend

            # Get session factory for caching support if available
            hn_session_factory = None
            if hasattr(self, "metadata") and hasattr(self.metadata, "SessionLocal"):
                hn_session_factory = self.metadata.SessionLocal

            backend = HNConnectorBackend(
                cache_ttl=backend_config.get("cache_ttl", 300),
                stories_per_feed=backend_config.get("stories_per_feed", 10),
                include_comments=backend_config.get("include_comments", True),
                session_factory=hn_session_factory,
            )
        elif backend_type == "gmail_connector":
            from nexus.backends.gmail_connector import GmailConnectorBackend

            # Get session factory for caching support if available
            gmail_session_factory = None
            if hasattr(self, "metadata") and hasattr(self.metadata, "SessionLocal"):
                gmail_session_factory = self.metadata.SessionLocal

            backend = GmailConnectorBackend(
                token_manager_db=backend_config["token_manager_db"],
                user_email=backend_config.get("user_email"),
                provider=backend_config.get("provider", "gmail"),
                session_factory=gmail_session_factory,
                max_message_per_label=backend_config.get("max_message_per_label", 2000),
            )
        else:
            raise RuntimeError(f"Unsupported backend type: {backend_type}")

        # Add mount to router
        self.router.add_mount(
            mount_point=mount_point, backend=backend, priority=priority, readonly=readonly
        )

        # Grant direct_owner permission to the user who created the mount
        self._grant_mount_owner_permission(mount_point, context)

        # Generate SKILL.md for connector backends
        if backend_type.endswith("_connector") or backend_type in ("google_drive", "gdrive"):
            self._generate_connector_skill(mount_point, backend_type, context)

        return mount_point  # Return mount_point as the mount ID

    @rpc_expose(description="Remove backend mount")
    def remove_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem.

        This removes the mount from the router and deletes the mount point directory.
        Files inside the mount are NOT deleted - only the directory entry and permissions
        for the mount point itself are cleaned up.

        Args:
            mount_point: Virtual path of mount to remove (e.g., "/personal/alice")
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with removal details:
            - removed: bool - Whether mount was removed
            - directory_deleted: bool - Whether mount point directory was deleted
            - permissions_cleaned: int - Number of permission tuples removed
            - errors: list[str] - Any errors encountered

        Examples:
            >>> # Remove mount and clean up directory
            >>> result = nx.remove_mount("/personal/alice")
            >>> print(f"Removed: {result['removed']}, Dir deleted: {result['directory_deleted']}")
        """
        result: dict[str, Any] = {
            "removed": False,
            "directory_deleted": False,
            "permissions_cleaned": 0,
            "errors": [],
        }

        # Check if mount exists and remove it
        if not self.router.remove_mount(mount_point):
            result["errors"].append(f"Mount not found: {mount_point}")
            return result

        result["removed"] = True
        logger.info(f"Removed mount from router: {mount_point}")

        # Delete the mount point directory (but not the files inside)
        try:
            if hasattr(self, "metadata") and hasattr(self.metadata, "delete"):
                # Soft delete the directory entry from metadata
                self.metadata.delete(mount_point)
                result["directory_deleted"] = True
                logger.info(f"Deleted mount point directory: {mount_point}")
        except Exception as e:
            error_msg = f"Failed to delete mount point directory {mount_point}: {e}"
            result["errors"].append(error_msg)
            logger.warning(error_msg)

        # Clean up ReBAC permissions for the mount point
        try:
            if hasattr(self, "hierarchy_manager") and hasattr(
                self.hierarchy_manager, "remove_parent_tuples"
            ):
                tenant_id = get_tenant_id(context)
                tuples_removed = self.hierarchy_manager.remove_parent_tuples(mount_point, tenant_id)
                result["permissions_cleaned"] += tuples_removed
                logger.info(f"Removed {tuples_removed} parent tuples for {mount_point}")
        except Exception as e:
            error_msg = f"Failed to clean up parent tuples: {e}"
            result["errors"].append(error_msg)
            logger.warning(error_msg)

        # Remove direct_owner permission tuple for the mount point
        try:
            if hasattr(self, "rebac_delete_object_tuples"):
                tenant_id = get_tenant_id(context)
                deleted = self.rebac_delete_object_tuples(
                    object=("file", mount_point), tenant_id=tenant_id
                )
                result["permissions_cleaned"] += deleted
                logger.info(f"Removed {deleted} permission tuples for {mount_point}")
        except Exception as e:
            error_msg = f"Failed to delete permission tuples: {e}"
            result["errors"].append(error_msg)
            logger.warning(error_msg)

        if result["errors"]:
            logger.warning(f"Mount removed with {len(result['errors'])} errors: {result['errors']}")
        else:
            logger.info(
                f"Successfully removed mount {mount_point} "
                f"(directory_deleted={result['directory_deleted']}, permissions_cleaned={result['permissions_cleaned']})"
            )

        return result

    @rpc_expose(description="List available connector types")
    def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
        """List all available connector types that can be used with add_mount().

        Args:
            category: Optional filter by category (storage, api, oauth, database)

        Returns:
            List of connector info dictionaries, each containing:
                - name: Connector identifier (str)
                - description: Human-readable description (str)
                - category: Category for grouping (str)
                - requires: List of optional dependencies (list[str])
                - user_scoped: Whether connector requires per-user OAuth (bool)
        """
        from nexus.backends.registry import ConnectorRegistry

        if category:
            connectors = ConnectorRegistry.list_by_category(category)
        else:
            connectors = ConnectorRegistry.list_all()

        return [
            {
                "name": c.name,
                "description": c.description,
                "category": c.category,
                "requires": c.requires,
                "user_scoped": c.user_scoped,
            }
            for c in connectors
        ]

    @rpc_expose(description="List all backend mounts")
    def list_mounts(self) -> list[dict[str, Any]]:
        """List all active backend mounts.

        Returns:
            List of mount info dictionaries, each containing:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> # List all mounts
            >>> for mount in nx.list_mounts():
            ...     print(f"{mount['mount_point']} (priority={mount['priority']})")
        """
        mounts = []
        for mount_info in self.router.list_mounts():
            mounts.append(
                {
                    "mount_point": mount_info.mount_point,
                    "priority": mount_info.priority,
                    "readonly": mount_info.readonly,
                    "backend_type": type(mount_info.backend).__name__,
                }
            )
        return mounts

    @rpc_expose(description="Get mount details")
    def get_mount(self, mount_point: str) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount (e.g., "/personal/alice")

        Returns:
            Mount info dict if found, None otherwise. Dict contains:
                - mount_point: Virtual path (str)
                - priority: Mount priority (int)
                - readonly: Read-only flag (bool)
                - backend_type: Backend type name (str)

        Examples:
            >>> mount = nx.get_mount("/personal/alice")
            >>> if mount:
            ...     print(f"Priority: {mount['priority']}")
        """
        mount_info = self.router.get_mount(mount_point)
        if mount_info:
            return {
                "mount_point": mount_info.mount_point,
                "priority": mount_info.priority,
                "readonly": mount_info.readonly,
                "backend_type": type(mount_info.backend).__name__,
            }
        return None

    @rpc_expose(description="Check if mount exists")
    def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists at the given path.

        Args:
            mount_point: Virtual path to check (e.g., "/personal/alice")

        Returns:
            True if mount exists, False otherwise

        Examples:
            >>> if nx.has_mount("/personal/alice"):
            ...     print("Alice's mount is active")
        """
        return self.router.has_mount(mount_point)

    @rpc_expose(description="Save mount configuration to database")
    def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        owner_user_id: str | None = None,
        tenant_id: str | None = None,
        description: str | None = None,
        context: OperationContext | None = None,
    ) -> str:
        """Save a mount configuration to the database for persistence.

        This allows mounts to survive server restarts. The mount must still be
        activated using add_mount() - this only stores the configuration.

        Automatically grants direct_owner permission to the user who saves the mount.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type - "local", "gcs", etc.
            backend_config: Backend-specific configuration dict
            priority: Mount priority (default: 0)
            readonly: Whether mount is read-only (default: False)
            owner_user_id: User who owns this mount (optional)
            tenant_id: Tenant ID for multi-tenant isolation (optional)
            description: Human-readable description (optional)
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (UUID string)

        Raises:
            ValueError: If mount already exists at mount_point
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Save personal Google Drive mount configuration
            >>> mount_id = nx.save_mount(
            ...     mount_point="/personal/alice",
            ...     backend_type="google_drive",
            ...     backend_config={"access_token": "ya29.xxx"},
            ...     owner_user_id="google:alice123",
            ...     tenant_id="acme",
            ...     description="Alice's personal Google Drive"
            ... )
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        mount_id = self.mount_manager.save_mount(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            priority=priority,
            readonly=readonly,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            description=description,
        )

        # Grant direct_owner permission to the user who saved the mount
        self._grant_mount_owner_permission(mount_point, context)

        # Generate SKILL.md for connector backends
        if backend_type.endswith("_connector") or backend_type in ("google_drive", "gdrive"):
            self._generate_connector_skill(mount_point, backend_type, context)

        return mount_id

    @rpc_expose(description="List saved mount configurations")
    def list_saved_mounts(
        self, owner_user_id: str | None = None, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        """List mount configurations saved in the database.

        Args:
            owner_user_id: Filter by owner user ID (optional)
            tenant_id: Filter by tenant ID (optional)

        Returns:
            List of saved mount configurations

        Raises:
            RuntimeError: If mount manager is not available

        Examples:
            >>> # List all saved mounts
            >>> mounts = nx.list_saved_mounts()

            >>> # List mounts for specific user
            >>> alice_mounts = nx.list_saved_mounts(owner_user_id="google:alice123")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return self.mount_manager.list_mounts(owner_user_id=owner_user_id, tenant_id=tenant_id)

    @rpc_expose(description="Load and activate saved mount")
    def load_mount(self, mount_point: str) -> str:
        """Load a saved mount configuration and activate it.

        This retrieves the mount configuration from the database and activates it
        by calling add_mount() internally.

        Args:
            mount_point: Virtual path of saved mount to load

        Returns:
            Mount ID if successfully loaded and activated

        Raises:
            ValueError: If mount not found in database
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Load Alice's saved mount
            >>> nx.load_mount("/personal/alice")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        # Get mount config from database
        mount_config = self.mount_manager.get_mount(mount_point)
        if not mount_config:
            raise ValueError(f"Mount not found in database: {mount_point}")

        # Parse backend config from JSON (if it's a string)
        import json

        backend_config = mount_config["backend_config"]
        if isinstance(backend_config, str):
            backend_config = json.loads(backend_config)

        # Normalize token_manager_db for OAuth-backed mounts (gdrive_connector, gmail_connector, x_connector)
        # Use centralized database URL resolution
        backend_type = mount_config["backend_type"]
        if backend_type in ("gdrive_connector", "gmail_connector", "x_connector"):
            try:
                database_url = get_database_url(self)
                backend_config["token_manager_db"] = database_url
            except RuntimeError as e:
                raise RuntimeError(f"Cannot load {backend_type} mount: {e}") from e

        # Activate the mount
        return self.add_mount(
            mount_point=mount_config["mount_point"],
            backend_type=mount_config["backend_type"],
            backend_config=backend_config,
            priority=mount_config["priority"],
            readonly=bool(mount_config["readonly"]),
        )

    @rpc_expose(description="Delete saved mount configuration")
    def delete_saved_mount(self, mount_point: str) -> bool:
        """Delete a saved mount configuration from the database.

        Note: This does NOT deactivate the mount if it's currently active.
        Use remove_mount() to deactivate an active mount.

        Args:
            mount_point: Virtual path of mount to delete

        Returns:
            True if deleted, False if not found

        Raises:
            RuntimeError: If mount manager is not available

        Examples:
            >>> # Remove from database
            >>> nx.delete_saved_mount("/personal/alice")
            >>> # Also deactivate if currently mounted
            >>> nx.remove_mount("/personal/alice")
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return self.mount_manager.remove_mount(mount_point)

    def load_all_saved_mounts(self, auto_sync: bool = False) -> dict[str, Any]:
        """Load all saved mount configurations from database and activate them.

        This method is called during NexusFS initialization to restore all
        persisted mounts from the database. It retrieves all saved mount configs
        via mount_manager.list_mounts() and activates each one.

        Args:
            auto_sync: If True, automatically sync connector backends after loading.
                      If False (default), skip auto-sync for faster server startup.
                      Users can manually call sync_mount() after server is up.

        Returns:
            Dictionary with loading results:
                - loaded: Number of successfully loaded mounts
                - synced: Number of connector mounts that were synced (only if auto_sync=True)
                - failed: Number of mounts that failed to load
                - errors: List of error messages for failed mounts

        Note:
            - mount_manager.list_mounts() returns SAVED mounts from database
            - self.list_mounts() returns ACTIVE mounts from router
            - This method loads saved mounts to make them active
            - auto_sync=False (default) for faster server startup
            - Call sync_mount() manually after startup for large connectors

        Examples:
            >>> # Fast startup (default) - no auto-sync
            >>> result = nx.load_all_saved_mounts()
            >>> print(f"Loaded {result['loaded']} mounts")

            >>> # With auto-sync (slower startup)
            >>> result = nx.load_all_saved_mounts(auto_sync=True)
            >>> print(f"Loaded {result['loaded']} mounts, {result['synced']} synced")

            >>> # Manual sync after startup
            >>> result = nx.load_all_saved_mounts()  # Fast startup
            >>> nx.sync_mount("/mnt/gcs")  # Sync when ready
        """
        if not hasattr(self, "mount_manager") or self.mount_manager is None:
            logger.warning("Mount manager not available, skipping mount restoration")
            return {"loaded": 0, "synced": 0, "failed": 0, "errors": []}

        # Get all saved mounts from database (NOT active mounts)
        saved_mounts = self.mount_manager.list_mounts()

        if not saved_mounts:
            logger.info("No saved mounts found in database")
            return {"loaded": 0, "synced": 0, "failed": 0, "errors": []}

        logger.info(f"Found {len(saved_mounts)} saved mount(s) to load")

        loaded = 0
        failed = 0
        synced = 0
        errors = []

        for mount in saved_mounts:
            mount_point = mount["mount_point"]
            try:
                logger.info(f"Loading mount: {mount_point} ({mount['backend_type']})")

                # Parse backend config from JSON (if it's a string)
                import json

                backend_config = mount["backend_config"]
                if isinstance(backend_config, str):
                    backend_config = json.loads(backend_config)

                # Activate the mount using add_mount
                self.add_mount(
                    mount_point=mount_point,
                    backend_type=mount["backend_type"],
                    backend_config=backend_config,
                    priority=mount["priority"],
                    readonly=bool(mount["readonly"]),
                )

                loaded += 1
                logger.info(f"✓ Successfully loaded mount: {mount_point}")

                # Only auto-sync if explicitly requested (default: False for fast startup)
                if auto_sync:
                    backend_type = mount["backend_type"]
                    if "connector" in backend_type.lower() or backend_type.lower() in ["gcs", "s3"]:
                        try:
                            logger.info(f"Auto-syncing connector mount: {mount_point}")
                            # Create a minimal context from mount owner if available
                            sync_context = None
                            if mount.get("owner_user_id"):
                                from nexus.core.permissions import OperationContext

                                # Parse owner_user_id (format: "user:alice" or "agent:bot")
                                owner_parts = mount["owner_user_id"].split(":", 1)
                                if len(owner_parts) == 2:
                                    subject_type, subject_id = owner_parts
                                else:
                                    subject_type, subject_id = "user", owner_parts[0]
                                sync_context = OperationContext(
                                    user=subject_id,
                                    groups=[],
                                    tenant_id=mount.get("tenant_id", "default"),
                                    subject_type=subject_type,
                                    subject_id=subject_id,
                                )
                                logger.info(
                                    f"Using owner context for sync: {subject_type}:{subject_id}"
                                )
                            sync_result = self.sync_mount(
                                mount_point, recursive=True, dry_run=False, context=sync_context
                            )
                            synced += 1
                            logger.info(
                                f"✓ Synced {mount_point}: "
                                f"{sync_result['files_scanned']} scanned, "
                                f"{sync_result['files_created']} created, "
                                f"{sync_result['files_updated']} updated, "
                                f"{sync_result['files_deleted']} deleted"
                            )
                        except Exception as sync_e:
                            # Log sync error but don't fail the mount
                            logger.warning(f"Failed to sync {mount_point}: {str(sync_e)}")
                else:
                    # Skip auto-sync for faster startup
                    backend_type = mount["backend_type"]
                    if "connector" in backend_type.lower() or backend_type.lower() in ["gcs", "s3"]:
                        logger.info(
                            f"⏭️  Skipping auto-sync for {mount_point} (use sync_mount() to sync manually)"
                        )

            except Exception as e:
                failed += 1
                error_msg = f"Failed to load mount {mount_point}: {str(e)}"
                errors.append(error_msg)
                logger.error(error_msg)
                # Continue loading other mounts even if one fails

        logger.info(f"Mount loading complete: {loaded} loaded, {synced} synced, {failed} failed")

        return {"loaded": loaded, "synced": synced, "failed": failed, "errors": errors}

    def _matches_patterns(
        self, file_path: str, include_patterns: list[str] | None, exclude_patterns: list[str] | None
    ) -> bool:
        """Check if a file path matches include/exclude patterns.

        Args:
            file_path: Virtual file path to check
            include_patterns: Glob patterns to include (None or empty means include all)
            exclude_patterns: Glob patterns to exclude (None or empty means exclude none)

        Returns:
            True if file should be included, False otherwise
        """
        import fnmatch

        # Check include patterns (if specified, file must match at least one)
        if include_patterns and not any(
            fnmatch.fnmatch(file_path, pattern) for pattern in include_patterns
        ):
            return False

        # Check exclude patterns (if file matches any, exclude it)
        return not (
            exclude_patterns
            and any(fnmatch.fnmatch(file_path, pattern) for pattern in exclude_patterns)
        )

    @rpc_expose(description="Sync metadata from connector backend")
    def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s) to Nexus database.

        For connector backends (like gcs_connector), this scans the external storage
        and updates Nexus's metadata database with any files that were added externally
        or existed before Nexus was configured. It also removes files from metadata
        that no longer exist in the backend.

        When sync_content=True (default), also populates the content_cache table
        for fast grep/search operations without hitting the backend.

        Args:
            mount_point: Virtual path of mount to sync (e.g., "/mnt/gcs_demo").
                        If None, syncs ALL connector mounts.
            path: Specific path within mount to sync (e.g., "/reports/2024/")
                  If None, syncs entire mount. Supports file or directory granularity.
            recursive: If True, sync all subdirectories recursively (default: True)
            dry_run: If True, only report what would be synced without making changes (default: False)
            sync_content: If True, also sync content to cache for grep/search (default: True)
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc", ".git/*"])
            generate_embeddings: If True, generate embeddings for semantic search (default: False)
            context: Operation context containing user/subject information (automatically provided by RPC server)
            progress_callback: Optional callback for progress updates (Issue #609).
                              Called with (files_scanned: int, current_path: str).
                              Used by async sync jobs to track progress.

        Returns:
            Dictionary with sync results:
                - files_scanned: Number of files scanned in backend
                - files_created: Number of new files added to database
                - files_updated: Number of existing files updated
                - files_deleted: Number of files deleted from database (no longer in backend)
                - cache_synced: Number of files synced to content cache (if sync_content=True)
                - cache_bytes: Total bytes synced to cache
                - embeddings_generated: Number of embeddings generated (if generate_embeddings=True)
                - errors: List of error messages (if any)

        Raises:
            ValueError: If mount_point doesn't exist
            RuntimeError: If backend doesn't support listing (not a connector backend)

        Examples:
            >>> # Sync entire GCS connector mount (metadata + content)
            >>> result = nx.sync_mount("/mnt/gcs_demo")
            >>> print(f"Created {result['files_created']}, cached {result['cache_synced']}")

            >>> # Sync specific directory
            >>> result = nx.sync_mount("/mnt/gcs", path="/reports/2024/")

            >>> # Sync single file
            >>> result = nx.sync_mount("/mnt/gcs", path="/data/report.pdf")

            >>> # Sync only Python files
            >>> result = nx.sync_mount("/mnt/gcs", include_patterns=["*.py"])

            >>> # Dry run to see what would be synced
            >>> result = nx.sync_mount("/mnt/gcs_demo", dry_run=True)
            >>> print(f"Would scan {result['files_scanned']} files")

            >>> # Sync metadata only (no content cache)
            >>> result = nx.sync_mount("/mnt/gcs", sync_content=False)

            >>> # Sync ALL connector mounts
            >>> result = nx.sync_mount()
            >>> print(f"Synced {result['mounts_synced']} mounts")
        """
        # STEP 1: If no mount_point specified, sync ALL connector mounts
        if mount_point is None:
            return self._sync_all_connector_mounts(
                path=path,
                recursive=recursive,
                dry_run=dry_run,
                sync_content=sync_content,
                include_patterns=include_patterns,
                exclude_patterns=exclude_patterns,
                generate_embeddings=generate_embeddings,
                context=context,
                progress_callback=progress_callback,
            )

        # Initialize statistics upfront for clearer data flow
        stats: dict[str, int | list[str]] = {
            "files_scanned": 0,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
            "cache_synced": 0,
            "cache_bytes": 0,
            "cache_skipped": 0,
            "embeddings_generated": 0,
            "errors": [],
        }

        # Create context object to reduce parameter passing
        ctx = SyncMountContext(
            mount_point=mount_point,
            path=path,
            recursive=recursive,
            dry_run=dry_run,
            sync_content=sync_content,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            generate_embeddings=generate_embeddings,
            context=context,
            progress_callback=progress_callback,
        )

        # STEP 2: Setup and validation
        self._sync_mount_setup(ctx)

        # STEP 3: Scan backend and sync metadata (using iterative directory traversal)
        result = self._sync_mount_metadata(ctx, stats)
        files_found_in_backend = result.files_found_in_backend

        # STEP 4: Handle file deletions
        self._sync_mount_deletions(ctx, files_found_in_backend, stats)

        # STEP 5: Sync content to cache
        self._sync_mount_content_cache(ctx, stats)

        return stats

    def _sync_all_connector_mounts(
        self,
        path: str | None,
        recursive: bool,
        dry_run: bool,
        sync_content: bool,
        include_patterns: list[str] | None,
        exclude_patterns: list[str] | None,
        generate_embeddings: bool,
        context: Any,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Step 1: Sync all connector mounts when mount_point is None.

        Args:
            path: Specific path within mounts to sync
            recursive: Whether to sync recursively
            dry_run: Whether this is a dry run
            sync_content: Whether to sync content to cache
            include_patterns: Patterns to include
            exclude_patterns: Patterns to exclude
            generate_embeddings: Whether to generate embeddings
            context: Operation context
            progress_callback: Optional callback for progress updates

        Returns:
            Dictionary with aggregated sync statistics
        """
        logger.info("[SYNC_MOUNT] No mount_point specified, syncing all connector mounts")
        all_mounts = self.list_mounts()

        # Aggregate results from all mounts
        total_stats: dict[str, Any] = {
            "mounts_synced": 0,
            "mounts_skipped": 0,
            "files_scanned": 0,
            "files_created": 0,
            "files_updated": 0,
            "files_deleted": 0,
            "cache_synced": 0,
            "cache_bytes": 0,
            "embeddings_generated": 0,
            "errors": [],
        }

        for mount_info in all_mounts:
            mp = mount_info.get("mount_point", "")
            backend_type = mount_info.get("backend_type", "")

            # Only sync connector-style backends (those with list_dir support)
            mount = self.router.get_mount(mp)
            if not mount or not hasattr(mount.backend, "list_dir"):
                logger.info(
                    f"[SYNC_MOUNT] Skipping {mp} ({backend_type}) - not a connector backend"
                )
                total_stats["mounts_skipped"] += 1
                continue

            logger.info(f"[SYNC_MOUNT] Syncing mount: {mp}")
            try:
                result = self.sync_mount(
                    mount_point=mp,
                    path=path,
                    recursive=recursive,
                    dry_run=dry_run,
                    sync_content=sync_content,
                    include_patterns=include_patterns,
                    exclude_patterns=exclude_patterns,
                    generate_embeddings=generate_embeddings,
                    context=context,
                    progress_callback=progress_callback,
                )

                # Aggregate stats
                total_stats["mounts_synced"] += 1
                total_stats["files_scanned"] += result.get("files_scanned", 0)
                total_stats["files_created"] += result.get("files_created", 0)
                total_stats["files_updated"] += result.get("files_updated", 0)
                total_stats["files_deleted"] += result.get("files_deleted", 0)
                total_stats["cache_synced"] += result.get("cache_synced", 0)
                total_stats["cache_bytes"] += result.get("cache_bytes", 0)
                total_stats["embeddings_generated"] += result.get("embeddings_generated", 0)

                # Prefix errors with mount point
                for error in result.get("errors", []):
                    total_stats["errors"].append(f"[{mp}] {error}")

            except Exception as e:
                total_stats["errors"].append(f"[{mp}] Failed to sync: {e}")
                logger.warning(f"[SYNC_MOUNT] Failed to sync {mp}: {e}")

        logger.info(
            f"[SYNC_MOUNT] All mounts sync complete: "
            f"{total_stats['mounts_synced']} synced, "
            f"{total_stats['mounts_skipped']} skipped"
        )
        return total_stats

    def _sync_mount_setup(self, ctx: SyncMountContext) -> None:
        """Step 2: Setup and validation for mount sync.

        Extracts context information, checks hierarchy manager status,
        validates mount exists, and ensures mount directory entry exists.
        Populates ctx.backend, ctx.created_by, and ctx.has_hierarchy.

        Args:
            ctx: Sync mount context (modified in-place)

        Raises:
            ValueError: If mount not found
            RuntimeError: If backend doesn't support list_dir
        """
        # Extract created_by from context
        if (
            ctx.context
            and hasattr(ctx.context, "subject_type")
            and hasattr(ctx.context, "subject_id")
        ):
            if ctx.context.subject_id:
                subject_type = ctx.context.subject_type if ctx.context.subject_type else "user"
                ctx.created_by = f"{subject_type}:{ctx.context.subject_id}"
                logger.info(f"[SYNC_MOUNT] Using created_by from context: {ctx.created_by}")
            else:
                logger.warning("[SYNC_MOUNT] Context provided but subject_id is None")
        else:
            logger.warning("[SYNC_MOUNT] No context provided, created_by will be NULL")

        # Check hierarchy manager status
        ctx.has_hierarchy = hasattr(self, "_hierarchy_manager") and self._hierarchy_manager
        enable_inheritance = (
            self._hierarchy_manager.enable_inheritance  # type: ignore[attr-defined]
            if ctx.has_hierarchy
            else False
        )
        logger.info(
            f"[SYNC_MOUNT] Starting sync for {ctx.mount_point}, "
            f"hierarchy_manager={ctx.has_hierarchy}, "
            f"enable_inheritance={enable_inheritance}"
        )

        # Get the mount
        mount = self.router.get_mount(ctx.mount_point)
        if not mount:
            raise ValueError(f"Mount not found: {ctx.mount_point}")

        ctx.backend = mount.backend
        assert ctx.backend is not None, f"Backend for mount {ctx.mount_point} is None"
        backend_name = type(ctx.backend).__name__

        # Check if backend supports list_dir (connector-style backends)
        if not hasattr(ctx.backend, "list_dir"):
            raise RuntimeError(
                f"Backend {backend_name} does not support metadata sync. "
                f"Only connector-style backends (e.g., gcs_connector) can be synced."
            )

        # Ensure mount directory entry exists (backwards compatibility)
        if hasattr(self, "mkdir"):
            try:
                self.mkdir(ctx.mount_point, parents=True, exist_ok=True, context=ctx.context)
                logger.debug(f"[SYNC_MOUNT] Ensured directory entry exists for {ctx.mount_point}")
            except Exception as e:
                logger.warning(
                    f"[SYNC_MOUNT] Failed to create directory entry for {ctx.mount_point}: {e}"
                )

    def _sync_mount_metadata(
        self, ctx: SyncMountContext, stats: dict[str, int | list[str]]
    ) -> MetadataSyncResult:
        """Step 3: Scan backend and sync metadata using ITERATIVE directory traversal.

        This method uses a deque-based BFS approach instead of recursion to avoid
        stack overflow issues with deeply nested directories.

        Applies include/exclude patterns to filter which files get added to metadata,
        ensuring consistency with content cache filtering in Step 5.

        Args:
            ctx: Sync mount context
            stats: Statistics dictionary to update

        Returns:
            MetadataSyncResult with stats and files found in backend
        """
        from collections import deque
        from datetime import UTC, datetime
        from typing import cast

        from nexus.core.metadata import FileMetadata

        # Track all files found in backend (for deletion detection)
        files_found_in_backend: set[str] = set()

        # Determine starting path for scan
        if ctx.path:
            # path can be relative to mount or absolute
            if ctx.path.startswith(ctx.mount_point):
                start_virtual_path = ctx.path
                start_backend_path = ctx.path[len(ctx.mount_point) :].lstrip("/")
            else:
                start_virtual_path = f"{ctx.mount_point.rstrip('/')}/{ctx.path.lstrip('/')}"
                start_backend_path = ctx.path.lstrip("/")

            # Check if this is a single file (not a directory)
            is_single_file = False
            try:
                entries = ctx.backend.list_dir(start_backend_path, context=ctx.context)  # type: ignore[union-attr]
                if not entries:
                    # Empty directory or file - check if path has extension
                    import os.path as osp

                    if osp.splitext(start_backend_path)[1]:
                        is_single_file = True
            except Exception:
                # If list_dir fails, assume it's a file
                is_single_file = True

            if is_single_file:
                # Single file sync - apply pattern filtering using helper
                if not self._matches_patterns(
                    start_virtual_path, ctx.include_patterns, ctx.exclude_patterns
                ):
                    logger.info(
                        f"[SYNC_MOUNT] Skipping {start_virtual_path} - filtered by patterns"
                    )
                    return MetadataSyncResult(stats, files_found_in_backend)

                # Process single file
                logger.info(f"[SYNC_MOUNT] Syncing single file: {start_virtual_path}")
                stats["files_scanned"] = 1
                files_found_in_backend.add(start_virtual_path)

                if not ctx.dry_run:
                    existing_meta = self.metadata.get(start_virtual_path)  # type: ignore[attr-defined]

                    if not existing_meta:
                        try:
                            now = datetime.now(UTC)
                            import hashlib

                            path_hash = hashlib.sha256(start_backend_path.encode()).hexdigest()

                            # Get actual file size from backend
                            file_size = 0
                            try:
                                if hasattr(ctx.backend, "get_content_size"):
                                    from nexus.core.operation_context import OperationContext

                                    size_context = OperationContext(backend_path=start_backend_path)
                                    file_size = ctx.backend.get_content_size(  # type: ignore[union-attr]
                                        path_hash, size_context
                                    )
                            except Exception:
                                # If size retrieval fails, default to 0
                                pass

                            meta = FileMetadata(
                                path=start_virtual_path,
                                backend_name=ctx.backend.name,  # type: ignore[union-attr]
                                physical_path=start_backend_path,
                                size=file_size,
                                etag=path_hash,
                                created_at=now,
                                modified_at=now,
                                version=1,
                                created_by=ctx.created_by,
                            )
                            self.metadata.put(meta)  # type: ignore[attr-defined]
                            stats["files_created"] = 1

                            # Create parent relationships
                            if ctx.has_hierarchy and self._hierarchy_manager:  # type: ignore[attr-defined]
                                try:
                                    self._hierarchy_manager.ensure_parent_tuples(  # type: ignore[attr-defined]
                                        start_virtual_path, tenant_id=None
                                    )
                                except Exception as parent_error:
                                    logger.warning(
                                        f"Failed to create parent tuples for {start_virtual_path}: {parent_error}"
                                    )
                        except Exception as e:
                            error_msg = f"Failed to add {start_virtual_path}: {e}"
                            cast(list[str], stats["errors"]).append(error_msg)
                return MetadataSyncResult(stats, files_found_in_backend)
        else:
            # Start from mount point root
            start_virtual_path = ctx.mount_point
            start_backend_path = ""

        # ITERATIVE directory scanning using BFS with deque
        # Queue contains tuples of (virtual_path, backend_path)
        queue: deque[tuple[str, str]] = deque([(start_virtual_path, start_backend_path)])

        while queue:
            virtual_path, backend_path = queue.popleft()

            try:
                # List entries in this directory
                entries = ctx.backend.list_dir(backend_path, context=ctx.context)  # type: ignore[union-attr]

                for entry_name in entries:
                    is_dir = entry_name.endswith("/")
                    entry_name = entry_name.rstrip("/")

                    # Construct full virtual path
                    if virtual_path == ctx.mount_point:
                        entry_virtual_path = f"{ctx.mount_point}/{entry_name}"
                    else:
                        entry_virtual_path = f"{virtual_path}/{entry_name}"

                    # Construct backend path
                    if backend_path:
                        entry_backend_path = f"{backend_path}/{entry_name}"
                    else:
                        entry_backend_path = entry_name

                    if is_dir:
                        # Add subdirectory to queue for BFS traversal
                        if ctx.recursive:
                            queue.append((entry_virtual_path, entry_backend_path))
                    else:
                        # Apply include/exclude patterns using helper
                        if not self._matches_patterns(
                            entry_virtual_path, ctx.include_patterns, ctx.exclude_patterns
                        ):
                            continue

                        # Process file
                        stats["files_scanned"] = stats["files_scanned"] + 1  # type: ignore[operator]

                        # Call progress callback if provided (Issue #609)
                        if ctx.progress_callback:
                            try:
                                ctx.progress_callback(
                                    int(stats["files_scanned"]),  # type: ignore[arg-type]
                                    entry_virtual_path,
                                )
                            except Exception as cb_error:
                                # Re-raise cancellation, log other errors
                                from nexus.core.sync_job_manager import SyncCancelled

                                if isinstance(cb_error, SyncCancelled):
                                    raise
                                logger.warning(f"Progress callback error: {cb_error}")

                        # Track this file as found in backend
                        files_found_in_backend.add(entry_virtual_path)

                        if ctx.dry_run:
                            # Dry run: just count, don't modify database
                            continue

                        # Check if file already exists in metadata
                        existing_meta = self.metadata.get(entry_virtual_path)  # type: ignore[attr-defined]

                        if not existing_meta:
                            # File doesn't exist in metadata - add it
                            try:
                                now = datetime.now(UTC)
                                import hashlib

                                path_hash = hashlib.sha256(entry_backend_path.encode()).hexdigest()

                                # Get actual file size from backend
                                file_size = 0
                                try:
                                    if hasattr(ctx.backend, "get_content_size"):
                                        from nexus.core.operation_context import OperationContext

                                        size_context = OperationContext(
                                            backend_path=entry_backend_path
                                        )
                                        file_size = ctx.backend.get_content_size(  # type: ignore[union-attr]
                                            path_hash, size_context
                                        )
                                except Exception:
                                    # If size retrieval fails, default to 0
                                    pass

                                meta = FileMetadata(
                                    path=entry_virtual_path,
                                    backend_name=ctx.backend.name,  # type: ignore[union-attr]
                                    physical_path=entry_backend_path,
                                    size=file_size,
                                    etag=path_hash,
                                    created_at=now,
                                    modified_at=now,
                                    version=1,
                                    created_by=ctx.created_by,
                                )

                                # Save to database
                                self.metadata.put(meta)  # type: ignore[attr-defined]
                                stats["files_created"] = stats["files_created"] + 1  # type: ignore[operator]

                                # Create parent relationships for permission inheritance
                                if ctx.has_hierarchy and self._hierarchy_manager:  # type: ignore[attr-defined]
                                    try:
                                        logger.info(
                                            f"[SYNC_MOUNT] Creating parent tuples for new file: {entry_virtual_path}"
                                        )
                                        created = self._hierarchy_manager.ensure_parent_tuples(  # type: ignore[attr-defined]
                                            entry_virtual_path, tenant_id=None
                                        )
                                        logger.info(
                                            f"[SYNC_MOUNT] Created {created} parent tuples for {entry_virtual_path}"
                                        )
                                    except Exception as parent_error:
                                        logger.warning(
                                            f"Failed to create parent tuples for {entry_virtual_path}: {parent_error}"
                                        )

                            except Exception as e:
                                error_msg = f"Failed to add {entry_virtual_path}: {e}"
                                cast(list[str], stats["errors"]).append(error_msg)

            except Exception as e:
                error_msg = f"Failed to scan {virtual_path}: {e}"
                cast(list[str], stats["errors"]).append(error_msg)

        return MetadataSyncResult(stats, files_found_in_backend)

    def _sync_mount_deletions(
        self,
        ctx: SyncMountContext,
        files_found_in_backend: set[str],
        stats: dict[str, int | list[str]],
    ) -> None:
        """Step 4: Handle file deletions - remove files no longer in backend.

        Only performs deletion check when syncing from root (path=None),
        not for partial syncs to avoid false positives.

        Args:
            ctx: Sync mount context
            files_found_in_backend: Set of files found during metadata scan
            stats: Statistics dictionary to update
        """
        from typing import cast

        # Only check deletions if we synced from root (path=None)
        if ctx.dry_run or ctx.path is not None:
            return

        try:
            # Get list of other mount points to exclude from deletion check
            other_mount_points = set()
            try:
                all_mounts = self.list_mounts()
                for m in all_mounts:
                    mp = m.get("mount_point", "")
                    if mp and mp != ctx.mount_point and mp != "/":
                        other_mount_points.add(mp)
            except Exception:
                pass

            # List all files in metadata under this mount point
            existing_metas = self.metadata.list(prefix=ctx.mount_point, recursive=True)  # type: ignore[attr-defined]
            existing_files = [meta.path for meta in existing_metas]

            for existing_path in existing_files:
                # Skip the mount point itself
                if existing_path == ctx.mount_point:
                    continue

                # Skip if it was found in the backend scan
                if existing_path in files_found_in_backend:
                    continue

                # Skip if this file belongs to another mount
                belongs_to_other_mount = any(
                    existing_path.startswith(mp + "/") or existing_path == mp
                    for mp in other_mount_points
                )
                if belongs_to_other_mount:
                    continue

                # Check if this is actually a file
                try:
                    meta = self.metadata.get(existing_path)  # type: ignore[attr-defined]
                    if meta:
                        # File exists in metadata but not in backend - delete it
                        logger.info(
                            f"[SYNC_MOUNT] Deleting file no longer in backend: {existing_path}"
                        )
                        self.metadata.delete(existing_path)  # type: ignore[attr-defined]
                        stats["files_deleted"] = stats["files_deleted"] + 1  # type: ignore[operator]
                except Exception as e:
                    error_msg = f"Failed to delete {existing_path}: {e}"
                    cast(list[str], stats["errors"]).append(error_msg)
                    logger.warning(error_msg)
        except Exception as e:
            error_msg = f"Failed to check for deletions: {e}"
            cast(list[str], stats["errors"]).append(error_msg)
            logger.warning(error_msg)

    def _sync_mount_content_cache(
        self,
        ctx: SyncMountContext,
        stats: dict[str, int | list[str]],
    ) -> None:
        """Step 5: Sync content to cache if requested.

        Delegates to backend.sync() method (from CacheConnectorMixin) to
        populate the content_cache table for fast grep/search operations.

        Args:
            ctx: Sync mount context
            stats: Statistics dictionary to update
        """
        from typing import cast

        if not ctx.sync_content or ctx.dry_run:
            return

        # Delegate to backend's sync() method if available (CacheConnectorMixin)
        if not hasattr(ctx.backend, "sync"):
            logger.info(
                f"[SYNC_MOUNT] Backend {type(ctx.backend).__name__} does not support sync(), "
                "skipping content cache population"
            )
            return

        logger.info("[SYNC_MOUNT] Delegating to backend.sync() for cache population")
        try:
            from nexus.backends.cache_mixin import SyncResult as CacheSyncResult

            # Determine path for cache sync (relative to mount)
            cache_sync_path = None
            if ctx.path:
                if ctx.path.startswith(ctx.mount_point):
                    cache_sync_path = ctx.path[len(ctx.mount_point) :].lstrip("/")
                else:
                    cache_sync_path = ctx.path.lstrip("/")

            cache_result: CacheSyncResult = ctx.backend.sync(  # type: ignore[union-attr]
                path=cache_sync_path,
                mount_point=ctx.mount_point,
                include_patterns=ctx.include_patterns,
                exclude_patterns=ctx.exclude_patterns,
                generate_embeddings=ctx.generate_embeddings,
                context=ctx.context,
            )

            stats["cache_synced"] = cache_result.files_synced
            stats["cache_skipped"] = cache_result.files_skipped
            stats["cache_bytes"] = cache_result.bytes_synced
            stats["embeddings_generated"] = cache_result.embeddings_generated

            # Add any cache sync errors
            if cache_result.errors:
                for error in cache_result.errors:
                    cast(list[str], stats["errors"]).append(f"[cache] {error}")

            logger.info(
                f"[SYNC_MOUNT] Content cache sync complete: "
                f"synced={cache_result.files_synced}, "
                f"bytes={cache_result.bytes_synced}, "
                f"embeddings={cache_result.embeddings_generated}"
            )
        except Exception as e:
            error_msg = f"Failed to sync content cache via backend.sync(): {e}"
            cast(list[str], stats["errors"]).append(error_msg)
            logger.warning(error_msg)

    # =========================================================================
    # Async Sync Methods (Issue #609)
    # =========================================================================

    @rpc_expose(description="Start async sync job for a mount")
    def sync_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount point.

        Unlike sync_mount() which blocks until completion, this method returns
        immediately with a job_id that can be used to monitor progress.

        Args:
            mount_point: Virtual path of mount to sync (required for async)
            path: Specific path within mount to sync
            recursive: If True, sync all subdirectories recursively
            dry_run: If True, only report what would be synced
            sync_content: If True, also sync content to cache
            include_patterns: Glob patterns to include
            exclude_patterns: Glob patterns to exclude
            generate_embeddings: If True, generate embeddings
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with job info:
                - job_id: UUID of the sync job
                - status: Initial status ("pending")
                - mount_point: Mount being synced

        Raises:
            ValueError: If mount_point is None (required for async)

        Example:
            >>> result = nx.sync_mount_async("/mnt/gmail")
            >>> job_id = result["job_id"]
            >>> # Monitor progress
            >>> status = nx.get_sync_job(job_id)
            >>> print(f"Progress: {status['progress_pct']}%")
        """
        import asyncio

        from nexus.core.sync_job_manager import SyncJobManager

        if mount_point is None:
            raise ValueError("mount_point is required for async sync")

        # Get user_id from context
        user_id = None
        if context:
            user_id = getattr(context, "subject_id", None)

        # Build sync params dict
        sync_params = {
            "path": path,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "generate_embeddings": generate_embeddings,
        }

        # Create sync job manager
        sync_manager = SyncJobManager(self.metadata.SessionLocal)  # type: ignore[attr-defined]

        # Create job record
        job_id = sync_manager.create_job(mount_point, sync_params, user_id)

        # Schedule the job to start (non-blocking)
        # Get or create event loop and schedule the task
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(sync_manager.start_job(job_id, self))  # type: ignore[arg-type]
        except RuntimeError:
            # No running loop - create one for the task
            # This handles the case when called from synchronous context
            import threading

            def run_async() -> None:
                asyncio.run(sync_manager.start_job(job_id, self))  # type: ignore[arg-type]

            thread = threading.Thread(target=run_async, daemon=True)
            thread.start()

        return {
            "job_id": job_id,
            "status": "pending",
            "mount_point": mount_point,
        }

    @rpc_expose(description="Get sync job status and progress")
    def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get the status and progress of a sync job.

        Args:
            job_id: UUID of the sync job

        Returns:
            Job details dict or None if not found:
                - id: Job UUID
                - mount_point: Mount being synced
                - status: pending, running, completed, failed, cancelled
                - progress_pct: Progress percentage (0-100)
                - progress_detail: Detailed progress info
                - created_at, started_at, completed_at: Timestamps
                - result: Final sync stats (if completed)
                - error_message: Error details (if failed)

        Example:
            >>> job = nx.get_sync_job("abc123")
            >>> if job:
            ...     print(f"Status: {job['status']}, Progress: {job['progress_pct']}%")
        """
        from nexus.core.sync_job_manager import SyncJobManager

        sync_manager = SyncJobManager(self.metadata.SessionLocal)  # type: ignore[attr-defined]
        return sync_manager.get_job(job_id)

    @rpc_expose(description="Cancel a running sync job")
    def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job.

        Args:
            job_id: UUID of the sync job to cancel

        Returns:
            Dictionary with result:
                - success: True if cancellation was requested
                - job_id: The job ID
                - message: Status message

        Example:
            >>> result = nx.cancel_sync_job("abc123")
            >>> if result["success"]:
            ...     print("Cancellation requested")
        """
        from nexus.core.sync_job_manager import SyncJobManager

        sync_manager = SyncJobManager(self.metadata.SessionLocal)  # type: ignore[attr-defined]
        success = sync_manager.cancel_job(job_id)

        if success:
            return {
                "success": True,
                "job_id": job_id,
                "message": "Cancellation requested",
            }
        else:
            job = sync_manager.get_job(job_id)
            if not job:
                return {
                    "success": False,
                    "job_id": job_id,
                    "message": "Job not found",
                }
            else:
                return {
                    "success": False,
                    "job_id": job_id,
                    "message": f"Cannot cancel job with status: {job['status']}",
                }

    @rpc_expose(description="List sync jobs")
    def list_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters.

        Args:
            mount_point: Filter by mount point
            status: Filter by status (pending, running, completed, failed, cancelled)
            limit: Maximum number of jobs to return (default: 50)

        Returns:
            List of job dicts, ordered by created_at descending

        Example:
            >>> # List all recent jobs
            >>> jobs = nx.list_sync_jobs()
            >>> # List running jobs for a specific mount
            >>> jobs = nx.list_sync_jobs(mount_point="/mnt/gmail", status="running")
        """
        from nexus.core.sync_job_manager import SyncJobManager

        sync_manager = SyncJobManager(self.metadata.SessionLocal)  # type: ignore[attr-defined]
        return sync_manager.list_jobs(mount_point=mount_point, status=status, limit=limit)
