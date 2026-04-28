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

from nexus.contracts.constants import ROOT_ZONE_ID
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
                f"_check_permission: SKIPPED (admin/system bypass) - "
                f"path={path}, permission={permission.name}, user={ctx.user_id}"
            )
            return

        logger.debug(
            f"_check_permission: path={path}, permission={permission.name}, "
            f"user={ctx.user_id}, zone={getattr(ctx, 'zone_id', None)}"
        )

        # ----------------------------------------------------------
        # Fix #332: Virtual parsed views inherit permissions from
        # their original files.
        # ----------------------------------------------------------
        from nexus.lib.virtual_views import parse_virtual_path

        def metadata_exists(check_path: str) -> bool:
            return bool(self._metadata_store.exists(check_path))

        original_path, view_type, _ = parse_virtual_path(path, metadata_exists)
        if view_type == "md":
            logger.debug(
                f"  -> Virtual view detected: checking permissions on original file {original_path}"
            )
            permission_path = original_path
        else:
            permission_path = path

        # ----------------------------------------------------------
        # Issue #3786 / Codex Round 9 #1 + Round 10 #1: federation-token
        # zone_perms tripwire BEFORE the owner fast-path.  Owner-on-file
        # would otherwise short-circuit authorization for /zone/X paths
        # the token never had grants on (or had only read-only grants
        # for, on a WRITE request).  Round 10 — runs for both root-zoned
        # multi-zone tokens *and* single-zone tokens whose ``zone_id`` is
        # the concrete zone but whose ``zone_perms`` entry is read-only.
        # ----------------------------------------------------------
        if self._permission_enforcer is not None:
            try:
                _eff_zp = self._permission_enforcer._effective_zone_perms(ctx)
            except Exception:
                _eff_zp = ctx.zone_perms or ()
            _real_zp = tuple((z, p) for z, p in (_eff_zp or ()) if z != ROOT_ZONE_ID)
            if _real_zp and permission_path.startswith("/zone/"):
                _parts = permission_path[6:].split("/", 1)
                _path_zone = _parts[0] if _parts else ""
                if _path_zone and _path_zone != ROOT_ZONE_ID:
                    _allowed = False
                    _perm_char = "w" if permission == Permission.WRITE else "r"
                    for _z, _perms in _real_zp:
                        if _z == _path_zone:
                            if _perm_char in _perms or "x" in _perms:
                                _allowed = True
                            else:
                                raise PermissionError(
                                    f"Access denied: zone {_path_zone!r} is "
                                    f"read-only for this token (path '{path}')"
                                )
                            break
                    if not _allowed:
                        raise PermissionError(
                            f"Access denied: zone {_path_zone!r} not in token's "
                            f"zone_perms allow-list (path '{path}')"
                        )

        # ----------------------------------------------------------
        # Issue #920 / #1825: O(1) owner fast-path via kernel contract.
        # Delegates to PermissionEnforcerProtocol.check_owner() so the
        # check lives on the kernel contract, not in this service class.
        # ----------------------------------------------------------
        if self._permission_enforcer is not None:
            _owner_meta = (
                file_metadata
                if (file_metadata is not None and permission_path == path)
                else self._metadata_store.get(permission_path)
            )
            if self._permission_enforcer.check_owner(_owner_meta, ctx):
                return  # Owner has all permissions — skip ReBAC

        # ----------------------------------------------------------
        # ReBAC graph traversal via PermissionEnforcer
        # ----------------------------------------------------------
        if self._permission_enforcer is None:
            raise PermissionError(
                f"Access denied: Permission enforcer unavailable (ReBAC degraded), "
                f"cannot check {permission.name} permission for '{path}'"
            )
        result = self._permission_enforcer.check(permission_path, permission, ctx)
        logger.debug(f"  -> permission_enforcer.check returned: {result}")

        if not result:
            raise PermissionError(
                f"Access denied: User '{ctx.user_id}' does not have "
                f"{permission.name} permission for '{path}'"
            )
