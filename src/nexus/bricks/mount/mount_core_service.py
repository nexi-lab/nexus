"""Mount Core Service - Core mount management operations.

This service handles adding, removing, and listing mounts.
All operations are synchronous and use the Gateway pattern.

Phase 2: Mount Mixin Refactoring
Extracted from: nexus_fs_mounts.py and mount_service.py

All methods are synchronous. FastAPI auto-wraps with to_thread.

Example:
    ```python
    mount_service = MountCoreService(gateway)

    # Add a mount
    mount_id = mount_service.add_mount(
        mount_point="/mnt/gcs",
        backend_type="gcs_connector",
        backend_config={"bucket": "my-bucket"},
    )

    # List mounts with permission filtering
    mounts = mount_service.list_mounts(context=context)
    ```
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.lib.context_utils import get_user_identity, get_zone_id
from nexus.lib.permission_utils import check_permission

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


def _record_error(result: dict, msg: str) -> None:
    """Append an error message to result["errors"] and log a warning."""
    result["errors"].append(msg)
    logger.warning(msg)


class MountCoreService:
    """Core mount management operations (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.

    AI-Friendly Design:
    - All NexusFS access via self._gw
    - Clear method responsibilities
    - No async wrappers
    """

    def __init__(
        self,
        gateway: Any,
        persist_service: Any = None,
        rmdir_fn: Any = None,
        token_manager_fn: Any = None,
    ):
        """Initialize mount core service.

        Args:
            gateway: NexusFSGateway for NexusFS access
            persist_service: MountPersistService for saved config ops
            rmdir_fn: Callback to delete directories (NexusFS.rmdir)
            token_manager_fn: Callback to get token manager for OAuth revocation
        """
        self._gw = gateway
        self._persist_service = persist_service
        self._rmdir_fn = rmdir_fn
        self._token_manager_fn = token_manager_fn

    # =========================================================================
    # Core Mount Operations
    # =========================================================================

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        context: "OperationContext | None" = None,
    ) -> str:
        """Add a dynamic backend mount.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type identifier
            backend_config: Backend-specific configuration
            readonly: Whether mount is read-only
            io_profile: I/O tuning profile (Issue #1413)
            context: Operation context for permissions

        Returns:
            Mount ID (mount_point)

        Raises:
            PermissionError: If user lacks write permission on parent path
            RuntimeError: If backend type is not supported
        """
        import os.path as osp

        # Check permission: user must have write access to parent directory
        parent_path = osp.dirname(mount_point.rstrip("/")) or "/"
        if not self._check_permission(parent_path, "write", context):
            raise PermissionError(
                f"Cannot create mount at {mount_point}: no write permission on {parent_path}"
            )

        # Make a mutable copy of config
        config = backend_config.copy()

        # Auto-inject token_manager_db for OAuth backends
        if self._needs_token_manager_db(backend_type, config):
            try:
                database_url = self._gw.get_database_url()
                config["token_manager_db"] = database_url
            except RuntimeError as e:
                raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e

        # Create backend instance
        backend = self._create_backend(backend_type, config)

        # Add to router (priority removed — router no longer supports it)
        self._gw.router.add_mount(
            mount_point=mount_point,
            backend=backend,
            readonly=readonly,
            io_profile=io_profile,
        )

        # Setup mount point (directory, permissions)
        self._setup_mount_point(mount_point, context)

        return mount_point

    def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Remove a backend mount.

        Args:
            mount_point: Virtual path of mount to remove
            context: Operation context

        Returns:
            Dictionary with removal details

        Raises:
            PermissionError: If user lacks write permission on mount
        """
        # Check permission: user must have write access to mount point
        if not self._check_permission(mount_point, "write", context):
            raise PermissionError(f"Cannot remove mount {mount_point}: no write permission")

        result: dict[str, Any] = {
            "removed": False,
            "directory_deleted": False,
            "permissions_cleaned": 0,
            "errors": [],
        }

        # Remove from router
        if not self._gw.router.remove_mount(mount_point):
            result["errors"].append(f"Mount not found: {mount_point}")
            return result

        result["removed"] = True
        logger.info("Removed mount from router: %s", mount_point)

        # Extract zone_id once for all cleanup operations
        zone_id = get_zone_id(context)

        # Delete all metadata entries (mount point + children)
        try:
            dir_prefix = mount_point if mount_point.endswith("/") else mount_point + "/"
            child_entries = self._gw.metadata_list(dir_prefix)
            paths_to_delete = [entry.path for entry in child_entries] if child_entries else []
            paths_to_delete.append(mount_point)  # Include mount point itself
            self._gw.metadata_delete_batch(paths_to_delete)
            result["files_deleted"] = len(paths_to_delete)
            logger.info("Deleted %d metadata entries for %s", len(paths_to_delete), mount_point)
        except Exception as e:
            _record_error(result, f"Failed to delete metadata entries for {mount_point}: {e}")

        # Clean up sparse directory index entries
        try:
            dir_entries_deleted = self._gw.delete_directory_entries_recursive(mount_point, zone_id)
            result["directory_entries_deleted"] = dir_entries_deleted
            logger.info(
                "Deleted %d directory index entries under %s",
                dir_entries_deleted,
                mount_point,
            )
        except Exception as e:
            _record_error(result, f"Failed to clean up directory index: {e}")

        # Clean up hierarchy tuples
        try:
            removed = self._gw.remove_parent_tuples(mount_point, zone_id)
            result["permissions_cleaned"] += removed
            logger.info("Removed %d parent tuples for %s", removed, mount_point)
        except Exception as e:
            _record_error(result, f"Failed to clean up parent tuples: {e}")

        # Remove permission tuples
        try:
            deleted = self._gw.rebac_delete_object_tuples(
                object=("file", mount_point),
                zone_id=zone_id,
            )
            result["permissions_cleaned"] += deleted
            logger.info("Removed %d permission tuples for %s", deleted, mount_point)
        except Exception as e:
            _record_error(result, f"Failed to delete permission tuples: {e}")

        if result["errors"]:
            logger.warning(
                "Mount removed with %d errors: %s", len(result["errors"]), result["errors"]
            )
        else:
            logger.info(
                "Successfully removed mount %s (directory_deleted=%s, permissions_cleaned=%s)",
                mount_point,
                result["directory_deleted"],
                result["permissions_cleaned"],
            )

        return result

    def list_mounts(
        self,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]:
        """List all active mounts with permission filtering.

        Args:
            context: Operation context for permission checks

        Returns:
            List of mount info dictionaries
        """
        mounts = []

        router_mounts = list(self._gw.router.list_mounts())
        logger.info("[LIST_MOUNTS] Total mounts in router: %d", len(router_mounts))

        for mount_info in router_mounts:
            mount_point = mount_info.mount_point

            # Check permission
            has_permission = self._check_mount_permission(mount_point, context)

            if has_permission:
                mounts.append(
                    {
                        "mount_point": mount_info.mount_point,
                        "readonly": mount_info.readonly,
                        "admin_only": mount_info.admin_only,
                    }
                )

        return mounts

    def get_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount
            context: Operation context for permissions

        Returns:
            Mount info dict or None if not found or no permission
        """
        # Check permission: user must have read access
        if not self._check_permission(mount_point, "read", context):
            return None

        mount_info = self._gw.router.get_mount(mount_point)
        if mount_info:
            return {
                "mount_point": mount_info.mount_point,
                "readonly": mount_info.readonly,
                "admin_only": mount_info.admin_only,
            }
        return None

    def has_mount(self, mount_point: str) -> bool:
        """Check if mount exists.

        Args:
            mount_point: Virtual path to check

        Returns:
            True if mount exists
        """
        return bool(self._gw.router.has_mount(mount_point))

    def list_connectors(self, category: str | None = None) -> list[dict[str, Any]]:
        """List available connector types.

        Args:
            category: Optional filter by category

        Returns:
            List of connector info dictionaries
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

    # =========================================================================
    # Helper Methods
    # =========================================================================

    @staticmethod
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

    def _create_backend(self, backend_type: str, config: dict[str, Any]) -> Any:
        """Create backend instance from type and config.

        Uses BackendFactory with ConnectorRegistry for all registered backends.

        Args:
            backend_type: Backend type identifier
            config: Backend configuration

        Returns:
            Backend instance

        Raises:
            KeyError: If backend type is not registered
        """
        from nexus.backends.factory import BackendFactory

        return BackendFactory.create(backend_type, config, record_store=self._gw.record_store)

    def _setup_mount_point(
        self,
        mount_point: str,
        context: "OperationContext | None",
    ) -> None:
        """Setup mount point with directory and permissions.

        Args:
            mount_point: Virtual path
            context: Operation context
        """
        logger.info("Setting up mount point: %s", mount_point)

        # Create directory entry
        try:
            self._gw.sys_mkdir(mount_point, parents=True, exist_ok=True, context=context)
            logger.info("Created directory entry for mount point: %s", mount_point)
        except Exception as e:
            logger.warning("Failed to create directory entry: %s", e)

        # Grant owner permission
        self._grant_owner_permission(mount_point, context)

    def _grant_owner_permission(
        self,
        mount_point: str,
        context: "OperationContext | None",
    ) -> None:
        """Grant direct_owner permission to mount creator.

        Args:
            mount_point: Virtual path
            context: Operation context
        """
        if not context:
            logger.warning("[MOUNT-PERM] No context, skipping permission grant")
            return

        try:
            zone_id = get_zone_id(context)
            subject_type, subject_id = get_user_identity(context)

            if not subject_id:
                logger.warning("[MOUNT-PERM] No subject_id, skipping permission grant")
                return

            tuple_id = self._gw.rebac_create(
                subject=(subject_type, subject_id),
                relation="direct_owner",
                object=("file", mount_point),
                zone_id=zone_id,
            )

            logger.info(
                "Granted direct_owner to %s:%s for %s (tuple_id=%s)",
                subject_type,
                subject_id,
                mount_point,
                tuple_id,
            )
        except Exception as e:
            logger.warning("Failed to grant permission for %s: %s", mount_point, e)

    def _check_permission(
        self,
        path: str,
        permission: str,
        context: "OperationContext | None",
    ) -> bool:
        """Check if user has permission on path.

        Delegates to shared permission_utils.check_permission.
        Raises PermissionCheckError on infrastructure failures.
        """
        return bool(check_permission(self._gw, path, permission, context))

    def _check_mount_permission(
        self,
        mount_point: str,
        context: "OperationContext | None",
    ) -> bool:
        """Check if user has read permission on mount.

        Args:
            mount_point: Virtual path
            context: Operation context

        Returns:
            True if user has permission
        """
        return self._check_permission(mount_point, "read", context)

    # =========================================================================
    # Connector Lifecycle
    # =========================================================================

    def delete_connector(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Delete a connector completely with bundled operations.

        Combines: deactivate, delete config, optional OAuth revocation, directory cleanup.
        """
        result: dict[str, Any] = {
            "removed": False,
            "directory_deleted": False,
            "config_deleted": False,
            "oauth_revoked": False,
            "errors": [],
            "warnings": [],
        }

        # Step 1: Try to deactivate connector if active (non-fatal)
        try:
            remove_result = self.remove_mount(mount_point, context)
            result["removed"] = remove_result.get("removed", False)
            result["directory_deleted"] = remove_result.get("removed", False)
            if remove_result.get("errors"):
                result["warnings"].extend(remove_result["errors"])
        except PermissionError:
            raise
        except Exception as e:
            result["warnings"].append(f"Failed to deactivate connector (continuing): {e}")

        # Step 2: Delete saved configuration (FATAL - must succeed)
        if self._persist_service is None:
            raise RuntimeError("MountPersistService not available for delete_connector")
        try:
            config_deleted = self._persist_service.delete_saved_mount(mount_point)
            result["config_deleted"] = config_deleted
        except Exception as e:
            error_msg = f"Failed to delete connector configuration: {e}"
            result["errors"].append(error_msg)
            raise RuntimeError(error_msg) from e

        # Step 3: Optionally revoke OAuth credentials
        if revoke_oauth:
            if not provider or not user_email:
                result["warnings"].append(
                    "OAuth revocation requested but provider or user_email not provided"
                )
            elif self._token_manager_fn is not None:
                try:
                    from nexus.lib.context_utils import get_zone_id
                    from nexus.lib.sync_bridge import run_sync

                    zone_id = get_zone_id(context)
                    token_manager = self._token_manager_fn()
                    revoked = run_sync(
                        token_manager.revoke_credential(
                            provider=provider,
                            user_email=user_email,
                            zone_id=zone_id,
                        )
                    )
                    result["oauth_revoked"] = revoked
                except Exception as e:
                    result["warnings"].append(
                        f"Failed to revoke OAuth credentials (non-fatal): {e}"
                    )

        # Step 4: Delete mount point directory
        if self._rmdir_fn is not None:
            try:
                self._rmdir_fn(mount_point, recursive=True, context=context)
                result["directory_deleted"] = True
                logger.info("Deleted mount point directory: %s", mount_point)
            except Exception as e:
                result["warnings"].append(
                    f"Failed to delete mount point directory (non-fatal): {e}"
                )
                logger.warning("Failed to delete mount point directory %s: %s", mount_point, e)

        return result
