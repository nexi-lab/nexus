"""PermissionEnforcer — ReBAC permission enforcement for Nexus (v0.6.0+).

Canonical location: nexus.bricks.rebac.enforcer (Issue #1847).
"""

import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import OperationalError

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext, Permission


def check_stale_session(agent_registry: Any, context: OperationContext) -> None:
    """Check for stale agent sessions and raise if the session is outdated.

    Compares the agent_generation from the JWT token (stored in context) against
    the current generation in the process table. A mismatch means a newer
    session has superseded this one.

    Issue #1240 / #1445: Shared helper used by both sync and async enforcers.

    Args:
        agent_registry: AgentRegistry instance (or None to skip check).
        context: Operation context with agent_generation from JWT claims.

    Raises:
        StaleSessionError: If the session generation is stale or the agent
            record no longer exists (deleted agent with valid JWT).
    """
    if (
        agent_registry is None
        or context.agent_generation is None
        or context.subject_type != "agent"
    ):
        return

    agent_id = context.agent_id or context.subject_id
    if not agent_id:
        logger.warning("[STALE-SESSION] No agent_id in context, skipping check")
        return

    current_record = agent_registry.get(agent_id)

    from nexus.contracts.exceptions import StaleSessionError

    # Issue #1445: Agent deleted but JWT still valid → stale session
    if current_record is None:
        raise StaleSessionError(
            agent_id,
            f"Agent '{agent_id}' no longer exists (session generation "
            f"{context.agent_generation} is stale)",
        )

    if current_record.generation != context.agent_generation:
        raise StaleSessionError(
            agent_id,
            f"Session generation {context.agent_generation} is stale "
            f"(current: {current_record.generation})",
        )


if TYPE_CHECKING:
    from nexus.bricks.rebac.cache.boundary import PermissionBoundaryCache
    from nexus.bricks.rebac.hotspot_detector import HotspotDetector
    from nexus.bricks.rebac.manager import ReBACManager
    from nexus.bricks.rebac.namespace_manager import NamespaceManager
    from nexus.bricks.rebac.permissions_enhanced import AuditStore

logger = logging.getLogger(__name__)

SEQUENTIAL_DEPTH_THRESHOLD = 3
"""Paths with depth <= this use sequential ancestor checks; deeper paths use batch."""

SYSTEM_BYPASS_SCOPE = "/system/"
"""Prefix for system-bypass write/delete operations."""

SYSTEM_BYPASS_EXTRA_PREFIXES = ("/nexus/pipes/",)
"""Additional prefixes allowed for system write bypass (e.g. audit pipe)."""


class PermissionEnforcer:
    """Pure ReBAC permission enforcement for Nexus filesystem (v0.6.0+).

    Implements permission checking using ReBAC (Relationship-Based Access Control)
    based on Google Zanzibar principles.

    Permission checks:
    1. Admin/system bypass - Scoped bypass with capabilities and audit logging (P0-4)
    2. ReBAC relationship check - Check permission graph for relationships

    P0-4 Features:
    - Scoped admin bypass (requires capabilities)
    - System bypass limited to /system paths (except read)
    - Audit logging for all bypasses
    - Kill-switch to disable bypasses
    - Path-based allowlist for admin bypass

    Migration from v0.5.x:
        - ACL and UNIX permissions have been removed
        - All permissions must be defined as ReBAC relationships
        - Use rebac_create() to grant permissions instead of chmod/setfacl
    """

    def __init__(
        self,
        metadata_store: Any = None,
        acl_store: Any | None = None,  # Deprecated, kept for backward compatibility
        rebac_manager: "ReBACManager | None" = None,
        entity_registry: Any = None,  # Entity registry (reserved for future use)
        dlc: Any = None,  # DriverLifecycleCoordinator for backend refs + routing
        # P0-4: Enhanced features
        allow_admin_bypass: bool = False,  # P0-4: Kill-switch DEFAULT OFF for production security
        allow_system_bypass: bool = True,  # P0-4: System bypass still enabled (for service operations)
        audit_store: "AuditStore | None" = None,  # P0-4: Audit logging
        admin_bypass_paths: list[str] | None = None,  # P0-4: Scoped bypass (allowlist)
        # Issue #922: Permission boundary cache for O(1) inheritance checks
        boundary_cache: "PermissionBoundaryCache | None" = None,
        enable_boundary_cache: bool = True,
        # Issue #921: Hotspot detection for proactive cache prefetching
        hotspot_detector: "HotspotDetector | None" = None,
        enable_hotspot_tracking: bool = True,
        # Issue #1239: Per-subject namespace visibility (Agent OS Phase 0)
        namespace_manager: "NamespaceManager | None" = None,
        # Issue #1240: Process table for stale-session detection (Agent OS Phase 1)
        agent_registry: Any = None,
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
            entity_registry: Entity registry (reserved for future use)
            dlc: DriverLifecycleCoordinator for routing + backend refs
            allow_admin_bypass: Enable admin bypass (DEFAULT: False for security)
            allow_system_bypass: Enable system bypass (for internal operations)
            audit_store: Audit store for bypass logging
            admin_bypass_paths: Optional path allowlist for admin bypass (e.g., ["/admin/*"])
            boundary_cache: Permission boundary cache for O(1) inheritance (Issue #922)
            enable_boundary_cache: Enable boundary caching (default: True)
            hotspot_detector: HotspotDetector for access pattern tracking (Issue #921)
            enable_hotspot_tracking: Enable hotspot tracking (default: True)
            namespace_manager: NamespaceManager for per-subject visibility (Issue #1239)
            agent_registry: AgentRegistry for stale-session detection (Issue #1240)
        """
        self.metadata_store = metadata_store
        self.rebac_manager: ReBACManager | None = rebac_manager
        self.entity_registry = entity_registry  # v0.5.0 ACE
        self.dlc = dlc  # DLC for routing + backend refs

        # Issue #1239: Per-subject namespace visibility (Agent OS Phase 0)
        self.namespace_manager: NamespaceManager | None = namespace_manager

        # Issue #1240: Process table for stale-session detection (Agent OS Phase 1)
        self.agent_registry = agent_registry

        # P0-4: Enhanced features
        self.allow_admin_bypass = allow_admin_bypass
        self.allow_system_bypass = allow_system_bypass
        self.audit_store = audit_store
        self.admin_bypass_paths = admin_bypass_paths or []

        # Issue #899: Centralized cache coordinator for all permission caches
        from nexus.bricks.rebac.permission_cache import PermissionCacheCoordinator

        self._cache = PermissionCacheCoordinator(
            rebac_manager=rebac_manager,
            boundary_cache=boundary_cache,
            enable_boundary_cache=enable_boundary_cache,
            hotspot_detector=hotspot_detector,
            enable_hotspot_tracking=enable_hotspot_tracking,
        )

        # Backward-compatible properties that delegate to coordinator
        self._boundary_cache = self._cache.boundary_cache
        self._hotspot_detector = self._cache.hotspot_detector
        self._bitmap_completeness_cache = self._cache._bitmap_completeness_cache
        self._leopard_dir_index = self._cache._leopard_dir_index

        # Register boundary cache invalidation callback with rebac_manager
        if (
            self._boundary_cache
            and self.rebac_manager
            and hasattr(self.rebac_manager, "register_boundary_cache_invalidator")
        ):
            callback_id = f"permission_enforcer_{id(self)}"
            self.rebac_manager.register_boundary_cache_invalidator(
                callback_id,
                self._boundary_cache.invalidate_permission_change,
            )

        # Warn if ACL store is provided (deprecated)
        if acl_store is not None:
            import warnings

            warnings.warn(
                "acl_store parameter is deprecated and will be removed in v0.7.0. "
                "Use ReBAC for all permissions.",
                DeprecationWarning,
                stacklevel=2,
            )

    def check_owner(
        self,
        metadata: Any,
        context: "OperationContext",
    ) -> bool:
        """Kernel DAC: O(1) owner fast-path (Issue #920, #1825).

        Returns True if context subject matches metadata.owner_id.
        Moved from PermissionChecker to kernel contract so the kernel
        can call it directly before hook dispatch.
        """
        if metadata is not None and getattr(metadata, "owner_id", None):
            subject_id = context.subject_id or context.user_id
            if metadata.owner_id == subject_id:
                logger.debug(
                    f"  -> OWNER FAST-PATH: {subject_id} owns {getattr(metadata, 'path', '?')}, skipping ReBAC"
                )
                return True
        return False

    def invalidate_cache(
        self,
        subject_type: str | None = None,
        subject_id: str | None = None,
        zone_id: str | None = None,
    ) -> None:
        """Invalidate permission caches when permissions change.

        Should be called after write_tuple, delete_tuple, or bulk permission changes.

        Args:
            subject_type: If provided, only invalidate cache for this subject type
            subject_id: If provided, only invalidate cache for this subject
            zone_id: If provided, only invalidate cache for this zone
        """
        self._cache.invalidate(subject_type, subject_id, zone_id)
        logger.debug(
            f"[CACHE-INVALIDATE] Invalidated cache for "
            f"{(subject_type, subject_id, zone_id) if subject_type else 'all'}"
        )

    def has_accessible_descendants(
        self,
        prefix: str,
        context: OperationContext,
    ) -> bool:
        """Check if user has any accessible paths under the given prefix.

        Delegates to has_accessible_descendants_batch() for a single prefix.
        Uses Tiger bitmap + Rust binary search for O(log N) lookup.

        Args:
            prefix: Directory path prefix (e.g., "/skills/")
            context: Operation context with user information

        Returns:
            True if user can access any path starting with prefix
        """
        results = self.has_accessible_descendants_batch([prefix], context)
        return results.get(prefix, True)

    def has_accessible_descendants_batch(
        self,
        prefixes: list[str],
        context: OperationContext,
    ) -> dict[str, bool]:
        """Check if user has accessible paths under multiple prefixes at once.

        Loads Tiger bitmap ONCE and scans all prefixes in a single pass.
        This avoids the N+1 pattern of calling has_accessible_descendants()
        individually per directory (Issue #1298).

        Args:
            prefixes: List of directory path prefixes (e.g., ["/skills/", "/docs/"])
            context: Operation context with user information

        Returns:
            Dict mapping each prefix to True/False
        """
        if not prefixes:
            return {}

        start = time.time()
        tiger_cache = getattr(self.rebac_manager, "_tiger_cache", None)
        if tiger_cache is None:
            logger.debug("[BATCH-OPT] No Tiger cache, returning all True (fallback)")
            return dict.fromkeys(prefixes, True)

        try:
            subject = context.get_subject()
            subject_type, subject_id = subject
            zone_id = context.zone_id

            # Get accessible paths via Tiger cache public API (Issue #1565)
            # Fix(#3709): was get_accessible_paths_list (non-existent method)
            accessible_paths = tiger_cache.get_accessible_paths(
                subject_type=subject_type,
                subject_id=subject_id,
                permission="read",
                resource_type="file",
                zone_id=zone_id,
            )

            if accessible_paths is None:
                # Fix(#3709): Tiger cache miss must fail-closed (deny), not
                # fail-open.  Returning all-True here would grant access to
                # every directory prefix for any user whose bitmap hasn't
                # been materialized yet.
                logger.debug(
                    f"[BATCH-OPT] No bitmap for {subject_type}:{subject_id}, "
                    f"returning all False for {len(prefixes)} prefixes (fail-closed)"
                )
                return dict.fromkeys(prefixes, False)

            if not accessible_paths:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[BATCH-OPT] Empty paths for {subject_type}:{subject_id}")
                return dict.fromkeys(prefixes, False)

            # RUST_FALLBACK: rebac enforcer — nexus_kernel for batch permission checks
            # Try Rust-accelerated prefix matching (Issue #1565)
            try:
                import nexus_kernel

                results_list = nexus_kernel.batch_prefix_check(
                    list(accessible_paths), list(prefixes)
                )
                results = dict(zip(prefixes, results_list, strict=True))
            except (ImportError, AttributeError):
                # Python fallback (same logic, O(N×M))
                results = {}
                for prefix in prefixes:
                    prefix_normalized = prefix.rstrip("/") + "/"
                    prefix_exact = prefix.rstrip("/")
                    results[prefix] = any(
                        p.startswith(prefix_normalized) or p == prefix_exact
                        for p in accessible_paths
                    )

            elapsed = (time.time() - start) * 1000
            found_count = sum(1 for v in results.values() if v)
            logger.debug(
                f"[BATCH-OPT] has_accessible_descendants_batch: "
                f"{found_count}/{len(prefixes)} accessible in {elapsed:.1f}ms "
                f"(paths: {len(accessible_paths)})"
            )
            return results

        except (OperationalError, TimeoutError, OSError, RuntimeError, AttributeError) as e:
            logger.warning(
                "[BATCH-OPT] has_accessible_descendants_batch error for "
                "zone=%s subject=%s prefix_count=%d: %s, returning all False (fail-closed)",
                context.zone_id,
                getattr(context, "user_id", "?"),
                len(prefixes),
                e,
                exc_info=True,
            )
            # Fail-closed: deny access on error (security-critical)
            return dict.fromkeys(prefixes, False)

    def check(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check if user has permission to perform operation on file.

        Permission check with scoped admin/system bypass and audit logging (P0-4):
        1. System bypass (limited scope) - Read: any path, Write/Delete: /system/* only
        2. Admin bypass (capability-based) - Requires capabilities and optional path allowlist
        3. ReBAC relationship check - Check permission graph

        Args:
            path: Virtual file path
            permission: Permission to check (READ, WRITE, EXECUTE)
            context: Operation context with user/group information

        Returns:
            True if permission is granted, False otherwise

        Examples:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> ctx = OperationContext(user_id="alice", groups=["developers"])
            >>> enforcer.check("/workspace/file.txt", Permission.READ, ctx)
            True
        """
        logger.debug(
            f"[PermissionEnforcer.check] path={path}, perm={permission.name}, user={context.user_id}, is_admin={context.is_admin}, is_system={context.is_system}"
        )

        # Map Permission enum to string
        permission_str = self._permission_to_string(permission)

        # P0-4: System bypass (limited scope)
        if context.is_system:
            if not self.allow_system_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "system", "kill_switch_disabled"
                )
                raise PermissionError("System bypass disabled by configuration")

            if not self._is_allowed_system_operation(path, permission_str):
                self._log_bypass_denied(context, path, permission_str, "system", "scope_limit")
                raise PermissionError(f"System bypass not allowed for {path}")

            self._log_bypass(context, path, permission_str, "system", allowed=True)
            return True

        # P0-4: Admin bypass (capability-based + path-scoped)
        if context.is_admin:
            if not self.allow_admin_bypass:
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "kill_switch_disabled"
                )
                # Fall through to ReBAC check instead of denying
                return self._check_rebac(path, permission, context)

            # P0-4: Check path-based allowlist (scoped bypass)
            if self.admin_bypass_paths and not self._path_matches_allowlist(
                path, self.admin_bypass_paths
            ):
                self._log_bypass_denied(
                    context, path, permission_str, "admin", "path_not_in_allowlist"
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            # Import AdminCapability here to avoid circular imports
            from nexus.bricks.rebac.permissions_enhanced import AdminCapability

            # P0-4: Zone boundary check (security fix for issue #819)
            # Extract zone from path (format: /zone/{zone_id}/...)
            path_zone_id = None
            if path.startswith("/zone/"):
                parts = path[6:].split("/", 1)  # Remove "/zone/" prefix
                if parts:
                    path_zone_id = parts[0]

            # Check if admin is attempting cross-zone access
            if (
                path_zone_id
                and context.zone_id
                and path_zone_id != context.zone_id
                and AdminCapability.MANAGE_ZONES not in context.admin_capabilities
            ):
                # Cross-zone access requires MANAGE_ZONES capability (system admin only)
                # Not system admin - deny cross-zone access
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"cross_zone_access_denied_path_zone={path_zone_id}_context_zone={context.zone_id}",
                )
                # Immediately raise PermissionError for cross-zone access violation
                raise PermissionError(
                    f"Access denied: Cross-zone access requires MANAGE_ZONES capability. "
                    f"Context zone: {context.zone_id}, Path zone: {path_zone_id}"
                )

            required_capability = AdminCapability.get_required_capability(path, permission_str)
            wildcard_capability = f"admin:{permission_str}:*"

            # Check if user has EITHER the path-specific capability OR the wildcard capability
            # Wildcard capability (admin:read:*) grants access to ALL paths
            has_feature = (
                required_capability in context.admin_capabilities
                or wildcard_capability in context.admin_capabilities
            )

            if not has_feature:
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"missing_capability_{required_capability}",
                )
                # Fall through to ReBAC check
                return self._check_rebac(path, permission, context)

            self._log_bypass(context, path, permission_str, "admin", allowed=True)
            return True

        # Issue #1239: Namespace visibility check (Agent OS Phase 0)
        # Unmounted paths are invisible (404 Not Found), not denied (403 Forbidden).
        # This runs AFTER admin/system bypass (admins see everything) and BEFORE
        # fine-grained ReBAC check (defense in depth).
        if self.namespace_manager is not None:
            subject = context.get_subject()
            if not self.namespace_manager.is_visible(subject, path, context.zone_id):
                from nexus.contracts.exceptions import NexusFileNotFoundError

                raise NexusFileNotFoundError(
                    path=path,
                    message="Path not found",  # Intentionally vague — path is invisible
                )

        # Issue #1240 / #1445: Stale-session detection (Agent OS Phase 1)
        check_stale_session(self.agent_registry, context)

        # Normal ReBAC check
        return self._check_rebac(path, permission, context)

    def _check_rebac(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check ReBAC relationships for permission.

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Operation context

        Returns:
            True if ReBAC grants permission, False otherwise
        """
        logger.debug(
            f"[_check_rebac] path={path}, permission={permission}, context.user_id={context.user_id}"
        )

        if not self.rebac_manager:
            # No ReBAC manager - deny by default
            # This ensures security: must explicitly configure ReBAC
            logger.debug("  -> DENY (no rebac_manager)")
            return False

        # Map Permission flags to string permission names
        permission_name = self._permission_to_string(permission)
        if permission_name == "unknown":
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(f"  -> DENY (unknown permission: {permission})")
            return False

        # Get backend-specific object type for ReBAC check
        # This allows different backends (Postgres, Redis, etc.) to have different permission models
        object_type = "file"  # Default
        # Unscope zone-prefixed paths so object_id matches how ReBAC tuples
        # are created (non-prefixed path + zone_id field for isolation).
        from nexus.core.path_utils import unscope_internal_path

        object_id = unscope_internal_path(path)  # Strip /zone/{id}/ prefix

        if self.dlc:
            try:
                resolved = self.dlc.resolve_path(path, context.zone_id or ROOT_ZONE_ID)
                if resolved is not None:
                    backend, backend_path, mount_point = resolved
                    from nexus.bricks.rebac.object_type_mapper import ObjectTypeMapper

                    mapper = ObjectTypeMapper()
                    object_type = mapper.get_object_type(backend, backend_path)
                    object_id = unscope_internal_path(
                        mapper.get_object_id(
                            backend,
                            backend_path,
                            virtual_path=path,
                            object_type=object_type,
                        )
                    )
            except (KeyError, AttributeError, LookupError, RuntimeError) as e:
                # If routing fails, fall back to default "file" type with virtual path
                logger.warning(
                    "[_check_rebac] Failed to route path=%s zone=%s for object type: %s, "
                    "using default 'file'",
                    path,
                    context.zone_id,
                    e,
                    exc_info=True,
                )

        # Check ReBAC permission using backend-provided object type
        # P0-4: Pass zone_id for multi-zone isolation
        zone_id = context.zone_id or ROOT_ZONE_ID
        subject = context.get_subject()

        logger.debug(
            f"[_check_rebac] Calling rebac_check: subject={subject}, permission={permission_name}, object=('{object_type}', '{object_id}'), zone_id={zone_id}"
        )

        # Issue #921: Record access for hotspot detection (before cache/graph check)
        # This tracks access patterns to enable proactive cache prefetching
        if self._hotspot_detector:
            self._hotspot_detector.record_access(
                subject_type=subject[0],
                subject_id=subject[1],
                resource_type=object_type,
                permission=permission_name,
                zone_id=zone_id,
            )

        # NOTE: Removed implicit directory TRAVERSE optimization (was incorrectly granting
        # access to ALL authenticated users for ANY implicit directory, violating Unix semantics)
        # Correct behavior: user should only see a directory if they have access to at least
        # one file inside it. This is handled by _has_descendant_access in the listing code.

        # Issue #899: Adaptive batch vs sequential ancestor resolution.
        # For shallow paths (depth <= 3), sequential is fine (1-3 queries).
        # For deeper paths, batch all checks into one rebac_check_bulk() call.
        # Threshold 3 avoids rebac_check_bulk SQLite race on some platforms.
        depth = object_id.count("/") if object_id else 0

        if depth <= SEQUENTIAL_DEPTH_THRESHOLD or permission_name not in (
            "read",
            "write",
            "traverse",
        ):
            return self._check_rebac_sequential(
                subject, permission_name, object_type, object_id, zone_id
            )

        return self._check_rebac_batched(subject, permission_name, object_type, object_id, zone_id)

    def _check_rebac_sequential(
        self,
        subject: tuple[str, str],
        permission_name: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool:
        """Sequential permission check for shallow paths (depth <= 3).

        Performs direct check, TRAVERSE implication, boundary cache lookup,
        and parent walk one level at a time. Efficient when only 1-2 parents.
        """
        assert self.rebac_manager is not None  # guaranteed by _check_rebac guard

        # 1. Direct permission check
        result = self.rebac_manager.rebac_check(
            subject=subject,
            permission=permission_name,
            object=(object_type, object_id),
            zone_id=zone_id,
        )
        if result:
            return True

        # 2. TRAVERSE implied by READ/WRITE
        if permission_name == "traverse":
            for implied_perm in ("read", "write"):
                if self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=implied_perm,
                    object=(object_type, object_id),
                    zone_id=zone_id,
                ):
                    logger.debug(
                        f"[_check_rebac] ALLOW TRAVERSE (has {implied_perm.upper()} permission)"
                    )
                    return True

        # 3. Parent directory inheritance (sequential walk)
        if permission_name in ("read", "write", "traverse") and object_id:
            subject_type, subject_id = subject

            # FAST PATH: Boundary cache (Issue #922)
            if self._boundary_cache:
                boundary = self._boundary_cache.get_boundary(
                    zone_id, subject_type, subject_id, permission_name, object_id
                )
                if boundary:
                    boundary_result = self.rebac_manager.rebac_check(
                        subject=subject,
                        permission=permission_name,
                        object=(object_type, boundary),
                        zone_id=zone_id,
                    )
                    if boundary_result:
                        logger.debug(
                            f"[_check_rebac] ALLOW (boundary cache hit: {object_id} → {boundary})"
                        )
                        return True
                    self._boundary_cache.invalidate_permission_change(
                        zone_id, subject_type, subject_id, permission_name, boundary
                    )

            # SLOW PATH: Walk up the directory tree
            parent_path = object_id
            while parent_path and parent_path != "/":
                parent_path = os.path.dirname(parent_path)
                if not parent_path or parent_path == object_id:
                    parent_path = "/"

                parent_result = self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=permission_name,
                    object=(object_type, parent_path),
                    zone_id=zone_id,
                )
                if parent_result:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(f"[_check_rebac] ALLOW (inherited from parent: {parent_path})")
                    if self._boundary_cache:
                        self._boundary_cache.set_boundary(
                            zone_id,
                            subject_type,
                            subject_id,
                            permission_name,
                            object_id,
                            parent_path,
                        )
                    return True

                if parent_path == "/":
                    break

        return False

    def _check_rebac_batched(
        self,
        subject: tuple[str, str],
        permission_name: str,
        object_type: str,
        object_id: str,
        zone_id: str,
    ) -> bool:
        """Batch permission check for deep paths (depth > 2) (Issue #899).

        Collects all check variants (direct, implied TRAVERSE, all ancestors)
        and resolves them in a single rebac_check_bulk() call instead of O(D)
        sequential queries.
        """
        assert self.rebac_manager is not None  # guaranteed by _check_rebac guard

        subject_type, subject_id = subject

        # FAST PATH: Boundary cache (Issue #922)
        if self._boundary_cache:
            boundary = self._boundary_cache.get_boundary(
                zone_id, subject_type, subject_id, permission_name, object_id
            )
            if boundary:
                boundary_result = self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=permission_name,
                    object=(object_type, boundary),
                    zone_id=zone_id,
                )
                if boundary_result:
                    logger.debug(
                        f"[_check_rebac_batched] ALLOW (boundary cache hit: "
                        f"{object_id} → {boundary})"
                    )
                    return True
                self._boundary_cache.invalidate_permission_change(
                    zone_id, subject_type, subject_id, permission_name, boundary
                )

        # Collect ALL checks for a single rebac_check_bulk() call
        from nexus.bricks.rebac._path_utils import get_ancestors

        ancestors = get_ancestors(object_id)
        checks: list[tuple[tuple[str, str], str, tuple[str, str]]] = []

        # Direct permission on the object itself
        checks.append((subject, permission_name, (object_type, object_id)))

        # TRAVERSE implied by READ/WRITE on the object
        if permission_name == "traverse":
            checks.append((subject, "read", (object_type, object_id)))
            checks.append((subject, "write", (object_type, object_id)))

        # All ancestor checks for permission inheritance
        # get_ancestors() excludes root "/", so add it explicitly
        for ancestor in (*ancestors, "/"):
            if ancestor != object_id:
                checks.append((subject, permission_name, (object_type, ancestor)))
                # TRAVERSE implication also applies to ancestors
                if permission_name == "traverse":
                    checks.append((subject, "read", (object_type, ancestor)))
                    checks.append((subject, "write", (object_type, ancestor)))

        # ONE bulk call instead of O(D) sequential calls
        results = self.rebac_manager.rebac_check_bulk(checks, zone_id=zone_id)

        # Process results: direct > implied > inherited (priority order)
        for check in checks:
            if results.get(check, False):
                granting_path = check[2][1]  # object_id from the check tuple
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(f"[_check_rebac_batched] ALLOW via {check[1]} on {granting_path}")
                # Update boundary cache if granted via an ancestor
                if self._boundary_cache and granting_path != object_id:
                    self._boundary_cache.set_boundary(
                        zone_id,
                        subject_type,
                        subject_id,
                        permission_name,
                        object_id,
                        granting_path,
                    )
                return True

        logger.debug(
            f"[_check_rebac_batched] DENY {permission_name} on {object_id} "
            f"(checked {len(checks)} tuples across {len(ancestors)} ancestors)"
        )
        return False

    def _is_allowed_system_operation(self, path: str, permission: str) -> bool:
        """Check if system bypass is allowed for this operation (P0-4).

        System bypass is limited to:
        - Read operations on any path (for auto-parse indexing)
        - Read, write, execute, delete operations on /system/* paths only

        Args:
            path: File path
            permission: Permission type

        Returns:
            True if system bypass is allowed
        """
        # Allow read operations on any path (for auto-parse and other system reads)
        if permission == "read":
            return True

        # For other operations, only allow /system paths and approved extras
        # Use strict matching: /system/ or exactly /system (not /systemdata, etc.)
        allowed = (
            path.startswith(SYSTEM_BYPASS_SCOPE)
            or path == SYSTEM_BYPASS_SCOPE.rstrip("/")
            or any(path.startswith(p) for p in SYSTEM_BYPASS_EXTRA_PREFIXES)
        )
        if not allowed:
            return False

        # Allow common operations on /system paths
        return permission in ["write", "execute", "delete"]

    def _log_bypass(
        self,
        context: OperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        allowed: bool,
    ) -> None:
        """Log admin/system bypass to audit store (P0-4)."""
        if not self.audit_store:
            return

        from datetime import UTC, datetime

        from nexus.bricks.rebac.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user_id=context.user_id,
            zone_id=context.zone_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=allowed,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
        )

        self.audit_store.log_bypass(entry)

    def _log_bypass_denied(
        self,
        context: OperationContext,
        path: str,
        permission: str,
        bypass_type: str,
        reason: str,
    ) -> None:
        """Log denied bypass attempt (P0-4)."""
        if not self.audit_store:
            return

        from datetime import UTC, datetime

        from nexus.bricks.rebac.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user_id=context.user_id,
            zone_id=context.zone_id,
            path=path,
            permission=permission,
            bypass_type=bypass_type,
            allowed=False,
            capabilities=sorted(getattr(context, "admin_capabilities", [])),
            denial_reason=reason,
        )

        self.audit_store.log_bypass(entry)

    def _permission_to_string(self, permission: Permission) -> str:
        """Convert Permission enum to string."""
        if permission & Permission.READ:
            return "read"
        elif permission & Permission.WRITE:
            return "write"
        elif permission & Permission.EXECUTE:
            return "execute"
        elif permission & Permission.TRAVERSE:
            return "traverse"
        elif permission & Permission.NONE:
            return "none"
        else:
            return "unknown"

    def _path_matches_allowlist(self, path: str, allowlist: list[str]) -> bool:
        """Check if path matches any pattern in allowlist.

        P0-4: Scoped admin bypass - only allow admin bypass for specific paths

        Args:
            path: File path to check
            allowlist: List of path patterns (supports wildcards: /admin/*, /workspace/*)

        Returns:
            True if path matches any allowlist pattern
        """
        import fnmatch

        return any(fnmatch.fnmatch(path, pat) for pat in allowlist)

    def get_boundary_cache_stats(self) -> dict[str, Any] | None:
        """Get permission boundary cache statistics (Issue #922).

        Returns statistics about the boundary cache including hit rate,
        miss rate, and number of cached entries.

        Returns:
            Dictionary with cache statistics, or None if boundary cache is disabled

        Example:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> stats = enforcer.get_boundary_cache_stats()
            >>> print(f"Hit rate: {stats['hit_rate_percent']}%")
        """
        if self._boundary_cache is None:
            return None
        return self._boundary_cache.get_stats()

    def reset_boundary_cache_stats(self) -> None:
        """Reset permission boundary cache statistics (Issue #922)."""
        if self._boundary_cache is not None:
            self._boundary_cache.reset_stats()

    def clear_boundary_cache(self) -> None:
        """Clear all entries from the permission boundary cache (Issue #922)."""
        if self._boundary_cache is not None:
            self._boundary_cache.clear()

    @property
    def hotspot_detector(self) -> "HotspotDetector | None":
        """Get the hotspot detector instance (Issue #921).

        Returns:
            HotspotDetector instance, or None if hotspot tracking is disabled
        """
        return self._hotspot_detector

    def get_hotspot_stats(self) -> dict[str, Any] | None:
        """Get hotspot detection statistics (Issue #921).

        Returns statistics about access pattern tracking including
        hot entries count, total accesses, and prefetch triggers.

        Returns:
            Dictionary with hotspot statistics, or None if disabled

        Example:
            >>> enforcer = PermissionEnforcer(metadata_store, rebac_manager=rebac)
            >>> stats = enforcer.get_hotspot_stats()
            >>> print(f"Hot entries: {stats['hot_entries_detected']}")
        """
        if self._hotspot_detector is None:
            return None
        return self._hotspot_detector.get_stats()

    def get_hot_entries(self, limit: int | None = 10) -> list[Any] | None:
        """Get current hot permission entries (Issue #921).

        Returns list of HotspotEntry objects representing frequently
        accessed permission paths.

        Args:
            limit: Maximum number of entries to return (default: 10)

        Returns:
            List of HotspotEntry objects, or None if disabled
        """
        if self._hotspot_detector is None:
            return None
        return self._hotspot_detector.get_hot_entries(limit=limit)

    def filter_list(
        self,
        paths: list[str],
        context: OperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission (Issue #899 strategy chain).

        Uses a composable strategy chain for performance:
        1. Tiger bitmap (O(1) per path)
        2. Leopard directory index (cached dir grants)
        3. Hierarchy pre-filter (batch parent checks)
        4. Zone pre-filter (cross-zone elimination)
        5. Bulk ReBAC (final fallback)

        Args:
            paths: List of paths to filter
            context: Operation context

        Returns:
            Filtered list of paths user can read
        """
        # Admin/system bypass
        if (context.is_admin and self.allow_admin_bypass) or (
            context.is_system and self.allow_system_bypass
        ):
            return paths

        # Issue #1239 + #1244: Namespace pre-filter
        if self.namespace_manager is not None:
            subject = context.get_subject()
            paths = self.namespace_manager.filter_visible(subject, paths, context.zone_id)
            if not paths:
                return []

        # Use strategy chain if rebac_manager supports bulk checks
        if self.rebac_manager and hasattr(self.rebac_manager, "rebac_check_bulk"):
            overall_start = time.time()
            zone_id = context.zone_id or ROOT_ZONE_ID
            subject = context.get_subject()

            logger.debug(
                f"[PERF-FILTER] filter_list START: {len(paths)} paths, "
                f"subject={subject}, zone={zone_id}"
            )

            from nexus.bricks.rebac.permission_filter_chain import (
                FilterContext,
                run_filter_chain,
            )

            filter_ctx = FilterContext(
                paths=paths,
                subject=subject,
                zone_id=zone_id,
                context=context,
                cache=self._cache,
                rebac_manager=self.rebac_manager,
                dlc=self.dlc,
            )

            filtered = run_filter_chain(filter_ctx)

            overall_elapsed = time.time() - overall_start
            logger.info(
                f"[PERF-FILTER] filter_list DONE: {overall_elapsed:.3f}s total, "
                f"allowed {len(filtered)}/{len(paths)} paths"
            )
            return filtered

        # Fallback: Filter by ReBAC permissions individually
        result = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                result.append(path)

        return result

    def filter_search_results(
        self,
        paths: list[str],
        *,
        user_id: str,
        zone_id: str,
        is_admin: bool = False,
    ) -> list[str]:
        """Filter search result paths by read permission via bulk check.

        Unlike filter_list(), this method:
        1. Skips the NamespaceManager pre-filter (search paths lack mount entries)
        2. Uses compute_permissions_bulk with fresh graph (unique tuple_version)
           to check only the N search result paths — O(N) not O(total_zone_tuples)

        Performance: 1 SQL query + 1 Rust graph build + N permission checks.
        Scales with search result count, NOT zone size.

        Args:
            paths: Absolute paths from search results
            user_id: The authenticated user's ID
            zone_id: Zone ID for isolation
            is_admin: Whether the user is an admin (bypasses checks)

        Returns:
            Filtered list of paths the user can read
        """
        if not paths:
            return []

        # Admin bypass
        if is_admin and self.allow_admin_bypass:
            return paths

        if self.rebac_manager is None:
            return []  # fail-closed: no manager = no access

        try:
            return self._filter_search_bulk(paths, user_id=user_id, zone_id=zone_id)
        except Exception:
            logger.warning(
                "filter_search_results failed, denying all (fail-closed)",
                exc_info=True,
            )
            return []  # fail-closed

    def _filter_search_bulk(
        self,
        paths: list[str],
        *,
        user_id: str,
        zone_id: str,
    ) -> list[str]:
        """Check only the given paths via Rust bulk permission check.

        Uses a unique tuple_version (time_ns) to force a fresh graph build,
        bypassing the stale GRAPH_CACHE bug in compute_permissions_bulk.
        """
        import time as time_module

        from nexus.bricks.rebac.utils.fast import check_permissions_bulk_with_fallback

        assert self.rebac_manager is not None  # caller already checked

        # Fetch tuples for zone (1 SQL query) — includes cross-zone for user
        tuples = self.rebac_manager._fetch_tuples_for_zone(
            zone_id, include_cross_zone_for_user=user_id
        )
        namespace_configs = self.rebac_manager._get_namespace_configs_dict()

        # Build checks: [(subject, permission, object), ...]
        subject = ("user", user_id)
        checks = [(subject, "read", ("file", p)) for p in paths]

        # Use unique tuple_version to force fresh Rust graph (bypass stale cache)
        results = check_permissions_bulk_with_fallback(
            checks=checks,
            tuples=tuples,
            namespace_configs=namespace_configs,
            tuple_version=time_module.time_ns(),
        )

        # results is dict[(subj_type, subj_id, perm, obj_type, obj_id) -> bool]
        return [p for p in paths if results.get(("user", user_id, "read", "file", p), False)]
