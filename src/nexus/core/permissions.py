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
    """Context for file operations with subject identity (P0-2).

    This class carries authentication and authorization context through
    all filesystem operations to enable permission checking.

    P0-2: Subject-based identity supports:
    - user: Human users (alice, bob)
    - agent: AI agents (claude_001, gpt4_agent)
    - service: Backend services (backup_service, indexer)
    - session: Temporary sessions (session_abc123)

    Attributes:
        user: Subject ID performing the operation (LEGACY: use subject_id)
        subject_type: Type of subject (user, agent, service, session)
        subject_id: Unique identifier for the subject
        groups: List of group IDs the subject belongs to
        tenant_id: Tenant/organization ID for multi-tenant isolation (optional)
        agent_id: Agent ID for workspace isolation (optional, DEPRECATED)
        is_admin: Whether the subject has admin privileges
        is_system: Whether this is a system operation (bypasses all checks)

    Examples:
        >>> # Human user context (P0-2)
        >>> ctx = OperationContext(
        ...     subject_type="user",
        ...     subject_id="alice",
        ...     groups=["developers"],
        ...     tenant_id="org_acme"
        ... )
        >>> # AI agent context (P0-2)
        >>> ctx = OperationContext(
        ...     subject_type="agent",
        ...     subject_id="claude_001",
        ...     groups=["ai_agents"],
        ...     tenant_id="org_acme"
        ... )
        >>> # Service context (P0-2)
        >>> ctx = OperationContext(
        ...     subject_type="service",
        ...     subject_id="backup_service",
        ...     groups=[],
        ...     is_system=True
        ... )
        >>> # Legacy: user field (backward compatibility)
        >>> ctx = OperationContext(user="alice", groups=["developers"])
        >>> # Auto-sets: subject_type="user", subject_id="alice"
    """

    user: str  # LEGACY: Kept for backward compatibility
    groups: list[str]
    tenant_id: str | None = None
    agent_id: str | None = None  # DEPRECATED: Use subject_type + subject_id
    is_admin: bool = False
    is_system: bool = False

    # P0-2: Subject-based identity
    subject_type: str = "user"  # Default to "user" for backward compatibility
    subject_id: str | None = None  # If None, uses self.user

    def __post_init__(self) -> None:
        """Validate context and apply P0-2 subject defaults."""
        # P0-2: If subject_id not provided, use user field for backward compatibility
        if self.subject_id is None:
            self.subject_id = self.user

        if not self.user:
            raise ValueError("user is required")
        if not isinstance(self.groups, list):
            raise TypeError(f"groups must be list, got {type(self.groups)}")

    def get_subject(self) -> tuple[str, str]:
        """Get subject as (type, id) tuple for ReBAC.

        P0-2: Returns properly typed subject for permission checking.

        Returns:
            Tuple of (subject_type, subject_id)

        Example:
            >>> ctx = OperationContext(subject_type="agent", subject_id="claude_001", groups=[])
            >>> ctx.get_subject()
            ('agent', 'claude_001')
        """
        return (self.subject_type, self.subject_id or self.user)


class PermissionEnforcer:
    """Pure ReBAC permission enforcement for Nexus filesystem (v0.6.0).

    Implements permission checking using ReBAC (Relationship-Based Access Control)
    based on Google Zanzibar principles.

    Permission checks:
    1. Admin/system bypass - Always allow for admin and system users
    2. ReBAC relationship check - Check permission graph for relationships

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
    ):
        """Initialize permission enforcer.

        Args:
            metadata_store: Metadata store for file lookup (optional)
            acl_store: Deprecated, ignored (kept for backward compatibility)
            rebac_manager: ReBAC manager for relationship-based permissions
        """
        self.metadata_store = metadata_store
        self.rebac_manager: EnhancedReBACManager | None = rebac_manager

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

        # Check ReBAC permission using path directly
        # Object: ("file", path) - use path as the file identifier
        # P0-4: Pass tenant_id for multi-tenant isolation
        tenant_id = context.tenant_id or "default"
        subject = context.get_subject()
        logger.info(
            f"[_check_rebac] Calling rebac_check: subject={subject}, permission={permission_name}, object=('file', '{path}'), tenant_id={tenant_id}"
        )

        result = self.rebac_manager.rebac_check(
            subject=subject,  # P0-2: Use typed subject
            permission=permission_name,
            object=("file", path),
            tenant_id=tenant_id,
        )
        logger.info(f"[_check_rebac] rebac_manager.rebac_check returned: {result}")
        return result

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
