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
from dataclasses import dataclass
from enum import IntFlag
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core.permissions_enhanced import EnhancedOperationContext
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
    """Pure ReBAC permission enforcement for Nexus filesystem (v0.6.0).

    Implements permission checking using ReBAC (Relationship-Based Access Control)
    based on Google Zanzibar principles.

    Permission checks:
    1. Admin/system bypass - Always allow for admin and system users
    2. ReBAC relationship check - Check permission graph for relationships
    3. v0.5.0 ACE: Agent inheritance from user (if entity_registry provided)

    This is a simplified version that removed the legacy ACL and UNIX permission
    layers. All permissions are now managed through ReBAC relationships.

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
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
            entity_registry: Entity registry for agent→user inheritance (v0.5.0)
            router: PathRouter for resolving backend object types (v0.5.0+)
        """
        self.metadata_store = metadata_store
        self.rebac_manager: EnhancedReBACManager | None = rebac_manager
        self.entity_registry = entity_registry  # v0.5.0 ACE
        self.router = router  # For backend object type resolution

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
        context: OperationContext | EnhancedOperationContext,
    ) -> bool:
        """Check if user has permission to perform operation on file.

        Pure ReBAC check:
        1. Admin/system bypass - Always allow for admin/system
        2. ReBAC relationship check - Check permission graph

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
        logger.info(
            f"[PermissionEnforcer.check] path={path}, perm={permission.name}, user={context.user}, is_admin={context.is_admin}, is_system={context.is_system}"
        )

        # 1. Admin/system bypass
        if context.is_admin or context.is_system:
            logger.info("  -> ALLOW (admin/system bypass)")
            return True

        # 2. ReBAC check (pure relationship-based permissions)
        result = self._check_rebac(path, permission, context)
        logger.info(f"  -> _check_rebac returned: {result}")
        return result

    def _check_rebac(
        self,
        path: str,
        permission: Permission,
        context: OperationContext | EnhancedOperationContext,
    ) -> bool:
        """Check ReBAC relationships for permission.

        Args:
            path: Virtual file path
            permission: Permission to check
            context: Operation context

        Returns:
            True if ReBAC grants permission, False otherwise
        """
        logger.info(
            f"[_check_rebac] path={path}, permission={permission}, context.user={context.user}"
        )

        if not self.rebac_manager:
            # No ReBAC manager - deny by default
            # This ensures security: must explicitly configure ReBAC
            logger.info("  -> DENY (no rebac_manager)")
            return False

        # Map Permission flags to string permission names
        permission_name: str
        if permission & Permission.READ:
            permission_name = "read"
        elif permission & Permission.WRITE:
            permission_name = "write"
        elif permission & Permission.EXECUTE:
            permission_name = "execute"
        else:
            # Unknown permission
            logger.info(f"  -> DENY (unknown permission: {permission})")
            return False

        # Get backend-specific object type for ReBAC check
        # This allows different backends (Postgres, Redis, etc.) to have different permission models
        object_type = "file"  # Default
        object_id = path  # Default

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
                object_id = route.backend.get_object_id(route.backend_path)

                # FIX: Normalize file paths to always have leading slash for ReBAC consistency
                # Router strips leading slash by design (backend_path is relative)
                # But ReBAC tuples are created with leading slash ("/workspace/alice")
                if object_type == "file" and object_id and not object_id.startswith("/"):
                    object_id = "/" + object_id
                    logger.info(
                        f"[PermissionEnforcer] Normalized path: '{route.backend_path}' → '{object_id}'"
                    )
            except Exception as e:
                # If routing fails, fall back to default "file" type
                logger.warning(
                    f"[_check_rebac] Failed to route path for object type: {e}, using default 'file'"
                )

        # Check ReBAC permission using backend-provided object type
        # P0-4: Pass tenant_id for multi-tenant isolation
        tenant_id = context.tenant_id or "default"
        subject = context.get_subject()
        logger.info(
            f"[_check_rebac] Calling rebac_check: subject={subject}, permission={permission_name}, object=('{object_type}', '{object_id}'), tenant_id={tenant_id}"
        )

        # 1. Direct permission check
        result = self.rebac_manager.rebac_check(
            subject=subject,  # P0-2: Use typed subject
            permission=permission_name,
            object=(object_type, object_id),
            tenant_id=tenant_id,
        )
        logger.info(f"[_check_rebac] rebac_manager.rebac_check returned: {result}")

        if result:
            return True

        # 2. v0.5.0 ACE: Agent inheritance from user
        # If subject is an agent, check if the agent's owner (user) has permission
        if context.subject_type == "agent" and context.agent_id and self.entity_registry:
            logger.info(f"[_check_rebac] Checking agent inheritance for agent={context.agent_id}")
            # Look up agent's owner
            parent = self.entity_registry.get_parent(
                entity_type="agent", entity_id=context.agent_id
            )

            if parent and parent.entity_type == "user":
                logger.info(
                    f"[_check_rebac] Agent {context.agent_id} owned by user {parent.entity_id}, checking user permission"
                )
                # Check if user has permission (using same object type as direct check)
                user_result = self.rebac_manager.rebac_check(
                    subject=("user", parent.entity_id),
                    permission=permission_name,
                    object=(object_type, object_id),
                    tenant_id=tenant_id,
                )
                logger.info(f"[_check_rebac] User permission check returned: {user_result}")
                if user_result:
                    # ✅ Agent inherits user's permission
                    logger.info(
                        f"[_check_rebac] ALLOW (agent {context.agent_id} inherits from user {parent.entity_id})"
                    )
                    return True

        return False

    def filter_list(
        self,
        paths: list[str],
        context: OperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission.

        This is used by list() operations to only return files
        the user has permission to read.

        Args:
            paths: List of file paths to filter
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
        # Admin/system sees all files
        if context.is_admin or context.is_system:
            return paths

        # Filter paths by read permission
        filtered = []
        for path in paths:
            if self.check(path, Permission.READ, context):
                filtered.append(path)
        return filtered
