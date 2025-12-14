"""Memory Permission Enforcer with Pure ReBAC (v0.6.0).

Extends the base PermissionEnforcer with identity-based relationships
for AI agent memories. Uses pure ReBAC (Relationship-Based Access Control)
for all permission checks.

Migration from v0.5.x:
  - Removed ACL and UNIX permission layers
  - All permissions managed through ReBAC relationships
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import (
    OperationContext,
    Permission,
    PermissionEnforcer,
)
from nexus.storage.models import MemoryModel

if TYPE_CHECKING:
    from nexus.core.rebac_manager_enhanced import EnhancedReBACManager


class MemoryPermissionEnforcer(PermissionEnforcer):
    """Permission enforcer for memory with identity relationships.

    Enhances the base PermissionEnforcer with:
    - Identity-based ReBAC checks (user ownership, agent relationships)
    - Tenant-scoped memory sharing
    - User ownership inheritance (agents owned by same user can access)
    """

    def __init__(
        self,
        metadata_store: Any = None,
        acl_store: Any | None = None,  # Deprecated, kept for backward compatibility
        rebac_manager: EnhancedReBACManager | None = None,
        memory_router: MemoryViewRouter | None = None,
        entity_registry: EntityRegistry | None = None,
    ) -> None:
        """Initialize memory permission enforcer.

        Args:
            metadata_store: Metadata store for file permissions.
            acl_store: Deprecated, ignored (kept for backward compatibility).
            rebac_manager: ReBAC manager for relationship-based permissions.
            memory_router: Memory view router for resolving paths.
            entity_registry: Entity registry for identity lookups.
        """
        super().__init__(metadata_store, acl_store, rebac_manager)
        self.memory_router = memory_router
        self.entity_registry = entity_registry

    def check_memory(
        self,
        memory: MemoryModel,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check if user has permission to access memory.

        Pure ReBAC check with identity relationships:
        1. Admin/system bypass
        2. ReBAC with identity relationships

        Args:
            memory: Memory instance.
            permission: Permission to check.
            context: Operation context.

        Returns:
            True if permission is granted.
        """
        # 1. Admin/system bypass
        if context.is_admin or context.is_system:
            return True

        # 2. ReBAC check with identity relationships
        return self._check_memory_rebac(memory, permission, context)

    def _check_memory_rebac(
        self,
        memory: MemoryModel,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check ReBAC with identity relationships.

        Identity-based permission checks:
        1. Legacy/system memories without owner (empty user_id and agent_id) - read-only access
        2. Direct creator access (agent created the memory)
        3. User ownership inheritance (agent owned by memory owner)
        4. Tenant-scoped sharing (same tenant, scope='tenant')
        5. Explicit ReBAC relations (if rebac_manager available)

        Args:
            memory: Memory instance.
            permission: Permission to check.
            context: Operation context.

        Returns:
            True if ReBAC grants permission.
        """
        # 1. Legacy/system memories without owner
        # If memory has no user_id and no agent_id (empty string or None), treat as public read-only
        if not memory.user_id and not memory.agent_id:
            # Allow READ for everyone, but WRITE/EXECUTE requires explicit ownership
            # For WRITE/EXECUTE on ownerless memories, only admins/system can modify
            return permission == Permission.READ

        # 2. Direct creator access
        if context.user == memory.agent_id:
            return True

        # 3. User ownership inheritance (v0.5.1: conditional on inherit_permissions flag)
        # Check if the requesting agent is owned by the same user as the memory
        # BUT only for user/tenant/global scoped memories (not agent-scoped)
        if memory.user_id and self.entity_registry and memory.scope in ["user", "tenant", "global"]:
            # Look up the requesting user/agent in the entity registry
            requesting_entities = self.entity_registry.lookup_entity_by_id(context.user)

            for entity in requesting_entities:
                # If requesting user is an agent, check if it's owned by the memory's user
                # v0.5.1: Only inherit if inherit_permissions flag is enabled
                if (
                    entity.entity_type == "agent"
                    and entity.parent_id == memory.user_id
                    and context.inherit_permissions
                ):
                    # Same user owns both the agent and the memory
                    return True

                # If requesting user matches memory user directly
                if entity.entity_type == "user" and entity.entity_id == memory.user_id:
                    return True

        # 4. Tenant-scoped sharing
        if memory.scope == "tenant" and memory.tenant_id and self.entity_registry:
            # Check if requesting agent belongs to same tenant
            requesting_entities = self.entity_registry.lookup_entity_by_id(context.user)

            for entity in requesting_entities:
                # Check tenant membership through hierarchy
                if entity.entity_type == "agent":
                    # Get agent's parent (user)
                    if entity.parent_id:
                        user_entities = self.entity_registry.lookup_entity_by_id(entity.parent_id)
                        for user_entity in user_entities:
                            # Check if user belongs to same tenant
                            if (
                                user_entity.entity_type == "user"
                                and user_entity.parent_id == memory.tenant_id
                            ):
                                return True

                elif (
                    entity.entity_type == "user"
                    and entity.parent_id == memory.tenant_id
                    or entity.entity_type == "tenant"
                    and entity.entity_id == memory.tenant_id
                ):
                    return True

        # 5. Explicit ReBAC relations (fallback to base implementation)
        if self.rebac_manager:
            permission_name: str
            if permission & Permission.READ:
                permission_name = "read"
            elif permission & Permission.WRITE:
                permission_name = "write"
            elif permission & Permission.EXECUTE:
                permission_name = "execute"
            else:
                return False

            # P0-4: Pass tenant_id for multi-tenant isolation
            tenant_id = context.tenant_id or "default"

            # 5a. Direct permission check
            if self.rebac_manager.rebac_check(
                subject=context.get_subject(),  # P0-2: Use typed subject
                permission=permission_name,
                object=("memory", memory.memory_id),
                tenant_id=tenant_id,
            ):
                return True

            # 5b. v0.5.0 ACE: Agent inheritance from user
            # If subject is an agent, check if the agent's owner (user) has permission
            if context.subject_type == "agent" and context.agent_id and self.entity_registry:
                # Look up agent's owner
                parent = self.entity_registry.get_parent(
                    entity_type="agent", entity_id=context.agent_id
                )

                if (
                    parent
                    and parent.entity_type == "user"
                    and self.rebac_manager.rebac_check(
                        subject=("user", parent.entity_id),
                        permission=permission_name,
                        object=("memory", memory.memory_id),
                        tenant_id=tenant_id,
                    )
                ):
                    # ✅ Agent inherits user's permission
                    return True

            # Permission not granted
            return False

        return False

    def check_memory_by_path(
        self,
        virtual_path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check permission for memory accessed by virtual path.

        Resolves the path to canonical memory using MemoryViewRouter,
        then checks permissions.

        Args:
            virtual_path: Virtual path to memory.
            permission: Permission to check.
            context: Operation context.

        Returns:
            True if permission is granted.
        """
        if not self.memory_router:
            # Fall back to base file permission check
            return self.check(virtual_path, permission, context)

        # Resolve virtual path to memory
        memory = self.memory_router.resolve(virtual_path)
        if not memory:
            return False

        # Check memory permissions
        return self.check_memory(memory, permission, context)
