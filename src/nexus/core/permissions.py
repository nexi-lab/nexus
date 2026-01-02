"""ReBAC permission enforcement for Nexus (v0.6.0+).

This module implements pure ReBAC (Relationship-Based Access Control)
based on Google Zanzibar principles. All UNIX-style permission classes
have been removed as of v0.6.0.

Permission Model:
    - Subject: (type, id) tuple (e.g., ("user", "alice"), ("agent", "bot"))
    - Relation: Direct relations (direct_owner, direct_editor, direct_viewer)
    - Object: (type, id) tuple (e.g., ("file", "/path"), ("workspace", "ws1"))
    - Permission: Computed from relations (read, write, execute)

All permissions are now managed through ReBAC relationships.
Use rebac_create() to grant permissions instead of chmod/chown.
"""

from __future__ import annotations

import logging
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntFlag
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.hotspot_detector import HotspotDetector
    from nexus.core.permission_boundary_cache import PermissionBoundaryCache
    from nexus.core.permissions_enhanced import AuditStore
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager

logger = logging.getLogger(__name__)


class Permission(IntFlag):
    """Permission flags for file operations.

    Note: These are still IntFlag for backward compatibility with
    bit operations, but they map to ReBAC permissions:
    - READ → "read" permission
    - WRITE → "write" permission
    - EXECUTE → "execute" permission
    - TRAVERSE → "traverse" permission (can stat/access by name, but not list contents)

    TRAVERSE is similar to Unix execute permission on directories - it allows
    accessing a path by name without the ability to list its contents.
    This enables O(1) permission checks for path traversal in FUSE operations.
    """

    NONE = 0
    EXECUTE = 1  # x
    WRITE = 2  # w
    READ = 4  # r
    TRAVERSE = 8  # t - can traverse/stat but not list (like Unix x on directories)
    ALL = 7  # rwx (does not include TRAVERSE by default)
    ALL_WITH_TRAVERSE = 15  # rwxt


@dataclass
class OperationContext:
    """Context for file operations with subject identity (v0.5.0).

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    v0.5.0 ACE: Unified agent identity system
    - user_id: Human owner (always tracked)
    - agent_id: Agent identity (optional)
    - subject_type: "user" or "agent" (for authentication)
    - subject_id: Actual identity (user_id or agent_id)

    Agent lifecycle managed via API key TTL (no agent_type field needed).

    Subject-based identity supports:
    - user: Human users (alice, bob)
    - agent: AI agents (claude_001, gpt4_agent)
    - service: Backend services (backup_service, indexer)
    - session: Temporary sessions (session_abc123)

    Attributes:
        user: Subject ID performing the operation (LEGACY: use user_id)
        user_id: Human owner ID (v0.5.0: NEW, for explicit tracking)
        agent_id: Agent ID if operation is from agent (optional)
        subject_type: Type of subject (user, agent, service, session)
        subject_id: Unique identifier for the subject
        groups: List of group IDs the subject belongs to
        tenant_id: Tenant/organization ID for multi-tenant isolation (optional)
        is_admin: Whether the subject has admin privileges
        is_system: Whether this is a system operation (bypasses all checks)
        admin_capabilities: Set of granted admin capabilities (P0-4)
        request_id: Unique ID for audit trail correlation (P0-4)
        backend_path: Backend-relative path for connector backends (optional)

    Examples:
        >>> # Human user context
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     groups=["developers"],
        ...     tenant_id="org_acme"
        ... )
        >>> # User-authenticated agent (uses user's auth)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="notebook_xyz",
        ...     subject_type="user",  # Authenticates as user
        ...     groups=[]
        ... )
        >>> # Agent-authenticated (has own API key)
        >>> ctx = OperationContext(
        ...     user="alice",
        ...     agent_id="agent_data_analyst",
        ...     subject_type="agent",  # Authenticates as agent
        ...     subject_id="agent_data_analyst",
        ...     groups=[]
        ... )
    """

    user: str  # LEGACY: Kept for backward compatibility (maps to user_id)
    groups: list[str]
    tenant_id: str | None = None
    agent_id: str | None = None  # Agent identity (optional)
    is_admin: bool = False
    is_system: bool = False

    # v0.5.0 ACE: Unified agent identity
    user_id: str | None = None  # NEW: Human owner (auto-populated from user if None)

    # P0-2: Subject-based identity
    subject_type: str = "user"  # Default to "user" for backward compatibility
    subject_id: str | None = None  # If None, uses self.user

    # P0-4: Admin capabilities and audit trail
    admin_capabilities: set[str] = field(default_factory=set)  # Scoped admin capabilities
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))  # Audit trail correlation ID

    # Backend path for path-based connectors (GCS, S3, etc.)
    backend_path: str | None = None  # Backend-relative path for connector backends
    virtual_path: str | None = None  # Full virtual path with mount prefix (for cache keys)

    def __post_init__(self) -> None:
        """Validate context and apply defaults."""
        # v0.5.0: Auto-populate user_id from user if not provided
        if self.user_id is None:
            self.user_id = self.user

        # P0-2: If subject_id not provided, use user field for backward compatibility
        if self.subject_id is None:
            self.subject_id = self.user

        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC.

        Returns properly typed subject for permission checking.

        Returns:
            Tuple of (subject_type, subject_id)

        Example:
            >>> ctx = OperationContext(user="alice", groups=[])
            >>> ctx.get_subject()
            ('user', 'alice')
            >>> ctx = OperationContext(
            ...     user="alice",
            ...     agent_id="agent_data_analyst",
            ...     subject_type="agent",
            ...     subject_id="agent_data_analyst",
            ...     groups=[]
            ... )
            >>> ctx.get_subject()
            ('agent', 'agent_data_analyst')
        """
        return (self.subject_type, self.subject_id or self.user)


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
        rebac_manager: EnhancedReBACManager | None = None,
        entity_registry: Any = None,  # Entity registry (reserved for future use)
        router: Any = None,  # PathRouter for backend object type resolution
        # P0-4: Enhanced features
        allow_admin_bypass: bool = False,  # P0-4: Kill-switch DEFAULT OFF for production security
        allow_system_bypass: bool = True,  # P0-4: System bypass still enabled (for service operations)
        audit_store: AuditStore | None = None,  # P0-4: Audit logging
        admin_bypass_paths: list[str] | None = None,  # P0-4: Scoped bypass (allowlist)
        # Issue #922: Permission boundary cache for O(1) inheritance checks
        boundary_cache: PermissionBoundaryCache | None = None,
        enable_boundary_cache: bool = True,
        # Issue #921: Hotspot detection for proactive cache prefetching
        hotspot_detector: HotspotDetector | None = None,
        enable_hotspot_tracking: bool = True,
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
            entity_registry: Entity registry (reserved for future use)
            router: PathRouter for resolving backend object types (v0.5.0+)
            allow_admin_bypass: Enable admin bypass (DEFAULT: False for security)
            allow_system_bypass: Enable system bypass (for internal operations)
            audit_store: Audit store for bypass logging
            admin_bypass_paths: Optional path allowlist for admin bypass (e.g., ["/admin/*"])
            boundary_cache: Permission boundary cache for O(1) inheritance (Issue #922)
            enable_boundary_cache: Enable boundary caching (default: True)
            hotspot_detector: HotspotDetector for access pattern tracking (Issue #921)
            enable_hotspot_tracking: Enable hotspot tracking (default: True)
        """
        self.metadata_store = metadata_store
        self.rebac_manager: EnhancedReBACManager | None = rebac_manager
        self.entity_registry = entity_registry  # v0.5.0 ACE
        self.router = router  # For backend object type resolution

        # P0-4: Enhanced features
        self.allow_admin_bypass = allow_admin_bypass
        self.allow_system_bypass = allow_system_bypass
        self.audit_store = audit_store
        self.admin_bypass_paths = admin_bypass_paths or []

        # Issue #922: Permission boundary cache
        self._enable_boundary_cache = enable_boundary_cache
        self._boundary_cache: PermissionBoundaryCache | None = None
        if boundary_cache is not None:
            self._boundary_cache = boundary_cache
            logger.info("[PermissionEnforcer] Using provided boundary cache")
        elif enable_boundary_cache:
            # Lazy import to avoid circular dependencies
            from nexus.core.permission_boundary_cache import PermissionBoundaryCache

            self._boundary_cache = PermissionBoundaryCache()
            logger.info("[PermissionEnforcer] Boundary cache ENABLED (50k entries, 300s TTL)")

        # Register boundary cache invalidation callback with rebac_manager
        if (
            self._boundary_cache
            and self.rebac_manager
            and hasattr(self.rebac_manager, "register_boundary_cache_invalidator")
        ):
            # Create a unique ID for this enforcer's callback
            callback_id = f"permission_enforcer_{id(self)}"
            self.rebac_manager.register_boundary_cache_invalidator(
                callback_id,
                self._boundary_cache.invalidate_permission_change,
            )

        # Issue #921: Hotspot detection for proactive cache prefetching
        self._enable_hotspot_tracking = enable_hotspot_tracking
        self._hotspot_detector: HotspotDetector | None = None
        if hotspot_detector is not None:
            self._hotspot_detector = hotspot_detector
            logger.info("[PermissionEnforcer] Using provided hotspot detector")
        elif enable_hotspot_tracking:
            # Lazy import to avoid circular dependencies
            from nexus.core.hotspot_detector import HotspotDetector

            self._hotspot_detector = HotspotDetector()
            logger.info("[PermissionEnforcer] Hotspot tracking ENABLED (5min window, 50 threshold)")

        # perf19: Bitmap completeness cache
        # Tracks users whose Tiger bitmap contains ALL their permissions
        # (no directory-level grants that could provide inherited access)
        # Key: (subject_type, subject_id, tenant_id) -> (is_complete, cached_at)
        self._bitmap_completeness_cache: dict[tuple[str, str, str], tuple[bool, float]] = {}
        self._bitmap_completeness_ttl = 60.0  # 1 minute TTL (conservative)

        # perf19: Leopard Directory Index (Option 4)
        # Caches which directories a user can access (for inheritance checks)
        # Key: (subject_type, subject_id, tenant_id) -> (accessible_dirs: set, cached_at)
        # When filtering, if any ancestor dir is in this set, path inherits access
        self._leopard_dir_index: dict[tuple[str, str, str], tuple[set[str], float]] = {}
        self._leopard_dir_ttl = 60.0  # 1 minute TTL

        # Warn if ACL store is provided (deprecated)
        if acl_store is not None:
            import warnings

            warnings.warn(
                "acl_store parameter is deprecated and will be removed in v0.7.0. "
                "Use ReBAC for all permissions.",
                DeprecationWarning,
                stacklevel=2,
            )

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
            >>> ctx = OperationContext(user="alice", groups=["developers"])
            >>> enforcer.check("/workspace/file.txt", Permission.READ, ctx)
            True
        """
        logger.debug(
            f"[PermissionEnforcer.check] path={path}, perm={permission.name}, user={context.user}, is_admin={context.is_admin}, is_system={context.is_system}"
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
            from nexus.core.permissions_enhanced import AdminCapability

            # P0-4: Tenant boundary check (security fix for issue #819)
            # Extract tenant from path (format: /tenant:{tenant_id}/...)
            path_tenant_id = None
            if path.startswith("/tenant:"):
                parts = path[8:].split("/", 1)  # Remove "/tenant:" prefix
                if parts:
                    path_tenant_id = parts[0]

            # Check if admin is attempting cross-tenant access
            if (
                path_tenant_id
                and context.tenant_id
                and path_tenant_id != context.tenant_id
                and AdminCapability.MANAGE_TENANTS not in context.admin_capabilities
            ):
                # Cross-tenant access requires MANAGE_TENANTS capability (system admin only)
                # Not system admin - deny cross-tenant access
                self._log_bypass_denied(
                    context,
                    path,
                    permission_str,
                    "admin",
                    f"cross_tenant_access_denied_path_tenant={path_tenant_id}_context_tenant={context.tenant_id}",
                )
                # Immediately raise PermissionError for cross-tenant access violation
                raise PermissionError(
                    f"Access denied: Cross-tenant access requires MANAGE_TENANTS capability. "
                    f"Context tenant: {context.tenant_id}, Path tenant: {path_tenant_id}"
                )

            required_capability = AdminCapability.get_required_capability(path, permission_str)
            wildcard_capability = f"admin:{permission_str}:*"

            # Check if user has EITHER the path-specific capability OR the wildcard capability
            # Wildcard capability (admin:read:*) grants access to ALL paths
            has_capability = (
                required_capability in context.admin_capabilities
                or wildcard_capability in context.admin_capabilities
            )

            if not has_capability:
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
            f"[_check_rebac] path={path}, permission={permission}, context.user={context.user}"
        )

        if not self.rebac_manager:
            # No ReBAC manager - deny by default
            # This ensures security: must explicitly configure ReBAC
            logger.debug("  -> DENY (no rebac_manager)")
            return False

        # Map Permission flags to string permission names
        permission_name = self._permission_to_string(permission)
        if permission_name == "unknown":
            logger.debug(f"  -> DENY (unknown permission: {permission})")
            return False

        # Get backend-specific object type for ReBAC check
        # This allows different backends (Postgres, Redis, etc.) to have different permission models
        object_type = "file"  # Default
        object_id = path  # Default - use virtual path for permission checks

        if self.router:
            try:
                # Route path to backend to get object type
                route = self.router.route(
                    path,
                    tenant_id=context.tenant_id,
                    is_admin=context.is_admin,
                    check_write=False,
                )
                # Ask backend for its object type
                object_type = route.backend.get_object_type(route.backend_path)

                # CRITICAL FIX: For file objects, use the VIRTUAL path for permission checks,
                # not the backend-relative path. ReBAC tuples are created with virtual paths
                # (e.g., /mnt/gcs/file.csv), but backend.get_object_id() returns backend-relative
                # paths (e.g., file.csv) which breaks permission inheritance for mounted backends.
                # Non-file backends (DB tables, Redis keys, etc.) can still override object_id.
                if object_type == "file":
                    # Use virtual path for file permission checks (mount-aware)
                    object_id = path
                    logger.debug(
                        f"[PermissionEnforcer] Using virtual path for file permission check: '{path}'"
                    )
                else:
                    # For non-file backends, use backend-provided object_id
                    object_id = route.backend.get_object_id(route.backend_path)
                    logger.debug(
                        f"[PermissionEnforcer] Using backend object_id for {object_type}: '{object_id}'"
                    )
            except Exception as e:
                # If routing fails, fall back to default "file" type with virtual path
                logger.warning(
                    f"[_check_rebac] Failed to route path for object type: {e}, using default 'file'"
                )

        # Check ReBAC permission using backend-provided object type
        # P0-4: Pass tenant_id for multi-tenant isolation
        tenant_id = context.tenant_id or "default"
        subject = context.get_subject()

        logger.debug(
            f"[_check_rebac] Calling rebac_check: subject={subject}, permission={permission_name}, object=('{object_type}', '{object_id}'), tenant_id={tenant_id}"
        )

        # Issue #921: Record access for hotspot detection (before cache/graph check)
        # This tracks access patterns to enable proactive cache prefetching
        if self._hotspot_detector:
            self._hotspot_detector.record_access(
                subject_type=subject[0],
                subject_id=subject[1],
                resource_type=object_type,
                permission=permission_name,
                tenant_id=tenant_id,
            )

        # NOTE: Removed implicit directory TRAVERSE optimization (was incorrectly granting
        # access to ALL authenticated users for ANY implicit directory, violating Unix semantics)
        # Correct behavior: user should only see a directory if they have access to at least
        # one file inside it. This is handled by _has_descendant_access in the listing code.

        # 1. Direct permission check (uses Tiger Cache, L1 cache, then graph traversal)
        result = self.rebac_manager.rebac_check(
            subject=subject,  # P0-2: Use typed subject
            permission=permission_name,
            object=(object_type, object_id),
            tenant_id=tenant_id,
        )
        logger.debug(f"[_check_rebac] rebac_manager.rebac_check returned: {result}")

        if result:
            return True

        # 2b. TRAVERSE implied by READ/WRITE - if user has READ or WRITE, they can TRAVERSE
        if permission_name == "traverse" and not result:
            # Check if user has READ (which implies TRAVERSE)
            read_result = self.rebac_manager.rebac_check(
                subject=subject,
                permission="read",
                object=(object_type, object_id),
                tenant_id=tenant_id,
            )
            if read_result:
                logger.debug("[_check_rebac] ALLOW TRAVERSE (has READ permission)")
                return True

            # Check if user has WRITE (which implies TRAVERSE)
            write_result = self.rebac_manager.rebac_check(
                subject=subject,
                permission="write",
                object=(object_type, object_id),
                tenant_id=tenant_id,
            )
            if write_result:
                logger.debug("[_check_rebac] ALLOW TRAVERSE (has WRITE permission)")
                return True

        # 3. Check parent directories for inherited permissions (filesystem hierarchy)
        # For READ/WRITE/TRAVERSE, if user has permission on parent directory, grant access to child
        # This enables permission inheritance: grant /workspace → inherits to /workspace/file.txt
        #
        # Issue #922: Use boundary cache for O(1) inheritance checks
        # Instead of walking up O(depth) for every file, cache the nearest ancestor with a grant
        if permission_name in ("read", "write", "traverse") and object_id:
            import os

            subject_type, subject_id = subject

            # FAST PATH: Check boundary cache first (Issue #922)
            if self._boundary_cache:
                boundary = self._boundary_cache.get_boundary(
                    tenant_id, subject_type, subject_id, permission_name, object_id
                )
                if boundary:
                    # Found cached boundary - verify it's still valid
                    boundary_result = self.rebac_manager.rebac_check(
                        subject=subject,
                        permission=permission_name,
                        object=(object_type, boundary),
                        tenant_id=tenant_id,
                    )
                    if boundary_result:
                        logger.debug(
                            f"[_check_rebac] ALLOW (boundary cache hit: {object_id} → {boundary})"
                        )
                        return True
                    else:
                        # Boundary no longer valid - invalidate and fall through to slow path
                        logger.debug(
                            f"[_check_rebac] Boundary cache stale: {boundary} no longer grants {permission_name}"
                        )
                        self._boundary_cache.invalidate_permission_change(
                            tenant_id, subject_type, subject_id, permission_name, boundary
                        )

            # SLOW PATH: Walk up the directory tree
            parent_path = object_id
            checked_parents = []

            while parent_path and parent_path != "/":
                parent_path = os.path.dirname(parent_path)
                if not parent_path or parent_path == object_id:
                    # Reached root or no change
                    parent_path = "/"

                checked_parents.append(parent_path)
                logger.debug(f"[_check_rebac] Checking parent directory: {parent_path}")

                # Check parent directory permission
                parent_result = self.rebac_manager.rebac_check(
                    subject=subject,
                    permission=permission_name,
                    object=(object_type, parent_path),
                    tenant_id=tenant_id,
                )

                if parent_result:
                    logger.debug(
                        f"[_check_rebac] ALLOW (inherited from parent directory: {parent_path})"
                    )
                    # Cache this boundary for future lookups (Issue #922)
                    if self._boundary_cache:
                        self._boundary_cache.set_boundary(
                            tenant_id,
                            subject_type,
                            subject_id,
                            permission_name,
                            object_id,
                            parent_path,
                        )
                    return True

                # Stop at root
                if parent_path == "/":
                    break

            logger.debug(
                f"[_check_rebac] No parent directory permissions found (checked: {checked_parents})"
            )

        # No permission found
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

        # For other operations, only allow /system paths
        # Use strict matching: /system/ or exactly /system (not /systemdata, etc.)
        if not (path.startswith("/system/") or path == "/system"):
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

        from nexus.core.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            tenant_id=context.tenant_id,
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

        from nexus.core.permissions_enhanced import AuditLogEntry

        entry = AuditLogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            request_id=getattr(context, "request_id", str(uuid.uuid4())),
            user=context.user,
            tenant_id=context.tenant_id,
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
        from nexus.core import glob_fast

        return glob_fast.glob_match(path, list(allowlist))

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
    def hotspot_detector(self) -> HotspotDetector | None:
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
        """Filter list of paths by read permission.

        Performance optimized with bulk permission checking (issue #380).
        Instead of checking each path individually (N queries), uses rebac_check_bulk()
        to check all paths in a single batch (1-2 queries).

        This is used by list() operations to only return files
        the user has permission to read.

        Args:
            paths: List of paths to filter
            context: Operation context

        Returns:
            Filtered list of paths user can read

        Examples:
            >>> enforcer = PermissionEnforcer(metadata_store)
            >>> ctx = OperationContext(user="alice", groups=["developers"])
            >>> all_paths = ["/file1.txt", "/file2.txt", "/secret.txt"]
            >>> enforcer.filter_list(all_paths, ctx)
            ["/file1.txt", "/file2.txt"]  # /secret.txt filtered out
        """
        # Admin/system bypass
        if (context.is_admin and self.allow_admin_bypass) or (
            context.is_system and self.allow_system_bypass
        ):
            return paths

        # OPTIMIZATION: Use bulk permission checking for better performance
        # This reduces N individual checks (each with 10-15 queries) to 1-2 bulk queries
        if self.rebac_manager and hasattr(self.rebac_manager, "rebac_check_bulk"):
            import time

            overall_start = time.time()
            tenant_id = context.tenant_id or "default"
            logger.debug(
                f"[PERF-FILTER] filter_list START: {len(paths)} paths, subject={context.get_subject()}, tenant={tenant_id}"
            )

            # TIGER CACHE + RUST ACCELERATION (Issue #896)
            # Try O(1) bitmap filtering before falling back to O(n) graph traversal
            tiger_cache = getattr(self.rebac_manager, "_tiger_cache", None)
            if tiger_cache is not None and len(paths) > 0:
                try:
                    tiger_start = time.time()
                    subject = context.get_subject()
                    subject_type, subject_id = subject

                    # Get bitmap bytes for this subject's read permissions on files
                    bitmap_bytes = tiger_cache.get_bitmap_bytes(
                        subject_type=subject_type,
                        subject_id=subject_id,
                        permission="read",
                        resource_type="file",
                        tenant_id=tenant_id,
                    )

                    if bitmap_bytes is not None:
                        # Get path int IDs from resource map
                        resource_map = tiger_cache._resource_map
                        path_to_int: dict[str, int] = {}
                        int_to_path: dict[int, str] = {}

                        # Bulk lookup path int IDs (no tenant - resource paths are globally unique)
                        with resource_map._lock:
                            for path in paths:
                                key = ("file", path)  # No tenant - cross-tenant fix
                                int_id = resource_map._uuid_to_int.get(key)
                                if int_id is not None:
                                    path_to_int[path] = int_id
                                    int_to_path[int_id] = path

                        if path_to_int:
                            # Use Rust for O(1) bitmap filtering
                            try:
                                import nexus_fast

                                path_int_ids = list(path_to_int.values())
                                accessible_ids = nexus_fast.filter_paths_with_tiger_cache(
                                    path_int_ids, bitmap_bytes
                                )

                                # Convert back to paths
                                filtered = [
                                    int_to_path[id] for id in accessible_ids if id in int_to_path
                                ]

                                # Handle paths not in resource map (need fallback check)
                                paths_not_in_map = [p for p in paths if p not in path_to_int]

                                # CRITICAL FIX: Also handle paths that ARE in the map but NOT
                                # in the bitmap. These might have inherited permissions via
                                # parent directories (e.g., agent has viewer on directory,
                                # child files inherit read permission via parent_viewer).
                                # Tiger Cache only stores direct grants, not inherited permissions.
                                paths_in_map_but_not_granted = [
                                    p for p in paths if p in path_to_int and p not in filtered
                                ]

                                paths_needing_fallback = (
                                    paths_not_in_map + paths_in_map_but_not_granted
                                )

                                if paths_needing_fallback:
                                    # OPTIMIZATION: Only check fallback paths via rebac_check_bulk,
                                    # not ALL paths. Combine Tiger Cache results with bulk results.
                                    logger.debug(
                                        f"[TIGER-RUST] {len(paths_needing_fallback)} paths need fallback: "
                                        f"{len(paths_not_in_map)} not in map, "
                                        f"{len(paths_in_map_but_not_granted)} in map but not in bitmap "
                                        "(may have inherited permissions)"
                                    )

                                    subject = context.get_subject()
                                    subject_type, subject_id = subject

                                    # BITMAP COMPLETENESS CHECK (perf19 - Option 3):
                                    # If we've previously determined this user's bitmap is complete
                                    # (no directory grants providing inherited access), skip fallback entirely
                                    completeness_key = (subject_type, subject_id, tenant_id)
                                    cached_completeness = self._bitmap_completeness_cache.get(
                                        completeness_key
                                    )
                                    if cached_completeness:
                                        is_complete, cached_at = cached_completeness
                                        if (
                                            is_complete
                                            and (time.time() - cached_at)
                                            < self._bitmap_completeness_ttl
                                        ):
                                            # Bitmap is complete - all permissions are direct grants
                                            # No need to check fallback, paths not in bitmap are denied
                                            tiger_elapsed = time.time() - tiger_start
                                            logger.info(
                                                f"[BITMAP-COMPLETE] Skipped {len(paths_needing_fallback)} fallback checks "
                                                f"(bitmap complete for {subject_type}:{subject_id}), "
                                                f"filter_list: {tiger_elapsed:.3f}s, allowed {len(filtered)}/{len(paths)} paths"
                                            )
                                            return filtered

                                    # LEOPARD DIRECTORY INDEX (perf19 - Option 4):
                                    # Check cached accessible directories first - if any ancestor
                                    # of a path is in the index, the path inherits access
                                    leopard_key = (subject_type, subject_id, tenant_id)
                                    cached_leopard = self._leopard_dir_index.get(leopard_key)
                                    leopard_allowed: list[str] = []

                                    if cached_leopard:
                                        accessible_dirs, cached_at = cached_leopard
                                        if (
                                            time.time() - cached_at
                                        ) < self._leopard_dir_ttl and accessible_dirs:
                                            # Check each path against cached accessible directories
                                            remaining_paths = []
                                            for p in paths_needing_fallback:
                                                # Check if any ancestor is in accessible_dirs
                                                current = p
                                                found = False
                                                while current and current != "/":
                                                    parent = os.path.dirname(current) or "/"
                                                    if parent in accessible_dirs:
                                                        leopard_allowed.append(p)
                                                        found = True
                                                        break
                                                    current = parent
                                                if not found:
                                                    remaining_paths.append(p)

                                            if leopard_allowed:
                                                logger.info(
                                                    f"[LEOPARD-INDEX] Allowed {len(leopard_allowed)} paths via cached directory grants, "
                                                    f"{len(remaining_paths)} remaining"
                                                )
                                                paths_needing_fallback = remaining_paths
                                                # Add leopard-allowed paths to filtered results
                                                filtered.extend(leopard_allowed)

                                    # HIERARCHICAL PRE-FILTER (perf19 - Option 2):
                                    # Instead of checking N paths individually, first check unique
                                    # parent directories. If user has no access to a parent dir
                                    # (and bitmap already has all their grants), skip all children.
                                    # This reduces 5000+ checks to ~50 parent directory checks.
                                    hierarchy_start = time.time()
                                    original_fallback_count = len(paths_needing_fallback)

                                    # Skip if no paths left after Leopard check
                                    if not paths_needing_fallback:
                                        tiger_elapsed = time.time() - tiger_start
                                        logger.info(
                                            f"[TIGER-RUST] filter_list hybrid: {tiger_elapsed:.3f}s, "
                                            f"allowed {len(filtered)}/{len(paths)} paths "
                                            f"(bitmap: {len(filtered) - len(leopard_allowed)}, leopard: {len(leopard_allowed)})"
                                        )
                                        return filtered

                                    # Group paths by their immediate parent directory
                                    paths_by_parent: dict[str, list[str]] = defaultdict(list)
                                    for p in paths_needing_fallback:
                                        parent = os.path.dirname(p) or "/"
                                        paths_by_parent[parent].append(p)

                                    unique_parents = list(paths_by_parent.keys())

                                    # Only do hierarchical check if it would save significant work
                                    # (more than 100 paths and fewer unique parents than paths)
                                    if (
                                        len(unique_parents) < len(paths_needing_fallback)
                                        and len(paths_needing_fallback) > 100
                                    ):
                                        # Check which parent directories user might have access to
                                        parent_checks = [
                                            (subject, "read", ("file", parent))
                                            for parent in unique_parents
                                        ]
                                        parent_results = self.rebac_manager.rebac_check_bulk(
                                            parent_checks, tenant_id=tenant_id
                                        )

                                        # Find parents with access (children may inherit)
                                        accessible_parents = {
                                            parent
                                            for parent, check in zip(
                                                unique_parents, parent_checks, strict=False
                                            )
                                            if parent_results.get(check, False)
                                        }

                                        hierarchy_elapsed = (time.time() - hierarchy_start) * 1000
                                        logger.info(
                                            f"[HIERARCHY-PREFILTER] {len(accessible_parents)}/{len(unique_parents)} "
                                            f"parents accessible in {hierarchy_elapsed:.1f}ms"
                                        )

                                        # Store accessible directories in Leopard index for future requests
                                        if accessible_parents:
                                            # Merge with existing cached dirs (if any)
                                            existing_dirs = set()
                                            if (
                                                cached_leopard
                                                and (time.time() - cached_leopard[1])
                                                < self._leopard_dir_ttl
                                            ):
                                                existing_dirs = cached_leopard[0]
                                            new_dirs = existing_dirs | accessible_parents
                                            self._leopard_dir_index[leopard_key] = (
                                                new_dirs,
                                                time.time(),
                                            )
                                            logger.info(
                                                f"[LEOPARD-INDEX] Cached {len(accessible_parents)} accessible directories "
                                                f"for {subject_type}:{subject_id} (total: {len(new_dirs)})"
                                            )

                                        # Only check paths under accessible parents
                                        if len(accessible_parents) < len(unique_parents):
                                            paths_needing_fallback = []
                                            for parent in accessible_parents:
                                                paths_needing_fallback.extend(
                                                    paths_by_parent[parent]
                                                )

                                            logger.info(
                                                f"[HIERARCHY-PREFILTER] Reduced fallback: {original_fallback_count} -> "
                                                f"{len(paths_needing_fallback)} paths "
                                                f"(skipped {original_fallback_count - len(paths_needing_fallback)} under denied parents)"
                                            )

                                            # If NO accessible parents, mark bitmap as complete
                                            # This user has no directory-level grants, so bitmap has all their permissions
                                            if (
                                                len(accessible_parents) == 0
                                                and original_fallback_count > 100
                                            ):
                                                self._bitmap_completeness_cache[
                                                    completeness_key
                                                ] = (True, time.time())
                                                logger.info(
                                                    f"[BITMAP-COMPLETE] Marked bitmap complete for {subject_type}:{subject_id} "
                                                    f"(0 accessible parents out of {len(unique_parents)})"
                                                )

                                    # Build checks only for remaining paths needing fallback
                                    fallback_checks = []
                                    for path in paths_needing_fallback:
                                        fallback_checks.append((subject, "read", ("file", path)))

                                    # Check fallback paths via rebac_check_bulk
                                    if fallback_checks:
                                        fallback_results = self.rebac_manager.rebac_check_bulk(
                                            fallback_checks, tenant_id=tenant_id
                                        )
                                    else:
                                        fallback_results = {}

                                    # Add paths that passed fallback check to filtered results
                                    fallback_allowed_count = 0
                                    for path, check in zip(
                                        paths_needing_fallback, fallback_checks, strict=False
                                    ):
                                        if fallback_results.get(check, False):
                                            filtered.append(path)
                                            fallback_allowed_count += 1

                                    # If fallback found 0 additional paths, mark bitmap as complete
                                    # (all permissions are direct grants in bitmap, no inheritance)
                                    if (
                                        fallback_allowed_count == 0
                                        and original_fallback_count > 100
                                    ):
                                        self._bitmap_completeness_cache[completeness_key] = (
                                            True,
                                            time.time(),
                                        )
                                        logger.info(
                                            f"[BITMAP-COMPLETE] Marked bitmap complete for {subject_type}:{subject_id} "
                                            f"(0 fallback results from {original_fallback_count} checks)"
                                        )

                                    tiger_elapsed = time.time() - tiger_start
                                    bitmap_count = (
                                        len(filtered)
                                        - fallback_allowed_count
                                        - len(leopard_allowed)
                                    )
                                    logger.info(
                                        f"[TIGER-RUST] filter_list hybrid: {tiger_elapsed:.3f}s, "
                                        f"allowed {len(filtered)}/{len(paths)} paths "
                                        f"(bitmap: {bitmap_count}, leopard: {len(leopard_allowed)}, fallback: {fallback_allowed_count})"
                                    )
                                    return filtered
                                else:
                                    tiger_elapsed = time.time() - tiger_start
                                    overall_elapsed = time.time() - overall_start
                                    logger.info(
                                        f"[TIGER-RUST] filter_list via Rust bitmap: {tiger_elapsed:.3f}s, "
                                        f"allowed {len(filtered)}/{len(paths)} paths"
                                    )
                                    return filtered

                            except ImportError:
                                logger.debug(
                                    "[TIGER-RUST] nexus_fast not available, falling back to rebac_check_bulk"
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[TIGER-RUST] Rust filtering failed: {e}, falling back to rebac_check_bulk"
                                )
                        else:
                            logger.debug(
                                "[TIGER-RUST] No paths found in resource map, falling back to rebac_check_bulk"
                            )
                    else:
                        logger.debug(
                            "[TIGER-RUST] No bitmap available for subject, falling back to rebac_check_bulk"
                        )

                except Exception as e:
                    logger.warning(
                        f"[TIGER-RUST] Tiger Cache integration error: {e}, falling back to rebac_check_bulk"
                    )

            # OPTIMIZATION: Pre-filter paths by tenant before permission checks
            # This avoids checking permissions on paths the user can never access
            # For /tenants/* paths, only keep paths matching the user's tenant
            prefilter_start = time.time()
            paths_to_check = []
            paths_prefiltered = 0
            for path in paths:
                # Fast path: /tenants/X paths should only be checked for tenant X
                if path.startswith("/tenants/"):
                    # Extract tenant from path: /tenants/tenant_name/...
                    path_parts = path.split("/")
                    if len(path_parts) >= 3:
                        path_tenant = path_parts[2]  # /tenants/<tenant_name>/...
                        if path_tenant != tenant_id:
                            # Skip paths for other tenants entirely
                            paths_prefiltered += 1
                            continue
                paths_to_check.append(path)

            prefilter_elapsed = time.time() - prefilter_start
            if paths_prefiltered > 0:
                logger.debug(
                    f"[PERF-FILTER] Tenant pre-filter: {paths_prefiltered} paths skipped "
                    f"(not in tenant {tenant_id}), {len(paths_to_check)} remaining in {prefilter_elapsed:.3f}s"
                )

            # Build list of checks: (subject, "read", object) for each path
            build_start = time.time()
            checks = []
            subject = context.get_subject()

            for path in paths_to_check:
                # PERFORMANCE FIX: Skip expensive router.route() call for each file
                # For standard file paths, just use "file" as object type
                # This avoids O(N) routing overhead during bulk permission checks
                obj_type = "file"  # Default to file for all paths

                # Only check router for special namespaces (non-file paths)
                # This is much faster than routing every single file
                if self.router and not path.startswith("/workspace"):
                    try:
                        # Use router to determine correct object type for special paths
                        route = self.router.route(
                            path,
                            tenant_id=context.tenant_id,
                            agent_id=context.agent_id,
                            is_admin=context.is_admin,
                        )
                        # Get object type from namespace (if available)
                        if hasattr(route, "namespace") and route.namespace:
                            obj_type = route.namespace
                    except Exception:
                        # Fallback to "file" if routing fails
                        pass

                checks.append((subject, "read", (obj_type, path)))

            build_elapsed = time.time() - build_start
            logger.debug(
                f"[PERF-FILTER] Built {len(checks)} permission checks in {build_elapsed:.3f}s"
            )

            try:
                # Perform bulk permission check
                bulk_start = time.time()
                results = self.rebac_manager.rebac_check_bulk(checks, tenant_id=tenant_id)
                bulk_elapsed = time.time() - bulk_start
                logger.debug(f"[PERF-FILTER] Bulk check completed in {bulk_elapsed:.3f}s")

                # Filter paths based on bulk results
                filtered = []
                for path, check in zip(paths_to_check, checks, strict=False):
                    if results.get(check, False):
                        filtered.append(path)

                overall_elapsed = time.time() - overall_start
                logger.debug(
                    f"[PERF-FILTER] filter_list DONE: {overall_elapsed:.3f}s total, "
                    f"allowed {len(filtered)}/{len(paths)} paths (prefiltered {paths_prefiltered})"
                )
                return filtered

            except Exception as e:
                # Fallback to individual checks if bulk fails
                logger.warning(
                    f"Bulk permission check failed, falling back to individual checks: {e}"
                )
                # Fall through to original implementation

        # Fallback: Filter by ReBAC permissions individually (original implementation)
        result = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                result.append(path)

        return result
