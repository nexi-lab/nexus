"""Mount Service — owns all mount management operations.

Operations:
- Dynamic backend mounting/unmounting
- Mount configuration persistence
- Connector discovery and listing
- Metadata synchronization (via SyncService / SyncJobService DI)
"""

import asyncio
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from nexus.lib.context_utils import get_database_url, get_user_identity, get_zone_id
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)


def _needs_token_manager_db(backend_type: str, config: dict[str, Any]) -> bool:
    """Check if backend needs token_manager_db auto-injection."""
    if "token_manager_db" in config:
        return False
    from nexus.backends.registry import ConnectorRegistry

    try:
        info = ConnectorRegistry.get_info(backend_type)
    except KeyError:
        return False
    return info.user_scoped and "token_manager_db" in info.connection_args


# Type alias for progress callback: (files_scanned: int, current_path: str) -> None
ProgressCallback = Callable[[int, str], None]

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.core.nexus_fs import NexusFS as NexusFilesystem
    from nexus.core.router import PathRouter

    from .mount_manager import MountManager


class MountService:
    """Independent mount service extracted from NexusFS.

    Handles all mount management operations:
    - Add/remove dynamic backend mounts
    - List available connectors and active mounts
    - Save/load/delete mount configurations
    - Sync metadata from connector backends

    Architecture:
        - Delegates to PathRouter for mount routing
        - Uses MountManager for persistence
        - Uses sub-services (SyncService, SyncJobService, etc.) for sync ops
        - Requires NexusFS reference only for kernel ops (mkdir, rmdir, rebac)
        - Uses OperationContext for permissions
    """

    def __init__(
        self,
        router: "PathRouter",
        mount_manager: "MountManager | None" = None,
        nexus_fs: "NexusFilesystem | None" = None,
        *,
        sync_service: Any = None,
        sync_job_service: Any = None,
        mount_core_service: Any = None,
        mount_persist_service: Any = None,
        oauth_service: Any = None,
    ):
        """Initialize mount service.

        Args:
            router: Path router for backend resolution
            mount_manager: Optional mount manager for persistence
            nexus_fs: Optional NexusFS instance (for kernel ops: mkdir, rmdir, rebac)
            sync_service: SyncService for metadata sync operations
            sync_job_service: SyncJobService for async sync job management
            mount_core_service: MountCoreService for internal mount operations
            mount_persist_service: MountPersistService for config persistence
            oauth_service: OAuthService for credential revocation
        """
        self.router = router
        self.mount_manager = mount_manager
        self.nexus_fs = nexus_fs
        self._sync_service = sync_service
        self._sync_job_service = sync_job_service
        self._mount_core_service = mount_core_service
        self._mount_persist_service = mount_persist_service
        self._oauth_service = oauth_service

        logger.info("[MountService] Initialized")

    # =========================================================================
    # Public API: Core Mount Management
    # =========================================================================

    @rpc_expose(description="Add dynamic backend mount")
    async def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        context: "OperationContext | None" = None,
    ) -> str:
        """Add a dynamic backend mount to the filesystem.

        This adds a backend mount at runtime without requiring server restart.
        Useful for user-specific storage, temporary backends, or multi-zone scenarios.

        Automatically grants direct_owner permission to the user who creates the mount.

        Args:
            mount_point: Virtual path where backend is mounted (e.g., "/personal/alice")
            backend_type: Backend type - "local", "gcs", "gcs_connector", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            readonly: Whether mount is read-only (default: False)
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            ValueError: If mount_point already exists or configuration is invalid
            RuntimeError: If backend type is not supported

        Examples:
            # Add personal GCS mount (CAS-based)
            mount_id = await service.add_mount(
                mount_point="/personal/alice",
                backend_type="gcs",
                backend_config={
                    "bucket": "alice-personal-bucket",
                    "project_id": "my-project"
                },
            )

            # Add GCS connector mount (direct path mapping for external buckets)
            mount_id = await service.add_mount(
                mount_point="/workspace/gdrive",
                backend_type="gcs_connector",
                backend_config={
                    "bucket": "my-external-bucket",
                    "project_id": "my-project",
                    "prefix": "workspace"  # Optional prefix in bucket
                }
            )
        """

        # Run backend instantiation in thread pool to avoid blocking
        def _add_mount_sync() -> str:
            # Make a mutable copy of backend_config to avoid modifying the original
            config = backend_config.copy()

            # Auto-inject token_manager_db for OAuth-backed connectors
            if _needs_token_manager_db(backend_type, config):
                # Use centralized database URL resolution
                if self.nexus_fs:
                    try:
                        database_url = get_database_url(self.nexus_fs)
                        config = {**config, "token_manager_db": database_url}
                    except RuntimeError as e:
                        raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e
                else:
                    raise RuntimeError(
                        f"Cannot create {backend_type} mount: nexus_fs not configured"
                    )

            # Create backend via centralized factory
            from nexus.backends.factory import BackendFactory

            # Get record store for caching support if available
            record_store = None
            if self.nexus_fs and hasattr(self.nexus_fs, "_record_store"):
                record_store = self.nexus_fs._record_store

            backend = BackendFactory.create(backend_type, config, record_store=record_store)

            # Add mount to router
            self.router.add_mount(
                mount_point=mount_point,
                backend=backend,
                readonly=readonly,
                io_profile=io_profile,
            )

            # Grant direct_owner permission to the user who created the mount
            self._grant_mount_owner_permission(mount_point, context)

            # Generate SKILL.md for connector backends
            if backend_type.endswith("_connector") or backend_type in ("google_drive", "gdrive"):
                self._generate_connector_skill(mount_point, backend_type, context)

            return mount_point  # Return mount_point as the mount ID

        # Run in thread pool to avoid blocking event loop
        return await asyncio.to_thread(_add_mount_sync)

    @rpc_expose(description="Remove backend mount")
    async def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
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
            # Remove mount and clean up directory
            result = await service.remove_mount("/personal/alice")
            print(f"Removed: {result['removed']}, Dir deleted: {result['directory_deleted']}")
        """

        def _remove_mount_sync() -> dict[str, Any]:
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
            if self.nexus_fs and hasattr(self.nexus_fs, "metadata"):
                try:
                    if hasattr(self.nexus_fs.metadata, "delete"):
                        # Soft delete the directory entry from metadata
                        self.nexus_fs.metadata.delete(mount_point)
                        result["directory_deleted"] = True
                        logger.info(f"Deleted mount point directory: {mount_point}")
                except Exception as e:
                    error_msg = f"Failed to delete mount point directory {mount_point}: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)

            # Clean up ReBAC permissions for the mount point
            if self.nexus_fs and hasattr(self.nexus_fs, "hierarchy_manager"):
                try:
                    if hasattr(self.nexus_fs.hierarchy_manager, "remove_parent_tuples"):
                        zone_id = get_zone_id(context)
                        tuples_removed = self.nexus_fs.hierarchy_manager.remove_parent_tuples(
                            mount_point, zone_id
                        )
                        result["permissions_cleaned"] += tuples_removed
                        logger.info(f"Removed {tuples_removed} parent tuples for {mount_point}")
                except Exception as e:
                    error_msg = f"Failed to clean up parent tuples: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)

            # Remove direct_owner permission tuple for the mount point
            if self.nexus_fs and hasattr(self.nexus_fs, "rebac_service"):
                try:
                    zone_id = get_zone_id(context)
                    svc = self.nexus_fs.rebac_service
                    tuples = svc.rebac_list_tuples_sync(object=("file", mount_point))
                    deleted = 0
                    for t in tuples:
                        tid = t.get("tuple_id")
                        if tid and svc.rebac_delete_sync(tid):
                            deleted += 1
                    result["permissions_cleaned"] += deleted
                    logger.info(f"Removed {deleted} permission tuples for {mount_point}")
                except Exception as e:
                    error_msg = f"Failed to delete permission tuples: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)

            if result["errors"]:
                logger.warning(
                    f"Mount removed with {len(result['errors'])} errors: {result['errors']}"
                )
            else:
                logger.info(
                    f"Successfully removed mount {mount_point} "
                    f"(directory_deleted={result['directory_deleted']}, "
                    f"permissions_cleaned={result['permissions_cleaned']})"
                )

            return result

        return await asyncio.to_thread(_remove_mount_sync)

    @rpc_expose(description="Delete connector with bundled cleanup")
    async def delete_connector(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Delete a connector completely with bundled operations.

        Combines: deactivate mount, delete saved config, optional OAuth
        credential revocation, and directory cleanup.

        Args:
            mount_point: Virtual path of connector mount to delete
            revoke_oauth: If True, also revoke associated OAuth credentials
            provider: OAuth provider name (required if revoke_oauth=True)
            user_email: User email for OAuth revocation (required if revoke_oauth=True)
            context: Operation context for permission checks

        Returns:
            Dict with removal details including removed, config_deleted,
            oauth_revoked, errors, and warnings lists.

        Raises:
            RuntimeError: If mount_core_service or mount_persist_service not configured
        """

        def _delete_connector_sync() -> dict[str, Any]:
            result: dict[str, Any] = {
                "removed": False,
                "directory_deleted": False,
                "config_deleted": False,
                "oauth_revoked": False,
                "errors": [],
                "warnings": [],
            }

            # Step 1: Try to deactivate connector if active (non-fatal)
            if self._mount_core_service is not None:
                try:
                    remove_result = self._mount_core_service.remove_mount(mount_point, context)
                    result["removed"] = remove_result.get("removed", False)
                    result["directory_deleted"] = remove_result.get("removed", False)
                    if remove_result.get("errors"):
                        result["warnings"].extend(remove_result["errors"])
                except PermissionError:
                    raise
                except Exception as e:
                    result["warnings"].append(f"Failed to deactivate connector (continuing): {e}")
            else:
                result["warnings"].append("mount_core_service not available, skipping deactivation")

            # Step 2: Delete saved configuration (FATAL - must succeed)
            if self._mount_persist_service is not None:
                try:
                    config_deleted = self._mount_persist_service.delete_saved_mount(mount_point)
                    result["config_deleted"] = config_deleted
                except Exception as e:
                    error_msg = f"Failed to delete connector configuration: {e}"
                    result["errors"].append(error_msg)
                    raise RuntimeError(error_msg) from e
            else:
                error_msg = "mount_persist_service not available, cannot delete configuration"
                result["errors"].append(error_msg)
                raise RuntimeError(error_msg)

            # Step 3: Optionally revoke OAuth credentials (handled async below)

            # Step 4: Delete mount point directory
            if self.nexus_fs and hasattr(self.nexus_fs, "rmdir"):
                try:
                    _nx: Any = self.nexus_fs
                    _nx.sys_rmdir(mount_point, recursive=True, context=context)
                    result["directory_deleted"] = True
                    logger.info(f"Deleted mount point directory: {mount_point}")
                except Exception as e:
                    result["warnings"].append(
                        f"Failed to delete mount point directory (non-fatal): {e}"
                    )
                    logger.warning(f"Failed to delete mount point directory {mount_point}: {e}")

            return result

        result = await asyncio.to_thread(_delete_connector_sync)

        # Step 3: Optionally revoke OAuth credentials (async)
        if revoke_oauth:
            if not provider or not user_email:
                result["warnings"].append(
                    "OAuth revocation requested but provider or user_email not provided"
                )
            elif self._oauth_service is not None:
                try:
                    revoke_result = await self._oauth_service.oauth_revoke_credential(
                        provider=provider,
                        user_email=user_email,
                        context=context,
                    )
                    result["oauth_revoked"] = revoke_result.get("success", False)
                except Exception as e:
                    result["warnings"].append(
                        f"Failed to revoke OAuth credentials (non-fatal): {e}"
                    )
            else:
                result["warnings"].append(
                    "OAuth revocation requested but oauth_service not available"
                )

        return result

    @rpc_expose(description="List available connector types")
    async def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
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

        def _list_connectors_sync() -> list[dict[str, Any]]:
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

        return await asyncio.to_thread(_list_connectors_sync)

    @rpc_expose(description="List all backend mounts")
    async def list_mounts(self, context: "OperationContext | None" = None) -> list[dict[str, Any]]:
        """List all active backend mounts that the user has permission to access.

        Automatically filters mounts based on the user's permissions. Only mounts
        where the user has read access (viewer or direct_owner) are returned.

        Args:
            context: Operation context (automatically provided by RPC server)

        Returns:
            List of mount info dictionaries, each containing:
                - mount_point: Virtual path (str)
                - readonly: Read-only flag (bool)
                - admin_only: Admin-only flag (bool)

        Examples:
            # List all mounts I have access to
            for mount in await service.list_mounts():
                print(f"{mount['mount_point']} (readonly={mount['readonly']})")
        """

        def _list_mounts_sync() -> list[dict[str, Any]]:
            mounts = []

            # Log context details for debugging
            logger.info(f"[LIST_MOUNTS] Called with context: {context}")
            if context:
                logger.info(f"[LIST_MOUNTS] Context type: {type(context)}")
                subject_type, subject_id = get_user_identity(context)
                zone_id = get_zone_id(context)
                logger.info(
                    f"[LIST_MOUNTS] Extracted: subject={subject_type}:{subject_id}, zone={zone_id}"
                )

            router_mounts = list(self.router.list_mounts())
            logger.info(f"[LIST_MOUNTS] Total mounts in router: {len(router_mounts)}")

            for mount_info in router_mounts:
                # Filter by permission - only include mounts the user can access
                mount_point = mount_info.mount_point
                logger.info(f"[LIST_MOUNTS] Checking mount: {mount_point}")

                # Check if user has permission to access this mount
                has_permission = False
                if context and self.nexus_fs and hasattr(self.nexus_fs, "rebac_check"):
                    try:
                        subject_type, subject_id = get_user_identity(context)
                        zone_id = get_zone_id(context)

                        logger.info(
                            f"[LIST_MOUNTS] Checking permission for {subject_type}:{subject_id} "
                            f"on {mount_point} (zone={zone_id})"
                        )

                        # Admin users can see all mounts
                        is_admin = getattr(context, "is_admin", False)
                        if is_admin:
                            has_permission = True
                            logger.info(
                                f"[LIST_MOUNTS] Admin user {subject_type}:{subject_id} - "
                                f"granting access to {mount_point}"
                            )
                        elif subject_id:
                            # Check if user has read permission (includes owner, editor, viewer)
                            has_permission = self.nexus_fs.rebac_service.rebac_check_sync(
                                subject=(subject_type, subject_id),
                                permission="read",
                                object=("file", mount_point),
                                zone_id=zone_id,
                            )
                            logger.info(
                                f"[LIST_MOUNTS] Permission check result for "
                                f"{subject_type}:{subject_id} on {mount_point}: {has_permission}"
                            )
                        else:
                            logger.warning(
                                f"[LIST_MOUNTS] No subject_id in context for {mount_point}"
                            )
                    except Exception as e:
                        # If permission check fails, exclude this mount for safety
                        logger.error(
                            f"[LIST_MOUNTS] Permission check failed for {mount_point}: {e}",
                            exc_info=True,
                        )
                        has_permission = False
                else:
                    # No context or no ReBAC configured — include all mounts
                    logger.info(
                        f"[LIST_MOUNTS] No context or no rebac_check - allowing {mount_point}"
                    )
                    has_permission = True

                # Only include mounts the user has permission to access
                if has_permission:
                    mounts.append(
                        {
                            "mount_point": mount_info.mount_point,
                            "readonly": mount_info.readonly,
                            "admin_only": mount_info.admin_only,
                        }
                    )

            return mounts

        return await asyncio.to_thread(_list_mounts_sync)

    @rpc_expose(description="Get mount details")
    async def get_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,  # noqa: ARG002 - Protocol compliance
    ) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount (e.g., "/personal/alice")

        Returns:
            Mount info dict if found, None otherwise. Dict contains:
                - mount_point: Virtual path (str)
                - readonly: Read-only flag (bool)
                - admin_only: Admin-only flag (bool)

        Examples:
            mount = await service.get_mount("/personal/alice")
            if mount:
                print(f"Readonly: {mount['readonly']}")
        """

        def _get_mount_sync() -> dict[str, Any] | None:
            mount_info = self.router.get_mount(mount_point)
            if mount_info:
                return {
                    "mount_point": mount_info.mount_point,
                    "readonly": mount_info.readonly,
                    "admin_only": mount_info.admin_only,
                }
            return None

        return await asyncio.to_thread(_get_mount_sync)

    @rpc_expose(description="Check if mount exists")
    async def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists at the given path.

        Args:
            mount_point: Virtual path to check (e.g., "/personal/alice")

        Returns:
            True if mount exists, False otherwise

        Examples:
            if await service.has_mount("/personal/alice"):
                print("Alice's mount is active")
        """
        return await asyncio.to_thread(self.router.has_mount, mount_point)

    # =========================================================================
    # Public API: Persisted Mount Configuration
    # =========================================================================

    @rpc_expose(description="Save mount configuration to database")
    async def save_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        context: "OperationContext | None" = None,
    ) -> str:
        """Save a mount configuration to the database for persistence.

        This allows mounts to survive server restarts. The mount must still be
        activated using add_mount() - this only stores the configuration.

        Automatically grants direct_owner permission to the user who saves the mount.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type - "local", "gcs", etc.
            backend_config: Backend-specific configuration dict
            readonly: Whether mount is read-only (default: False)
            owner_user_id: User who owns this mount (optional)
            zone_id: Zone ID for multi-zone isolation (optional)
            description: Human-readable description (optional)
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (UUID string)

        Raises:
            ValueError: If mount already exists at mount_point
            RuntimeError: If mount manager is not available

        Examples:
            # Save personal Google Drive mount configuration
            mount_id = await service.save_mount(
                mount_point="/personal/alice",
                backend_type="google_drive",
                backend_config={"access_token": "ya29.xxx"},
                owner_user_id="google:alice123",
                zone_id="acme",
                description="Alice's personal Google Drive"
            )
        """
        if not self.mount_manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        def _save_mount_sync() -> str:
            # Auto-populate owner_user_id and zone_id from context if not provided
            nonlocal owner_user_id, zone_id

            if owner_user_id is None and context:
                subject_type, subject_id = get_user_identity(context)
                if subject_id:
                    owner_user_id = f"{subject_type}:{subject_id}"
                    logger.info(f"[SAVE_MOUNT] Auto-populated owner_user_id: {owner_user_id}")

            if zone_id is None and context:
                zone_id = get_zone_id(context)
                if zone_id:
                    logger.info(f"[SAVE_MOUNT] Auto-populated zone_id: {zone_id}")

            assert self.mount_manager is not None
            mount_id = self.mount_manager.save_mount(
                mount_point=mount_point,
                backend_type=backend_type,
                backend_config=backend_config,
                readonly=readonly,
                io_profile=io_profile,
                owner_user_id=owner_user_id,
                zone_id=zone_id,
                description=description,
            )

            # Grant direct_owner permission to the user who saved the mount
            self._grant_mount_owner_permission(mount_point, context)

            # Generate SKILL.md for connector backends
            if backend_type.endswith("_connector") or backend_type in ("google_drive", "gdrive"):
                self._generate_connector_skill(mount_point, backend_type, context)

            return mount_id

        return await asyncio.to_thread(_save_mount_sync)

    @rpc_expose(description="List saved mount configurations")
    async def list_saved_mounts(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]:
        """List mount configurations saved in the database.

        Automatically filters by the current user's context (subject_id and zone_id)
        unless explicit filter parameters are provided. This ensures users can only
        see their own mounts and mounts from their zone.

        Args:
            owner_user_id: Filter by owner user ID (optional, defaults to current user)
            zone_id: Filter by zone ID (optional, defaults to current zone)
            context: Operation context (automatically provided by RPC server)

        Returns:
            List of saved mount configurations owned by the user or in their zone

        Raises:
            RuntimeError: If mount manager is not available

        Examples:
            # List my saved mounts (automatically filtered)
            mounts = await service.list_saved_mounts()

            # List all mounts in my zone
            zone_mounts = await service.list_saved_mounts(owner_user_id=None)
        """
        if not self.mount_manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        def _list_saved_mounts_sync() -> list[dict[str, Any]]:
            # Auto-populate filters from context if not explicitly provided
            nonlocal owner_user_id, zone_id

            if owner_user_id is None and context:
                subject_type, subject_id = get_user_identity(context)
                if subject_id:
                    owner_user_id = f"{subject_type}:{subject_id}"
                    logger.info(f"[LIST_SAVED_MOUNTS] Auto-filtering by owner: {owner_user_id}")

            if zone_id is None and context:
                zone_id = get_zone_id(context)
                if zone_id:
                    logger.info(f"[LIST_SAVED_MOUNTS] Auto-filtering by zone: {zone_id}")

            assert self.mount_manager is not None
            return self.mount_manager.list_mounts(owner_user_id=owner_user_id, zone_id=zone_id)

        return await asyncio.to_thread(_list_saved_mounts_sync)

    @rpc_expose(description="Load and activate saved mount")
    async def load_mount(self, mount_point: str) -> str:
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
            # Load Alice's saved mount
            await service.load_mount("/personal/alice")
        """
        if not self.mount_manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        # Get mount config from database
        mount_config = await asyncio.to_thread(self.mount_manager.get_mount, mount_point)
        if not mount_config:
            raise ValueError(f"Mount not found in database: {mount_point}")

        # Parse backend config from JSON (if it's a string)
        backend_config = mount_config["backend_config"]
        if isinstance(backend_config, str):
            backend_config = json.loads(backend_config)

        # Normalize token_manager_db for OAuth-backed mounts
        backend_type = mount_config["backend_type"]
        if _needs_token_manager_db(backend_type, backend_config):
            if self.nexus_fs:
                try:
                    database_url = get_database_url(self.nexus_fs)
                    backend_config["token_manager_db"] = database_url
                except RuntimeError as e:
                    raise RuntimeError(f"Cannot load {backend_type} mount: {e}") from e
            else:
                raise RuntimeError(f"Cannot load {backend_type} mount: nexus_fs not configured")

        # Activate the mount
        return await self.add_mount(
            mount_point=mount_config["mount_point"],
            backend_type=mount_config["backend_type"],
            backend_config=backend_config,
            readonly=bool(mount_config["readonly"]),
        )

    @rpc_expose(description="Delete saved mount configuration")
    async def delete_saved_mount(self, mount_point: str) -> bool:
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
            # Remove from database
            await service.delete_saved_mount("/personal/alice")
            # Also deactivate if currently mounted
            await service.remove_mount("/personal/alice")
        """
        if not self.mount_manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return await asyncio.to_thread(self.mount_manager.remove_mount, mount_point)

    # =========================================================================
    # Public API: Metadata Synchronization
    # (These methods delegate to NexusFS for full implementation)
    # =========================================================================

    @rpc_expose(description="Sync metadata from connector backend")
    async def sync_mount(
        self,
        mount_point: str | None = None,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: "OperationContext | None" = None,
        progress_callback: ProgressCallback | None = None,
        full_sync: bool = False,
    ) -> dict[str, Any]:
        """Sync metadata and content from connector backend(s) to Nexus database.

        For connector backends (like gcs_connector), this scans the external storage
        and updates Nexus's metadata database with any files that were added externally
        or existed before Nexus was configured.

        Args:
            mount_point: Virtual path of mount to sync (None = sync all mounts)
            path: Specific path within mount to sync (None = entire mount)
            recursive: If True, sync all subdirectories recursively
            dry_run: If True, show what would be synced without changes
            sync_content: If True, also sync content to cache
            include_patterns: Glob patterns to include (e.g., ["*.py", "*.md"])
            exclude_patterns: Glob patterns to exclude (e.g., ["*.pyc"])
            generate_embeddings: If True, generate embeddings for semantic search
            context: Operation context
            progress_callback: Optional callback for progress updates
            full_sync: If True, perform a full resync

        Returns:
            Dictionary with sync results

        Raises:
            RuntimeError: If sync_service not configured
        """
        if self._sync_service is None:
            raise RuntimeError(
                "sync_mount requires sync_service. Pass sync_service to MountService.__init__"
            )

        def _sync_mount_sync() -> dict[str, Any]:
            from nexus.contracts.types import SyncContext

            ctx = SyncContext(
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
                full_sync=full_sync,
            )

            result = self._sync_service.sync_mount(ctx)
            return cast(dict[str, Any], result.to_dict())

        return await asyncio.to_thread(_sync_mount_sync)

    @rpc_expose(description="Start async sync job for a mount")
    async def sync_mount_async(
        self,
        mount_point: str,
        path: str | None = None,
        recursive: bool = True,
        dry_run: bool = False,
        sync_content: bool = True,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        generate_embeddings: bool = False,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Start an async sync job for a mount point.

        Creates a background sync job and starts it immediately.

        Args:
            mount_point: Virtual path of mount to sync
            path: Specific path within mount to sync
            recursive: If True, sync all subdirectories recursively
            dry_run: If True, only report what would be synced
            sync_content: If True, also sync content to cache
            include_patterns: Glob patterns to include
            exclude_patterns: Glob patterns to exclude
            generate_embeddings: If True, generate embeddings
            context: Operation context

        Returns:
            Dictionary with job info (job_id, status, mount_point)

        Raises:
            RuntimeError: If sync_job_service not configured
            ValueError: If mount_point is None
        """
        if self._sync_job_service is None:
            raise RuntimeError(
                "sync_mount_async requires sync_job_service. "
                "Pass sync_job_service to MountService.__init__"
            )

        if mount_point is None:
            raise ValueError("mount_point is required for async sync")

        user_id = None
        if context:
            user_id = getattr(context, "subject_id", None)

        params = {
            "path": path,
            "recursive": recursive,
            "dry_run": dry_run,
            "sync_content": sync_content,
            "include_patterns": include_patterns,
            "exclude_patterns": exclude_patterns,
            "generate_embeddings": generate_embeddings,
        }

        def _start_async_sync() -> dict[str, Any]:
            job_id = self._sync_job_service.create_job(mount_point, params, user_id)
            self._sync_job_service.start_job(job_id)
            return {
                "job_id": job_id,
                "status": "pending",
                "mount_point": mount_point,
            }

        return await asyncio.to_thread(_start_async_sync)

    @rpc_expose(description="Get sync job status and progress")
    async def get_sync_job(self, job_id: str) -> dict[str, Any] | None:
        """Get the status and progress of a sync job.

        Args:
            job_id: UUID of the sync job

        Returns:
            Job details dict or None if not found

        Raises:
            RuntimeError: If sync_job_service not configured
        """
        if self._sync_job_service is None:
            raise RuntimeError(
                "get_sync_job requires sync_job_service. "
                "Pass sync_job_service to MountService.__init__"
            )

        return await asyncio.to_thread(self._sync_job_service.get_job, job_id)

    @rpc_expose(description="Cancel a running sync job")
    async def cancel_sync_job(self, job_id: str) -> dict[str, Any]:
        """Cancel a running sync job.

        Args:
            job_id: UUID of the sync job to cancel

        Returns:
            Dictionary with result (success, job_id, message)

        Raises:
            RuntimeError: If sync_job_service not configured
        """
        if self._sync_job_service is None:
            raise RuntimeError(
                "cancel_sync_job requires sync_job_service. "
                "Pass sync_job_service to MountService.__init__"
            )

        def _cancel_sync_job_sync() -> dict[str, Any]:
            success = self._sync_job_service.cancel_job(job_id)

            if success:
                return {"success": True, "job_id": job_id, "message": "Cancellation requested"}

            job = self._sync_job_service.get_job(job_id)
            if not job:
                return {"success": False, "job_id": job_id, "message": "Job not found"}
            return {
                "success": False,
                "job_id": job_id,
                "message": f"Cannot cancel job with status: {job['status']}",
            }

        return await asyncio.to_thread(_cancel_sync_job_sync)

    @rpc_expose(description="List sync jobs")
    async def list_sync_jobs(
        self,
        mount_point: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List sync jobs with optional filters.

        Args:
            mount_point: Filter by mount point
            status: Filter by status
            limit: Maximum number of jobs to return

        Returns:
            List of job info dictionaries

        Raises:
            RuntimeError: If sync_job_service not configured
        """
        if self._sync_job_service is None:
            raise RuntimeError(
                "list_sync_jobs requires sync_job_service. "
                "Pass sync_job_service to MountService.__init__"
            )

        return await asyncio.to_thread(
            self._sync_job_service.list_jobs,
            mount_point=mount_point,
            status=status,
            limit=limit,
        )

    # =========================================================================
    # Helper Methods
    # =========================================================================

    def _grant_mount_owner_permission(
        self, mount_point: str, context: "OperationContext | None"
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
        if self.nexus_fs and hasattr(self.nexus_fs, "mkdir"):
            try:
                self.nexus_fs.sys_mkdir(mount_point, parents=True, exist_ok=True)
                logger.info(f"✓ Created directory entry for mount point: {mount_point}")
            except Exception as e:
                logger.warning(f"Failed to create directory entry for mount {mount_point}: {e}")

        # Grant direct_owner permission to the creating user
        if context:
            subject_type, subject_id = get_user_identity(context)
            zone_id = get_zone_id(context)

            if subject_id and self.nexus_fs and hasattr(self.nexus_fs, "rebac_service"):
                try:
                    self.nexus_fs.rebac_service.rebac_create_sync(
                        subject=(subject_type, subject_id),
                        relation="direct_owner",
                        object=("file", mount_point),
                        zone_id=zone_id,
                    )
                    logger.info(
                        f"✓ Granted direct_owner to {subject_type}:{subject_id} for {mount_point}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to grant direct_owner for {mount_point}: {type(e).__name__}: {e}"
                    )
            else:
                logger.warning(
                    "[MOUNT-PERMISSION] No subject_id in context or rebac_add_tuple not available, "
                    "skipping permission grant"
                )
        else:
            logger.warning(
                "[MOUNT-PERMISSION] No context provided, skipping permission grant for mount point"
            )

    def _generate_connector_skill(
        self, mount_point: str, backend_type: str, context: "OperationContext | None"
    ) -> bool:
        """Generate SKILL.md for a connector mount.

        Creates a skill file that documents the connector backend for LLMs.

        Args:
            mount_point: The virtual path of the mount
            backend_type: Backend type identifier
            context: Operation context

        Returns:
            True if skill was generated successfully
        """
        if not self.nexus_fs or not hasattr(self.nexus_fs, "write"):
            logger.warning("[CONNECTOR-SKILL] NexusFS not available, skipping skill generation")
            return False

        try:
            # Generate skill content
            skill_content = f"""# {mount_point} Connector

Backend Type: {backend_type}
Mount Point: {mount_point}

## Overview
This is a connector mount that provides access to external resources through Nexus.

## Capabilities
- Read files from external backend
- List directory contents
- Sync metadata with Nexus database

## Usage
Files in this mount are accessible through standard Nexus file operations.
Use sync_mount() to refresh metadata from the backend.
"""

            skill_path = f"{mount_point}/SKILL.md"
            self.nexus_fs.sys_write(skill_path, skill_content.encode("utf-8"), context=context)
            logger.info(f"✓ Generated SKILL.md for connector mount: {skill_path}")
            return True
        except Exception as e:
            logger.warning(f"Failed to generate SKILL.md for {mount_point}: {e}")
            return False
