"""Mount Service — unified mount management operations.

Owns both the sync core logic (add/remove/list/get/has mount) and
async RPC wrappers.  Replaces the former MountCoreService + MountService
split.

Operations:
- Dynamic backend mounting/unmounting (with metastore persistence)
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
from nexus.lib.permission_utils import PermissionCheckError, check_permission
from nexus.lib.rpc_decorator import rpc_expose

logger = logging.getLogger(__name__)


def _needs_token_manager_db(backend_type: str, config: dict[str, Any]) -> bool:
    """Check if backend needs token_manager_db auto-injection."""
    if "token_manager_db" in config:
        return False
    from nexus.backends.base.registry import ConnectorRegistry

    try:
        info = ConnectorRegistry.get_info(backend_type)
    except KeyError:
        return False
    return info.user_scoped and "token_manager_db" in info.connection_args


def _record_error(result: dict, msg: str) -> None:
    """Append an error message to result["errors"] and log a warning."""
    result["errors"].append(msg)
    logger.warning(msg)


# Type alias for progress callback: (files_scanned: int, current_path: str) -> None
ProgressCallback = Callable[[int, str], None]

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext

    from .mount_manager import MountManager


class MountService:
    """Unified mount service — sync core logic + async RPC wrappers.

    Handles all mount management operations:
    - Add/remove dynamic backend mounts (sync core + async wrappers)
    - List available connectors and active mounts
    - Save/load/delete mount configurations
    - Sync metadata from connector backends

    Architecture:
        - Uses NexusFSGateway for all NexusFS access (filesystem, metadata,
          permissions, router)
        - Uses MountManager for persistence
        - Uses sub-services (SyncService, SyncJobService, etc.) for sync ops
        - Uses OperationContext for permissions
    """

    def __init__(
        self,
        router: Any,
        mount_manager: "MountManager | None" = None,
        nexus_fs: Any = None,
        *,
        gateway: Any = None,
        sync_service: Any = None,
        sync_job_service: Any = None,
        mount_persist_service: Any = None,
        oauth_service: Any = None,
        persist_service: Any = None,
        rmdir_fn: Any = None,
        token_manager_fn: Any = None,
        search_service: Any = None,
    ):
        """Initialize mount service.

        Args:
            router: Path router for backend resolution
            mount_manager: Optional mount manager for persistence
            nexus_fs: Optional NexusFS instance (for kernel ops: mkdir, rmdir, rebac)
            gateway: NexusFSGateway for NexusFS access (preferred over nexus_fs)
            sync_service: SyncService for metadata sync operations
            sync_job_service: SyncJobService for async sync job management
            mount_persist_service: MountPersistService for config persistence
            oauth_service: OAuthCredentialService for credential revocation
            persist_service: MountPersistService (alias, used by delete_connector)
            rmdir_fn: Callback to delete directories (NexusFS.rmdir)
            token_manager_fn: Callback to get token manager for OAuth revocation
            search_service: Optional SearchService for post-mount indexing (Issue #3148)
        """
        self.router = router
        self.mount_manager = mount_manager
        self.nexus_fs = nexus_fs
        self._gw = gateway
        self._sync_service = sync_service
        self._sync_job_service = sync_job_service
        self._mount_persist_service = mount_persist_service
        self._oauth_service = oauth_service
        self._persist_service = persist_service
        self._rmdir_fn = rmdir_fn
        self._token_manager_fn = token_manager_fn
        self._search_service = search_service

        logger.info("[MountService] Initialized")

    # =========================================================================
    # Post-mount hooks (Issue #3148)
    # =========================================================================

    async def _run_post_mount_hooks(self, mount_point: str) -> None:
        """Run async post-mount hooks after a mount is added.

        Hooks are best-effort: failures are logged as warnings but do not
        fail the mount operation (Decision #12).

        Runs:
        - Skill doc generation for SkillDocMixin backends
        - Search indexing via search_service (Issue #3148 Gap 1)
        """
        try:
            # Get backend from router to check capabilities
            route = self.router.route(mount_point)
            backend = route.backend if route else None
            if backend is None:
                return

            # Skill doc generation (via mount_hooks if available)
            from nexus.contracts.capabilities import ConnectorCapability

            if hasattr(backend, "has_capability") and backend.has_capability(
                ConnectorCapability.SKILL_DOC
            ):
                await asyncio.to_thread(self._generate_skill_docs, mount_point, backend)

            # Search indexing — index the mount point so content is discoverable.
            # Uses search_service DI (Issue #3148 Phase 1).
            if self._search_service is not None:
                try:
                    index_fn = getattr(self._search_service, "index_directory", None)
                    if index_fn is not None:
                        await asyncio.to_thread(index_fn, mount_point)
                        logger.info("Indexed mount point %s for search", mount_point)
                    else:
                        # Fall back to semantic_search_index if available
                        semantic_fn = getattr(self._search_service, "semantic_search_index", None)
                        if semantic_fn is not None:
                            asyncio.create_task(semantic_fn(mount_point, recursive=True))
                            logger.info("Queued semantic search indexing for %s", mount_point)
                except Exception:
                    logger.warning(
                        "Post-mount search indexing failed for %s",
                        mount_point,
                        exc_info=True,
                    )

        except Exception:
            logger.warning(
                "Post-mount hooks failed for %s (mount still active)", mount_point, exc_info=True
            )

    def _generate_skill_docs(self, mount_point: str, backend: Any) -> None:
        """Generate .skill/ directory for a connector backend (sync).

        Called from post-mount hooks and sync completion.
        """
        from nexus.backends.connectors.base import SkillDocMixin

        if not isinstance(backend, SkillDocMixin):
            return
        if not backend.SKILL_NAME:
            return

        backend.set_mount_path(mount_point)

        # Determine filesystem to write to
        fs = None
        if self._gw is not None and hasattr(self._gw, "nexus_fs"):
            fs = self._gw.nexus_fs
        elif self.nexus_fs is not None:
            fs = self.nexus_fs

        if fs is not None:
            try:
                result = backend.write_skill_docs(mount_point, fs)
                if result.get("skill_md"):
                    logger.info(
                        "Generated skill docs for %s at %s", mount_point, result["skill_md"]
                    )
            except Exception:
                logger.warning("Failed to generate skill docs for %s", mount_point, exc_info=True)

    # =========================================================================
    # Sync Core Logic (inlined from MountCoreService)
    # =========================================================================

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
        from nexus.backends.base.factory import BackendFactory

        record_store = None
        if self._gw is not None:
            record_store = self._gw.record_store
        elif self.nexus_fs and hasattr(self.nexus_fs, "_record_store"):
            record_store = self.nexus_fs._record_store
        return BackendFactory.create(backend_type, config, record_store=record_store)

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
        logger.info(f"Setting up mount point: {mount_point}")

        # Create directory entry
        if self._gw is not None:
            try:
                self._gw.sys_mkdir(mount_point, parents=True, exist_ok=True, context=context)
                logger.info(f"Created directory entry for mount point: {mount_point}")
            except Exception as e:
                logger.warning(f"Failed to create directory entry: {e}")
        elif self.nexus_fs and hasattr(self.nexus_fs, "sys_mkdir"):
            try:
                self.nexus_fs.sys_mkdir(mount_point, parents=True, exist_ok=True)
                logger.info(f"Created directory entry for mount point: {mount_point}")
            except Exception as e:
                logger.warning(f"Failed to create directory entry: {e}")

        # Grant owner permission
        self._grant_owner_permission(mount_point, context)

    def _grant_owner_permission(
        self,
        mount_point: str,
        context: "OperationContext | None",
    ) -> None:
        """Grant direct_owner permission to mount creator.

        Raises on genuine ReBAC failures so that ``add_mount`` can roll
        back the router registration -- a mount must never be active
        without permissions (Issue #2754).

        If ReBAC is not configured (record_store missing), logs a warning
        and returns gracefully -- this allows mounts to work in minimal
        deployments without permission enforcement.

        Args:
            mount_point: Virtual path
            context: Operation context
        """
        if not context:
            logger.warning("[MOUNT-PERM] No context, skipping permission grant")
            return

        zone_id = get_zone_id(context)
        subject_type, subject_id = get_user_identity(context)

        if not subject_id:
            logger.warning("[MOUNT-PERM] No subject_id, skipping permission grant")
            return

        if self._gw is not None:
            try:
                tuple_id = self._gw.rebac_create(
                    subject=(subject_type, subject_id),
                    relation="direct_owner",
                    object=("file", mount_point),
                    zone_id=zone_id,
                )
            except RuntimeError as exc:
                if "not available" in str(exc):
                    logger.warning(
                        "[MOUNT-PERM] ReBAC not available, skipping permission grant: %s",
                        exc,
                    )
                    return
                raise

            logger.info(
                "Granted direct_owner to %s:%s for %s (tuple_id=%s)",
                subject_type,
                subject_id,
                mount_point,
                tuple_id,
            )
        elif self.nexus_fs and self.nexus_fs.service("rebac"):
            try:
                self.nexus_fs.service("rebac").rebac_create_sync(
                    subject=(subject_type, subject_id),
                    relation="direct_owner",
                    object=("file", mount_point),
                    zone_id=zone_id,
                )
                logger.info(
                    "Granted direct_owner to %s:%s for %s",
                    subject_type,
                    subject_id,
                    mount_point,
                )
            except Exception as e:
                logger.warning(
                    "Failed to grant direct_owner for %s: %s: %s",
                    mount_point,
                    type(e).__name__,
                    e,
                )
        else:
            logger.warning(
                "[MOUNT-PERM] No gateway or rebac service available, skipping permission grant"
            )

    def _check_permission(
        self,
        path: str,
        permission: str,
        context: "OperationContext | None",
    ) -> bool:
        """Check if user has permission on path.

        Delegates to shared permission_utils.check_permission when gateway
        is available, otherwise returns True (permissive fallback).
        Raises PermissionCheckError on infrastructure failures.
        """
        if self._gw is not None:
            return bool(check_permission(self._gw, path, permission, context))
        # No gateway — permissive fallback
        return True

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
    # Public Sync Accessors (for NexusFS facade and MountPersistService)
    # =========================================================================

    def add_mount_sync(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        context: "OperationContext | None" = None,
    ) -> str:
        """Add a dynamic backend mount (synchronous).

        .. deprecated::
            Use ``await add_mount(...)`` instead. This sync entry point
            skips async post-mount hooks (skill doc regeneration, search
            indexing). Retained for internal use by MountPersistService
            and NexusFS facade during startup. Will be made private in
            a future release.

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
        if _needs_token_manager_db(backend_type, config):
            if self._gw is not None:
                try:
                    database_url = self._gw.get_database_url()
                    config["token_manager_db"] = database_url
                except RuntimeError as e:
                    raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e
            elif self.nexus_fs:
                try:
                    database_url = get_database_url(self.nexus_fs)
                    config = {**config, "token_manager_db": database_url}
                except RuntimeError as e:
                    raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e
            else:
                raise RuntimeError(
                    f"Cannot create {backend_type} mount: no gateway or nexus_fs configured"
                )

        # Create backend instance
        backend = self._create_backend(backend_type, config)

        # Add to router, then setup -- rollback on failure (Issue #2754).
        # If _setup_mount_point fails after the mount is active in the router,
        # the mount would be accessible without proper permissions configured.
        self.router.add_mount(
            mount_point=mount_point,
            backend=backend,
            readonly=readonly,
            io_profile=io_profile,
        )
        try:
            self._setup_mount_point(mount_point, context)
        except Exception:
            logger.error(
                "Mount setup failed for %s, rolling back router registration",
                mount_point,
            )
            self.router.remove_mount(mount_point)
            raise

        return mount_point

    def remove_mount_sync(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Remove a backend mount (synchronous).

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
        if not self.router.remove_mount(mount_point):
            result["errors"].append(f"Mount not found: {mount_point}")
            return result

        result["removed"] = True
        logger.info(f"Removed mount from router: {mount_point}")

        # Extract zone_id once for all cleanup operations
        zone_id = get_zone_id(context)

        # --- Gateway-based cleanup (preferred) ---
        if self._gw is not None:
            # Delete all metadata entries (mount point + children)
            try:
                dir_prefix = mount_point if mount_point.endswith("/") else mount_point + "/"
                child_entries = self._gw.metadata_list(dir_prefix)
                paths_to_delete = [entry.path for entry in child_entries] if child_entries else []
                paths_to_delete.append(mount_point)  # Include mount point itself
                self._gw.metadata_delete_batch(paths_to_delete)
                result["files_deleted"] = len(paths_to_delete)
                logger.info(f"Deleted {len(paths_to_delete)} metadata entries for {mount_point}")
            except Exception as e:
                _record_error(result, f"Failed to delete metadata entries for {mount_point}: {e}")

            # Clean up sparse directory index entries
            try:
                dir_entries_deleted = self._gw.delete_directory_entries_recursive(
                    mount_point, zone_id
                )
                result["directory_entries_deleted"] = dir_entries_deleted
                logger.info(
                    f"Deleted {dir_entries_deleted} directory index entries under {mount_point}"
                )
            except Exception as e:
                _record_error(result, f"Failed to clean up directory index: {e}")

            # Clean up hierarchy tuples
            try:
                removed = self._gw.remove_parent_tuples(mount_point, zone_id)
                result["permissions_cleaned"] += removed
                logger.info(f"Removed {removed} parent tuples for {mount_point}")
            except Exception as e:
                _record_error(result, f"Failed to clean up parent tuples: {e}")

            # Remove permission tuples
            try:
                deleted = self._gw.rebac_delete_object_tuples(
                    object=("file", mount_point),
                    zone_id=zone_id,
                )
                result["permissions_cleaned"] += deleted
                logger.info(f"Removed {deleted} permission tuples for {mount_point}")
            except Exception as e:
                _record_error(result, f"Failed to delete permission tuples: {e}")
        else:
            # --- Fallback: NexusFS-based cleanup ---
            # Delete the mount point directory
            if self.nexus_fs and hasattr(self.nexus_fs, "metadata"):
                try:
                    if hasattr(self.nexus_fs.metadata, "delete"):
                        self.nexus_fs.metadata.delete(mount_point)
                        result["directory_deleted"] = True
                        logger.info(f"Deleted mount point directory: {mount_point}")
                except Exception as e:
                    error_msg = f"Failed to delete mount point directory {mount_point}: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)

            # Clean up ReBAC permissions
            if self.nexus_fs and hasattr(self.nexus_fs, "hierarchy_manager"):
                try:
                    if hasattr(self.nexus_fs.hierarchy_manager, "remove_parent_tuples"):
                        tuples_removed = self.nexus_fs.hierarchy_manager.remove_parent_tuples(
                            mount_point, zone_id
                        )
                        result["permissions_cleaned"] += tuples_removed
                        logger.info(f"Removed {tuples_removed} parent tuples for {mount_point}")
                except Exception as e:
                    error_msg = f"Failed to clean up parent tuples: {e}"
                    result["errors"].append(error_msg)
                    logger.warning(error_msg)

            # Remove direct_owner permission tuple
            if self.nexus_fs and self.nexus_fs.service("rebac"):
                try:
                    svc = self.nexus_fs.service("rebac")
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
            logger.warning(f"Mount removed with {len(result['errors'])} errors: {result['errors']}")
        else:
            logger.info(
                f"Successfully removed mount {mount_point} "
                f"(directory_deleted={result['directory_deleted']}, "
                f"permissions_cleaned={result['permissions_cleaned']})"
            )

        return result

    def list_mounts_sync(
        self,
        context: "OperationContext | None" = None,
    ) -> list[dict[str, Any]]:
        """List all active mounts with permission filtering (synchronous).

        Args:
            context: Operation context for permission checks

        Returns:
            List of mount info dictionaries
        """
        mounts = []

        router_mounts = list(self.router.list_mounts())
        logger.info(f"[LIST_MOUNTS] Total mounts in router: {len(router_mounts)}")

        for mount_info in router_mounts:
            mount_point = mount_info.mount_point

            # Check permission -- exclude on infrastructure failure (fail-safe)
            try:
                has_permission = self._check_mount_permission(mount_point, context)
            except PermissionCheckError:
                logger.warning("Permission check failed for mount %s, excluding", mount_point)
                continue

            if has_permission:
                mounts.append(
                    {
                        "mount_point": mount_info.mount_point,
                        "readonly": mount_info.readonly,
                        "admin_only": mount_info.admin_only,
                    }
                )

        return mounts

    def get_mount_sync(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Get details about a specific mount (synchronous).

        Args:
            mount_point: Virtual path of mount
            context: Operation context for permissions

        Returns:
            Mount info dict or None if not found or no permission
        """
        # Check permission: user must have read access
        if not self._check_permission(mount_point, "read", context):
            return None

        mount_info = self.router.get_mount(mount_point)
        if mount_info:
            return {
                "mount_point": mount_info.mount_point,
                "readonly": mount_info.readonly,
                "admin_only": mount_info.admin_only,
            }
        return None

    def has_mount_sync(self, mount_point: str) -> bool:
        """Check if mount exists (synchronous).

        Args:
            mount_point: Virtual path to check

        Returns:
            True if mount exists
        """
        return bool(self.router.has_mount(mount_point))

    def list_connectors_sync(self, category: str | None = None) -> list[dict[str, Any]]:
        """List available connector types (synchronous).

        Args:
            category: Optional filter by category

        Returns:
            List of connector info dictionaries
        """
        from nexus.backends.base.registry import ConnectorRegistry

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

    def delete_connector_sync(
        self,
        mount_point: str,
        revoke_oauth: bool = False,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Delete a connector completely with bundled operations (synchronous).

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
            remove_result = self.remove_mount_sync(mount_point, context)
            result["removed"] = remove_result.get("removed", False)
            result["directory_deleted"] = remove_result.get("removed", False)
            if remove_result.get("errors"):
                result["warnings"].extend(remove_result["errors"])
        except PermissionError:
            raise
        except Exception as e:
            result["warnings"].append(f"Failed to deactivate connector (continuing): {e}")

        # Step 2: Delete saved configuration (FATAL - must succeed)
        persist_svc = self._mount_persist_service or self._persist_service
        if persist_svc is None:
            raise RuntimeError("MountPersistService not available for delete_connector")
        try:
            config_deleted = persist_svc.delete_saved_mount(mount_point)
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
        rmdir_fn = self._rmdir_fn
        if rmdir_fn is None and self.nexus_fs and hasattr(self.nexus_fs, "sys_rmdir"):
            rmdir_fn = self.nexus_fs.sys_rmdir
        if rmdir_fn is not None:
            try:
                rmdir_fn(mount_point, recursive=True, context=context)
                result["directory_deleted"] = True
                logger.info(f"Deleted mount point directory: {mount_point}")
            except Exception as e:
                result["warnings"].append(
                    f"Failed to delete mount point directory (non-fatal): {e}"
                )
                logger.warning(f"Failed to delete mount point directory {mount_point}: {e}")

        return result

    # =========================================================================
    # Public API: Async RPC Wrappers
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
            backend_type: Backend type - "cas_local", "cas_gcs", "path_gcs", "google_drive", etc.
            backend_config: Backend-specific configuration dict
            readonly: Whether mount is read-only (default: False)
            io_profile: I/O tuning profile
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (unique identifier for this mount)

        Raises:
            PermissionError: If user lacks write permission on parent path
            RuntimeError: If backend type is not supported
        """
        mount_id = await asyncio.to_thread(
            self.add_mount_sync,
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            readonly=readonly,
            io_profile=io_profile,
            context=context,
        )

        # --- Post-mount hooks (Issue #3148, Decision #1A) ---
        # These run only through the async path. Sync callers (startup,
        # persist service) skip hooks — a separate generate_all_skill_docs
        # pass handles startup.
        await self._run_post_mount_hooks(mount_point)

        return mount_id

    @rpc_expose(description="Remove backend mount")
    async def remove_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Remove a backend mount from the filesystem.

        This removes the mount from the router and cleans up metadata,
        directory entries, hierarchy tuples, and permission tuples.

        Args:
            mount_point: Virtual path of mount to remove (e.g., "/personal/alice")
            context: Operation context (automatically provided by RPC server)

        Returns:
            Dictionary with removal details:
            - removed: bool - Whether mount was removed
            - directory_deleted: bool - Whether mount point directory was deleted
            - permissions_cleaned: int - Number of permission tuples removed
            - errors: list[str] - Any errors encountered
        """
        return await asyncio.to_thread(
            self.remove_mount_sync,
            mount_point=mount_point,
            context=context,
        )

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
            RuntimeError: If mount_persist_service not configured
        """
        result = await asyncio.to_thread(
            self.delete_connector_sync,
            mount_point=mount_point,
            revoke_oauth=revoke_oauth if not self._oauth_service else False,
            provider=provider,
            user_email=user_email,
            context=context,
        )

        # Handle async OAuth revocation via oauth_service if available
        if revoke_oauth and self._oauth_service is not None:
            if not provider or not user_email:
                result["warnings"].append(
                    "OAuth revocation requested but provider or user_email not provided"
                )
            else:
                try:
                    revoke_result = await self._oauth_service.revoke_credential(
                        provider=provider,
                        user_email=user_email,
                        context=context,
                    )
                    result["oauth_revoked"] = revoke_result.get("success", False)
                except Exception as e:
                    result["warnings"].append(
                        f"Failed to revoke OAuth credentials (non-fatal): {e}"
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
        return await asyncio.to_thread(self.list_connectors_sync, category)

    @rpc_expose(description="Update mount backend configuration")
    async def update_mount(
        self,
        mount_point: str,
        backend_config: dict[str, Any],
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Update a mount's backend configuration without removing it.

        Reconfigures the backend (new endpoint, rotated key, updated token)
        while preserving the DT_MOUNT entry, permissions, and metadata index.
        This avoids the remove+add cycle that loses permissions and search index.

        Phase 5 (Issue #3148).

        Args:
            mount_point: Virtual path of mount to update
            backend_config: New backend configuration (merged with existing)
            context: Operation context for permission checks

        Returns:
            Dict with update details: {updated, mount_point, changed_keys}

        Raises:
            PermissionError: If user lacks write permission on mount
            ValueError: If mount does not exist
        """
        if not self._check_permission(mount_point, "write", context):
            raise PermissionError(f"Cannot update mount {mount_point}: no write permission")

        route = self.router.route(mount_point)
        if route is None:
            raise ValueError(f"Mount not found: {mount_point}")

        backend = route.backend
        result: dict[str, Any] = {
            "updated": False,
            "mount_point": mount_point,
            "changed_keys": [],
        }

        # Apply config updates to backend
        for key, value in backend_config.items():
            if hasattr(backend, key):
                old_val = getattr(backend, key, None)
                if old_val != value:
                    setattr(backend, key, value)
                    result["changed_keys"].append(key)
            elif hasattr(backend, f"_{key}"):
                old_val = getattr(backend, f"_{key}", None)
                if old_val != value:
                    setattr(backend, f"_{key}", value)
                    result["changed_keys"].append(key)

        result["updated"] = len(result["changed_keys"]) > 0

        if result["updated"]:
            logger.info(
                "Updated mount %s config: %s",
                mount_point,
                ", ".join(result["changed_keys"]),
            )

        return result

    @rpc_expose(description="Refresh OAuth credentials for a mount")
    async def reauth_mount(
        self,
        mount_point: str,
        provider: str | None = None,
        user_email: str | None = None,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any]:
        """Refresh or rotate OAuth credentials for a mounted connector.

        Triggers a token refresh via TokenManager without unmounting.
        Useful for credential rotation, expired tokens, or provider changes.

        Phase 5 (Issue #3148).

        Args:
            mount_point: Virtual path of mount to reauth
            provider: OAuth provider name (auto-detected from backend if not given)
            user_email: User email for token lookup (auto-detected from context)
            context: Operation context

        Returns:
            Dict with reauth details: {refreshed, provider, user_email}

        Raises:
            PermissionError: If user lacks write permission on mount
            ValueError: If mount not found or not OAuth-capable
        """
        if not self._check_permission(mount_point, "write", context):
            raise PermissionError(f"Cannot reauth mount {mount_point}: no write permission")

        route = self.router.route(mount_point)
        if route is None:
            raise ValueError(f"Mount not found: {mount_point}")

        backend = route.backend

        # Auto-detect provider from backend
        if provider is None:
            provider = getattr(backend, "provider", None) or getattr(backend, "_provider", None)
        if provider is None:
            raise ValueError(f"Cannot determine OAuth provider for {mount_point}")

        # Auto-detect user_email from context
        if user_email is None and context is not None:
            user_email = getattr(context, "user_id", None)
        if user_email is None:
            raise ValueError("user_email required for reauth")

        # Get token manager from backend or service
        token_manager = getattr(backend, "_token_manager", None)
        if token_manager is None and self._token_manager_fn is not None:
            token_manager = self._token_manager_fn()

        if token_manager is None:
            raise ValueError(f"No token manager available for {mount_point}")

        # Refresh token
        zone_id = getattr(context, "zone_id", None) if context else None
        try:
            await asyncio.to_thread(
                token_manager.refresh_token,
                user_email=user_email,
                provider=provider,
                zone_id=zone_id,
            )
            logger.info("Refreshed OAuth token for %s (%s/%s)", mount_point, provider, user_email)
            return {"refreshed": True, "provider": provider, "user_email": user_email}
        except Exception as e:
            logger.warning("Token refresh failed for %s: %s", mount_point, e)
            return {
                "refreshed": False,
                "provider": provider,
                "user_email": user_email,
                "error": str(e),
            }

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
        """
        return await asyncio.to_thread(self.list_mounts_sync, context)

    @rpc_expose(description="Get mount details")
    async def get_mount(
        self,
        mount_point: str,
        context: "OperationContext | None" = None,
    ) -> dict[str, Any] | None:
        """Get details about a specific mount.

        Args:
            mount_point: Virtual path of mount (e.g., "/personal/alice")

        Returns:
            Mount info dict if found, None otherwise. Dict contains:
                - mount_point: Virtual path (str)
                - readonly: Read-only flag (bool)
                - admin_only: Admin-only flag (bool)
        """
        return await asyncio.to_thread(self.get_mount_sync, mount_point, context)

    @rpc_expose(description="Check if mount exists")
    async def has_mount(self, mount_point: str) -> bool:
        """Check if a mount exists at the given path.

        Args:
            mount_point: Virtual path to check (e.g., "/personal/alice")

        Returns:
            True if mount exists, False otherwise
        """
        return await asyncio.to_thread(self.has_mount_sync, mount_point)

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
            backend_type: Backend type - "cas_local", "cas_gcs", etc.
            backend_config: Backend-specific configuration dict
            readonly: Whether mount is read-only (default: False)
            io_profile: I/O tuning profile
            owner_user_id: User who owns this mount (optional)
            zone_id: Zone ID for multi-zone isolation (optional)
            description: Human-readable description (optional)
            context: Operation context (automatically provided by RPC server)

        Returns:
            Mount ID (UUID string)

        Raises:
            ValueError: If mount already exists at mount_point
            RuntimeError: If mount manager is not available
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
            self._grant_owner_permission(mount_point, context)

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
        unless explicit filter parameters are provided.

        Args:
            owner_user_id: Filter by owner user ID (optional, defaults to current user)
            zone_id: Filter by zone ID (optional, defaults to current zone)
            context: Operation context (automatically provided by RPC server)

        Returns:
            List of saved mount configurations owned by the user or in their zone

        Raises:
            RuntimeError: If mount manager is not available
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

        Args:
            mount_point: Virtual path of saved mount to load

        Returns:
            Mount ID if successfully loaded and activated

        Raises:
            ValueError: If mount not found in database
            RuntimeError: If mount manager is not available
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
            if self._gw is not None:
                try:
                    database_url = self._gw.get_database_url()
                    backend_config["token_manager_db"] = database_url
                except RuntimeError as e:
                    raise RuntimeError(f"Cannot load {backend_type} mount: {e}") from e
            elif self.nexus_fs:
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
        """
        if not self.mount_manager:
            raise RuntimeError(
                "Mount manager not available. Ensure NexusFS is initialized with a database."
            )

        return await asyncio.to_thread(self.mount_manager.remove_mount, mount_point)

    # =========================================================================
    # Public API: Metadata Synchronization
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

        sync_result = await asyncio.to_thread(_sync_mount_sync)

        # Post-sync: regenerate .skill/ directory (Issue #3148, Decision #7B).
        # Schema files are re-generated on every sync so they stay fresh.
        if mount_point and not dry_run:
            try:
                route = self.router.route(mount_point)
                if route:
                    await asyncio.to_thread(self._generate_skill_docs, mount_point, route.backend)
            except Exception:
                logger.warning(
                    "Post-sync skill doc regeneration failed for %s",
                    mount_point,
                    exc_info=True,
                )

        return sync_result

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
