"""PermissionChecker — service-layer permission checking pipeline.

Dependency-injected checker encapsulating the full pipeline:
  1. Enforce-permissions gate
  2. Zone boundary security (P0-4, Issue #819)
  3. Admin / system bypass
  4. Virtual-view path resolution (Fix #332)
  5. Owner fast-path (Issue #920)
  6. ReBAC graph traversal via PermissionEnforcer

Wired into NexusFS via factory/orchestrator.py DI.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.types import OperationContext, Permission

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)


class PermissionChecker:
    """Checks file-level permissions using ReBAC, owner fast-path, and admin bypass.

    All dependencies are provided via keyword-only constructor arguments so that
    the checker is fully decoupled from NexusFS and easy to test in isolation.
    """

    def __init__(
        self,
        *,
        permission_enforcer: Any,
        metadata_store: Any,
        default_context: Any,
        enforce_permissions: bool,
    ) -> None:
        """Initialise the permission checker.

        Args:
            permission_enforcer: PermissionEnforcer instance (ReBAC graph).
            metadata_store: Metastore used for the owner fast-path lookup.
            default_context: Default OperationContext when callers pass ``None``.
            enforce_permissions: Global toggle; when ``False`` all checks are
                skipped immediately.
        """
        self._permission_enforcer = permission_enforcer
        self._metadata_store: Any = metadata_store
        self._default_context = default_context
        self._enforce_permissions = enforce_permissions

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | None = None,
        file_metadata: "FileMetadata | None" = None,
    ) -> None:
        """Check whether the requested operation is permitted.

        Args:
            path: Virtual file path.
            permission: Permission to check (READ, WRITE, EXECUTE).
            context: Optional operation context (falls back to *default_context*).
            file_metadata: Pre-fetched metadata for the owner fast-path
                (avoids a redundant metadata lookup when the caller already
                has it).

        Raises:
            PermissionError: If access is denied.
        """

        # ----------------------------------------------------------
        # Gate: skip if permission enforcement is disabled
        # ----------------------------------------------------------
        if not self._enforce_permissions:
            return

        # ----------------------------------------------------------
        # Resolve context
        # ----------------------------------------------------------
        ctx_raw = context or self._default_context
        assert isinstance(ctx_raw, OperationContext), "Context must be OperationContext"
        ctx: OperationContext = ctx_raw

        # ----------------------------------------------------------
        # P0-4: Zone boundary security check (Issue #819)
        # Even admins need zone boundary checks (unless they have
        # the MANAGE_ZONES capability).
        # ----------------------------------------------------------
        if ctx.is_admin and self._permission_enforcer:
            import importlib as _il

            AdminCapability = _il.import_module(
                "nexus.bricks.rebac.permissions_enhanced"
            ).AdminCapability

            # Extract zone from path (format: /zone/{zone_id}/...)
            path_zone_id: str | None = None
            if path.startswith("/zone/"):
                parts = path[6:].split("/", 1)  # Remove "/zone/" prefix
                if parts:
                    path_zone_id = parts[0]

            # Check if admin is attempting cross-zone access without MANAGE_ZONES
            if (
                path_zone_id
                and ctx.zone_id
                and path_zone_id != ctx.zone_id
                and AdminCapability.MANAGE_ZONES not in ctx.admin_capabilities
            ):
                raise PermissionError(
                    f"Access denied: Cross-zone access requires MANAGE_ZONES capability. "
                    f"Context zone: {ctx.zone_id}, Path zone: {path_zone_id}"
                )

        # ----------------------------------------------------------
        # Admin / system bypass
        # ----------------------------------------------------------
        if ctx.is_admin or ctx.is_system:
            logger.debug(
                "_check_permission: SKIPPED (admin/system bypass) - "
                "path=%s, permission=%s, user=%s",
                path,
                permission.name,
                ctx.user_id,
            )
            return

        logger.debug(
            "_check_permission: path=%s, permission=%s, user=%s, zone=%s",
            path,
            permission.name,
            ctx.user_id,
            getattr(ctx, "zone_id", None),
        )

        # ----------------------------------------------------------
        # Fix #332: Virtual parsed views inherit permissions from
        # their original files.
        # ----------------------------------------------------------
        from nexus.lib.virtual_views import parse_virtual_path

        def metadata_exists(check_path: str) -> bool:
            return bool(self._metadata_store.exists(check_path))

        original_path, view_type = parse_virtual_path(path, metadata_exists)
        if view_type == "md":
            logger.debug(
                "  -> Virtual view detected: checking permissions on original file %s",
                original_path,
            )
            permission_path = original_path
        else:
            permission_path = path

        # ----------------------------------------------------------
        # Issue #920: O(1) owner fast-path check
        # If the file has posix_uid set and it matches the requesting
        # user, skip the expensive ReBAC graph traversal.
        # ----------------------------------------------------------
        file_meta = (
            file_metadata
            if (file_metadata is not None and permission_path == path)
            else self._metadata_store.get(permission_path)
        )
        if file_meta and file_meta.owner_id:
            subject_id = ctx.subject_id or ctx.user_id
            if file_meta.owner_id == subject_id:
                logger.debug(
                    "  -> OWNER FAST-PATH: %s owns %s, skipping ReBAC",
                    subject_id,
                    permission_path,
                )
                return  # Owner has all permissions

        # ----------------------------------------------------------
        # ReBAC graph traversal via PermissionEnforcer
        # ----------------------------------------------------------
        if self._permission_enforcer is None:
            raise PermissionError(
                f"Access denied: Permission enforcer unavailable (ReBAC degraded), "
                f"cannot check {permission.name} permission for '{path}'"
            )
        result = self._permission_enforcer.check(permission_path, permission, ctx)
        logger.debug("  -> permission_enforcer.check returned: %s", result)

        if not result:
            raise PermissionError(
                f"Access denied: User '{ctx.user_id}' does not have "
                f"{permission.name} permission for '{path}'"
            )
