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
import uuid
from dataclasses import dataclass, field
from enum import IntFlag
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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
    """

    NONE = 0
    EXECUTE = 1  # x
    WRITE = 2  # w
    READ = 4  # r
    ALL = 7  # rwx


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

    # v0.5.1: Permission inheritance control
    inherit_permissions: bool = True  # Default True for backward compatibility

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
    3. v0.5.0 ACE: Agent inheritance from user (if entity_registry provided)

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
        entity_registry: Any = None,  # v0.5.0 ACE: For agent inheritance
        router: Any = None,  # PathRouter for backend object type resolution
        # P0-4: Enhanced features
        allow_admin_bypass: bool = False,  # P0-4: Kill-switch DEFAULT OFF for production security
        allow_system_bypass: bool = True,  # P0-4: System bypass still enabled (for service operations)
        audit_store: AuditStore | None = None,  # P0-4: Audit logging
        admin_bypass_paths: list[str] | None = None,  # P0-4: Scoped bypass (allowlist)
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
            entity_registry: Entity registry for agent→user inheritance (v0.5.0)
            router: PathRouter for resolving backend object types (v0.5.0+)
            allow_admin_bypass: Enable admin bypass (DEFAULT: False for security)
            allow_system_bypass: Enable system bypass (for internal operations)
            audit_store: Audit store for bypass logging
            admin_bypass_paths: Optional path allowlist for admin bypass (e.g., ["/admin/*"])
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

        # 1. Direct permission check
        result = self.rebac_manager.rebac_check(
            subject=subject,  # P0-2: Use typed subject
            permission=permission_name,
            object=(object_type, object_id),
            tenant_id=tenant_id,
        )
        logger.debug(f"[_check_rebac] rebac_manager.rebac_check returned: {result}")

        if result:
            return True

        # 2. NEW: Check parent directories for inherited permissions (filesystem hierarchy)
        # For READ/WRITE, if user has permission on parent directory, grant access to child
        # This enables permission inheritance: grant /workspace → inherits to /workspace/file.txt
        if permission_name in ("read", "write") and object_id:
            import os

            parent_path = object_id
            checked_parents = []

            # Walk up the directory tree
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
                    return True

                # Stop at root
                if parent_path == "/":
                    break

            logger.debug(
                f"[_check_rebac] No parent directory permissions found (checked: {checked_parents})"
            )

        # 3. v0.5.0 ACE: Agent inheritance from user (v0.5.1: conditional on inherit_permissions flag)
        # If subject is an agent, check if the agent's owner (user) has permission
        if context.subject_type == "agent" and context.agent_id and self.entity_registry:
            logger.debug(f"[_check_rebac] Checking agent inheritance for agent={context.agent_id}")
            # v0.5.1: Only inherit if inherit_permissions flag is enabled
            if context.inherit_permissions:
                logger.debug("[_check_rebac] inherit_permissions=True, checking parent permissions")
                # Look up agent's owner
                parent = self.entity_registry.get_parent(
                    entity_type="agent", entity_id=context.agent_id
                )

                if parent and parent.entity_type == "user":
                    logger.debug(
                        f"[_check_rebac] Agent {context.agent_id} owned by user {parent.entity_id}, checking user permission"
                    )
                    # Check if user has permission (using same object type as direct check)
                    user_result = self.rebac_manager.rebac_check(
                        subject=("user", parent.entity_id),
                        permission=permission_name,
                        object=(object_type, object_id),
                        tenant_id=tenant_id,
                    )
                    logger.debug(f"[_check_rebac] User permission check returned: {user_result}")
                    if user_result:
                        # ✅ Agent inherits user's permission
                        logger.debug(
                            f"[_check_rebac] ALLOW (agent {context.agent_id} inherits from user {parent.entity_id})"
                        )
                        return True
            else:
                logger.debug("[_check_rebac] inherit_permissions=False, skipping inheritance check")

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

        return any(fnmatch.fnmatch(path, pattern) for pattern in allowlist)

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
