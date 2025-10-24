"""Memory Permission Enforcer with Identity Relationships (v0.4.0).

Extends the base PermissionEnforcer with identity-based relationships
for AI agent memories. Implements the 3-layer permission model:
  1. ReBAC - Identity relationships (user ownership, tenant sharing)
  2. ACL - Canonical path access control
  3. UNIX - Proper user ownership semantics
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nexus.core.entity_registry import EntityRegistry
from nexus.core.memory_router import MemoryViewRouter
from nexus.core.permissions import (
    FileMode,
    OperationContext,
    Permission,
    PermissionEnforcer,
)
from nexus.storage.models import MemoryModel

if TYPE_CHECKING:
    from nexus.core.acl import ACLStore
    from nexus.core.rebac_manager import ReBACManager


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
        acl_store: ACLStore | None = None,
        rebac_manager: ReBACManager | None = None,
        memory_router: MemoryViewRouter | None = None,
        entity_registry: EntityRegistry | None = None,
    ) -> None:
        """Initialize memory permission enforcer.

        Args:
            metadata_store: Metadata store for file permissions.
            acl_store: ACL store for access control lists.
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

        Three-layer check:
        1. ReBAC with identity relationships
        2. ACL on canonical memory path
        3. UNIX permissions with user ownership

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
        if self._check_memory_rebac(memory, permission, context):
            return True

        # 3. ACL check on canonical path
        if self.acl_store:
            canonical_path = f"/objs/memory/{memory.memory_id}"
            acl_result = self._check_acl(canonical_path, permission, context)
            if acl_result is not None:
                return acl_result

        # 4. UNIX permissions with user ownership
        return self._check_memory_unix(memory, permission, context)

    def _check_memory_rebac(
        self,
        memory: MemoryModel,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check ReBAC with identity relationships.

        Identity-based permission checks:
        1. Direct creator access (agent created the memory)
        2. User ownership inheritance (agent owned by memory owner)
        3. Tenant-scoped sharing (same tenant, scope='tenant')
        4. Explicit ReBAC relations (if rebac_manager available)

        Args:
            memory: Memory instance.
            permission: Permission to check.
            context: Operation context.

        Returns:
            True if ReBAC grants permission.
        """
        # 1. Direct creator access
        if context.user == memory.agent_id:
            return True

        # 2. User ownership inheritance
        # Check if the requesting agent is owned by the same user as the memory
        # BUT only for user/tenant/global scoped memories (not agent-scoped)
        if memory.user_id and self.entity_registry and memory.scope in ["user", "tenant", "global"]:
            # Look up the requesting user/agent in the entity registry
            requesting_entities = self.entity_registry.lookup_entity_by_id(context.user)

            for entity in requesting_entities:
                # If requesting user is an agent, check if it's owned by the memory's user
                if entity.entity_type == "agent" and entity.parent_id == memory.user_id:
                    # Same user owns both the agent and the memory
                    return True

                # If requesting user matches memory user directly
                if entity.entity_type == "user" and entity.entity_id == memory.user_id:
                    return True

        # 3. Tenant-scoped sharing
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

        # 4. Explicit ReBAC relations (fallback to base implementation)
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

            return self.rebac_manager.rebac_check(
                subject=("agent", context.user),
                permission=permission_name,
                object=("memory", memory.memory_id),
            )

        return False

    def _check_memory_unix(
        self,
        memory: MemoryModel,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check UNIX permissions with proper user ownership.

        Uses memory.user_id as the owner (not agent_id).
        For agent-scoped memories, checks against agent_id directly without resolving to user.

        Args:
            memory: Memory instance.
            permission: Permission to check.
            context: Operation context.

        Returns:
            True if UNIX permissions grant access.
        """
        # For agent-scoped memories, check against agent_id directly
        if memory.scope == "agent":
            # Don't resolve to user - check if context user is the agent creator
            if context.user == memory.agent_id:
                mode = FileMode(memory.mode)
                if permission & Permission.READ:
                    return mode.owner_can_read()
                elif permission & Permission.WRITE:
                    return mode.owner_can_write()
                elif permission & Permission.EXECUTE:
                    return mode.owner_can_execute()

            # Check group/other permissions for agent-scoped
            if memory.group and memory.group in context.groups:
                mode = FileMode(memory.mode)
                if permission & Permission.READ:
                    return mode.group_can_read()
                elif permission & Permission.WRITE:
                    return mode.group_can_write()
                elif permission & Permission.EXECUTE:
                    return mode.group_can_execute()

            mode = FileMode(memory.mode)
            if permission & Permission.READ:
                return mode.other_can_read()
            elif permission & Permission.WRITE:
                return mode.other_can_write()
            elif permission & Permission.EXECUTE:
                return mode.other_can_execute()

            return False

        # For user/tenant/global scoped memories, resolve agent to user
        context_user = context.user
        if self.entity_registry:
            entities = self.entity_registry.lookup_entity_by_id(context.user)
            for entity in entities:
                if entity.entity_type == "agent" and entity.parent_id:
                    # Requesting user is an agent, resolve to owner user
                    context_user = entity.parent_id
                    break
                elif entity.entity_type == "user":
                    context_user = entity.entity_id
                    break

        # Check owner permissions
        if memory.user_id and context_user == memory.user_id:
            mode = FileMode(memory.mode)
            if permission & Permission.READ:
                return mode.owner_can_read()
            elif permission & Permission.WRITE:
                return mode.owner_can_write()
            elif permission & Permission.EXECUTE:
                return mode.owner_can_execute()

        # Check group permissions
        if memory.group and memory.group in context.groups:
            mode = FileMode(memory.mode)
            if permission & Permission.READ:
                return mode.group_can_read()
            elif permission & Permission.WRITE:
                return mode.group_can_write()
            elif permission & Permission.EXECUTE:
                return mode.group_can_execute()

        # Check other permissions
        mode = FileMode(memory.mode)
        if permission & Permission.READ:
            return mode.other_can_read()
        elif permission & Permission.WRITE:
            return mode.other_can_write()
        elif permission & Permission.EXECUTE:
            return mode.other_can_execute()

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
