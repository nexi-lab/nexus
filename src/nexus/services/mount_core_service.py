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

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.context_utils import get_tenant_id, get_user_identity

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.services.gateway import NexusFSGateway

logger = logging.getLogger(__name__)


class MountCoreService:
    """Core mount management operations (SYNC).

    All methods are synchronous. FastAPI auto-wraps with to_thread.

    AI-Friendly Design:
    - All NexusFS access via self._gw
    - Clear method responsibilities
    - No async wrappers
    """

    def __init__(self, gateway: NexusFSGateway):
        """Initialize mount core service.

        Args:
            gateway: NexusFSGateway for NexusFS access
        """
        self._gw = gateway

    # =========================================================================
    # Core Mount Operations
    # =========================================================================

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        priority: int = 0,
        readonly: bool = False,
        context: OperationContext | None = None,
    ) -> str:
        """Add a dynamic backend mount.

        Args:
            mount_point: Virtual path where backend is mounted
            backend_type: Backend type identifier
            backend_config: Backend-specific configuration
            priority: Mount priority (higher takes precedence)
            readonly: Whether mount is read-only
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
        if (
            backend_type in ("gdrive_connector", "gmail_connector", "x_connector")
            and "token_manager_db" not in config
        ):
            try:
                database_url = self._gw.get_database_url()
                config["token_manager_db"] = database_url
            except RuntimeError as e:
                raise RuntimeError(f"Cannot create {backend_type} mount: {e}") from e

        # Create backend instance
        backend = self._create_backend(backend_type, config)

        # Add to router
        self._gw.router.add_mount(
            mount_point=mount_point,
            backend=backend,
            priority=priority,
            readonly=readonly,
        )

        # Setup mount point (directory, permissions, skill)
        self._setup_mount_point(mount_point, backend_type, context)

        return mount_point

    def remove_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
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
        logger.info(f"Removed mount from router: {mount_point}")

        # Delete directory entry
        try:
            self._gw.metadata_delete(mount_point)
            result["directory_deleted"] = True
            logger.info(f"Deleted mount point directory: {mount_point}")
        except Exception as e:
            error_msg = f"Failed to delete directory {mount_point}: {e}"
            result["errors"].append(error_msg)
            logger.warning(error_msg)

        # Clean up hierarchy tuples
        try:
            tenant_id = get_tenant_id(context)
            removed = self._gw.remove_parent_tuples(mount_point, tenant_id)
            result["permissions_cleaned"] += removed
            logger.info(f"Removed {removed} parent tuples for {mount_point}")
        except Exception as e:
            error_msg = f"Failed to clean up parent tuples: {e}"
            result["errors"].append(error_msg)
            logger.warning(error_msg)

        # Remove permission tuples
        try:
            tenant_id = get_tenant_id(context)
            deleted = self._gw.rebac_delete_object_tuples(
                object=("file", mount_point),
                tenant_id=tenant_id,
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
                f"(directory_deleted={result['directory_deleted']}, "
                f"permissions_cleaned={result['permissions_cleaned']})"
            )

        return result

    def list_mounts(
        self,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """List all active mounts with permission filtering.

        Args:
            context: Operation context for permission checks

        Returns:
            List of mount info dictionaries
        """
        mounts = []

        router_mounts = list(self._gw.router.list_mounts())
        logger.info(f"[LIST_MOUNTS] Total mounts in router: {len(router_mounts)}")

        for mount_info in router_mounts:
            mount_point = mount_info.mount_point

            # Check permission
            has_permission = self._check_mount_permission(mount_point, context)

            if has_permission:
                mounts.append(
                    {
                        "mount_point": mount_info.mount_point,
                        "priority": mount_info.priority,
                        "readonly": mount_info.readonly,
                        "backend_type": type(mount_info.backend).__name__,
                    }
                )

        return mounts

    def get_mount(
        self,
        mount_point: str,
        context: OperationContext | None = None,
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
                "priority": mount_info.priority,
                "readonly": mount_info.readonly,
                "backend_type": type(mount_info.backend).__name__,
            }
        return None

    def has_mount(self, mount_point: str) -> bool:
        """Check if mount exists.

        Args:
            mount_point: Virtual path to check

        Returns:
            True if mount exists
        """
        return self._gw.router.has_mount(mount_point)

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

    def _create_backend(self, backend_type: str, config: dict[str, Any]) -> Any:
        """Create backend instance from type and config.

        Args:
            backend_type: Backend type identifier
            config: Backend configuration

        Returns:
            Backend instance

        Raises:
            RuntimeError: If backend type is not supported
        """
        from nexus.backends.backend import Backend

        backend: Backend

        if backend_type == "local":
            from nexus.backends.local import LocalBackend

            backend = LocalBackend(root_path=config["data_dir"])

        elif backend_type == "gcs":
            from nexus.backends.gcs import GCSBackend

            backend = GCSBackend(
                bucket_name=config["bucket"],
                project_id=config.get("project_id"),
                credentials_path=config.get("credentials_path"),
            )

        elif backend_type == "gcs_connector":
            from nexus.backends.gcs_connector import GCSConnectorBackend

            backend = GCSConnectorBackend(
                bucket_name=config["bucket"],
                project_id=config.get("project_id"),
                prefix=config.get("prefix", ""),
                credentials_path=config.get("credentials_path"),
                access_token=config.get("access_token"),
                session_factory=self._gw.session_factory,
            )

        elif backend_type == "s3_connector":
            from nexus.backends.s3_connector import S3ConnectorBackend

            backend = S3ConnectorBackend(
                bucket_name=config["bucket"],
                region_name=config.get("region_name"),
                prefix=config.get("prefix", ""),
                credentials_path=config.get("credentials_path"),
                access_key_id=config.get("access_key_id"),
                secret_access_key=config.get("secret_access_key"),
                session_token=config.get("session_token"),
                session_factory=self._gw.session_factory,
            )

        elif backend_type == "gdrive_connector":
            from nexus.backends.gdrive_connector import GoogleDriveConnectorBackend

            backend = GoogleDriveConnectorBackend(
                token_manager_db=config["token_manager_db"],
                root_folder=config.get("root_folder", "nexus-data"),
                user_email=config.get("user_email"),
            )

        elif backend_type == "x_connector":
            from nexus.backends.x_connector import XConnectorBackend

            backend = XConnectorBackend(
                token_manager_db=config["token_manager_db"],
                user_email=config.get("user_email"),
                cache_ttl=config.get("cache_ttl"),
                cache_dir=config.get("cache_dir"),
            )

        elif backend_type == "hn_connector":
            from nexus.backends.hn_connector import HNConnectorBackend

            backend = HNConnectorBackend(
                cache_ttl=config.get("cache_ttl", 300),
                stories_per_feed=config.get("stories_per_feed", 10),
                include_comments=config.get("include_comments", True),
                session_factory=self._gw.session_factory,
            )

        elif backend_type == "gmail_connector":
            from nexus.backends.gmail_connector import GmailConnectorBackend

            backend = GmailConnectorBackend(
                token_manager_db=config["token_manager_db"],
                user_email=config.get("user_email"),
                provider=config.get("provider", "gmail"),
                session_factory=self._gw.session_factory,
                max_message_per_label=config.get("max_message_per_label", 2000),
            )

        else:
            raise RuntimeError(f"Unsupported backend type: {backend_type}")

        return backend

    def _setup_mount_point(
        self,
        mount_point: str,
        backend_type: str,
        context: OperationContext | None,
    ) -> None:
        """Setup mount point with directory, permissions, and skill.

        Args:
            mount_point: Virtual path
            backend_type: Backend type identifier
            context: Operation context
        """
        logger.info(f"Setting up mount point: {mount_point}")

        # Create directory entry
        try:
            self._gw.mkdir(mount_point, parents=True, exist_ok=True, context=context)
            logger.info(f"Created directory entry for mount point: {mount_point}")
        except Exception as e:
            logger.warning(f"Failed to create directory entry: {e}")

        # Grant owner permission
        self._grant_owner_permission(mount_point, context)

        # Generate SKILL.md for connector backends
        if backend_type.endswith("_connector") or backend_type in ("google_drive", "gdrive"):
            self._generate_skill(mount_point, backend_type, context)

    def _grant_owner_permission(
        self,
        mount_point: str,
        context: OperationContext | None,
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
            tenant_id = get_tenant_id(context)
            subject_type, subject_id = get_user_identity(context)

            if not subject_id:
                logger.warning("[MOUNT-PERM] No subject_id, skipping permission grant")
                return

            tuple_id = self._gw.rebac_create(
                subject=(subject_type, subject_id),
                relation="direct_owner",
                object=("file", mount_point),
                tenant_id=tenant_id,
            )

            logger.info(
                f"Granted direct_owner to {subject_type}:{subject_id} "
                f"for {mount_point} (tuple_id={tuple_id})"
            )
        except Exception as e:
            logger.warning(f"Failed to grant permission for {mount_point}: {e}")

    def _generate_skill(
        self,
        mount_point: str,
        backend_type: str,
        context: OperationContext | None,
    ) -> bool:
        """Generate SKILL.md for connector mount.

        Args:
            mount_point: Virtual path
            backend_type: Backend type identifier
            context: Operation context

        Returns:
            True if skill was generated
        """
        try:
            from nexus.backends.service_map import ServiceMap
            from nexus.skills.skill_generator import generate_skill_md

            # Get service name from backend type
            service_name = ServiceMap.get_service_name(connector=backend_type)
            if not service_name:
                service_name = backend_type.replace("_connector", "").replace("_", "-")

            # Determine skill path
            if context and hasattr(context, "user_id") and context.user_id:
                skill_base_path = f"/skills/users/{context.user_id}/"
            elif context and hasattr(context, "tenant_id") and context.tenant_id:
                skill_base_path = f"/skills/tenants/{context.tenant_id}/"
            else:
                skill_base_path = "/skills/system/"

            skill_path = f"{skill_base_path}{service_name}/"
            skill_md_path = f"{skill_path}SKILL.md"

            # Generate skill content
            skill_md = generate_skill_md(
                service_name=service_name,
                mount_path=mount_point,
            )

            # Create directory and write skill
            try:
                self._gw.mkdir(skill_path, parents=True, exist_ok=True, context=context)
            except Exception as e:
                logger.warning(f"Failed to create skill directory: {e}")

            self._gw.write(skill_md_path, skill_md, context=context)
            logger.info(f"Generated connector skill: {skill_md_path}")
            return True

        except Exception as e:
            logger.warning(f"Failed to generate skill for {backend_type}: {e}")
            return False

    def _check_permission(
        self,
        path: str,
        permission: str,
        context: OperationContext | None,
    ) -> bool:
        """Check if user has permission on path.

        Args:
            path: Virtual path to check
            permission: Permission to check ("read", "write", "owner")
            context: Operation context

        Returns:
            True if user has permission
        """
        if not context:
            # No context = allow (backward compatibility)
            return True

        try:
            # Admin users bypass permission checks
            is_admin = getattr(context, "is_admin", False)
            if is_admin:
                return True

            subject_type, subject_id = get_user_identity(context)
            if not subject_id:
                return False

            tenant_id = get_tenant_id(context)

            return self._gw.rebac_check(
                subject=(subject_type, subject_id),
                permission=permission,
                object=("file", path),
                tenant_id=tenant_id,
            )
        except Exception as e:
            logger.error(f"Permission check failed for {path}: {e}")
            return False

    def _check_mount_permission(
        self,
        mount_point: str,
        context: OperationContext | None,
    ) -> bool:
        """Check if user has read permission on mount.

        Args:
            mount_point: Virtual path
            context: Operation context

        Returns:
            True if user has permission
        """
        return self._check_permission(mount_point, "read", context)
