"""Async Permission Enforcement for Nexus (v0.6.0+).

This module provides async versions of permission enforcement operations
to work with FastAPI and async database operations.

Example:
    from nexus.core.async_permissions import AsyncPermissionEnforcer
    from nexus.core.async_rebac_manager import AsyncReBACManager

    enforcer = AsyncPermissionEnforcer(async_rebac_manager)

    # Check permission asynchronously
    if await enforcer.check_permission("/path/to/file", Permission.READ, context):
        content = await fs.read("/path/to/file")
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from nexus.core.permissions import OperationContext, Permission

if TYPE_CHECKING:
    from nexus.core.async_rebac_manager import AsyncReBACManager

logger = logging.getLogger(__name__)


class AsyncPermissionEnforcer:
    """Async permission enforcement using ReBAC.

    Non-blocking permission checks for use with FastAPI and async operations.
    Provides the same interface as PermissionEnforcer but with async methods.
    """

    def __init__(
        self,
        rebac_manager: AsyncReBACManager | None = None,
        backends: dict[str, Any] | None = None,
    ):
        """Initialize async permission enforcer.

        Args:
            rebac_manager: Async ReBAC manager instance
            backends: Backend registry for determining object types
        """
        self.rebac_manager = rebac_manager
        self.backends = backends or {}

    async def check_permission(
        self,
        path: str,
        permission: Permission,
        context: OperationContext,
    ) -> bool:
        """Check if operation is permitted (async).

        Args:
            path: Virtual file path
            permission: Permission to check (Permission.READ, etc.)
            context: Operation context with subject identity

        Returns:
            True if permitted, False otherwise

        Example:
            >>> allowed = await enforcer.check_permission(
            ...     "/workspace/doc.txt",
            ...     Permission.READ,
            ...     context
            ... )
        """
        # System operations bypass all checks
        if context.is_system:
            return True

        # Admin bypass (P0-4)
        if context.is_admin:
            return True

        # No ReBAC manager = permissive mode
        if not self.rebac_manager:
            return True

        # Determine object type from backend or default to "file"
        object_type = self._get_object_type(path)
        permission_name = self._permission_to_name(permission)

        # Check ReBAC permission
        tenant_id = context.tenant_id or "default"
        subject = context.get_subject()

        logger.debug(
            f"[ASYNC-PERM] Checking: subject={subject}, permission={permission_name}, "
            f"object=({object_type}, {path}), tenant={tenant_id}"
        )

        start_time = time.perf_counter()

        # 1. Direct permission check
        result = await self.rebac_manager.rebac_check(
            subject=subject,
            permission=permission_name,
            object=(object_type, path),
            tenant_id=tenant_id,
        )

        if result:
            elapsed = (time.perf_counter() - start_time) * 1000
            logger.debug(f"[ASYNC-PERM] Direct check passed ({elapsed:.1f}ms)")
            return True

        # 2. Check parent directories for inherited permissions
        checked_parents = []
        current_path = path
        while current_path and current_path != "/":
            # Get parent path
            parts = current_path.rstrip("/").rsplit("/", 1)
            parent_path = parts[0] if len(parts) > 1 and parts[0] else "/"

            if parent_path in checked_parents:
                break

            checked_parents.append(parent_path)
            logger.debug(f"[ASYNC-PERM] Checking parent: {parent_path}")

            parent_result = await self.rebac_manager.rebac_check(
                subject=subject,
                permission=permission_name,
                object=(object_type, parent_path),
                tenant_id=tenant_id,
            )

            if parent_result:
                elapsed = (time.perf_counter() - start_time) * 1000
                logger.debug(f"[ASYNC-PERM] Parent check passed: {parent_path} ({elapsed:.1f}ms)")
                return True

            current_path = parent_path

        # 3. For agents: check if owner user has permission
        if context.agent_id:
            parent = await self._get_agent_owner(context.agent_id, tenant_id)
            if parent and parent[0] == "user":
                logger.debug(f"[ASYNC-PERM] Checking agent owner: {parent}")
                user_result = await self.rebac_manager.rebac_check(
                    subject=("user", parent[1]),
                    permission=permission_name,
                    object=(object_type, path),
                    tenant_id=tenant_id,
                )
                if user_result:
                    elapsed = (time.perf_counter() - start_time) * 1000
                    logger.debug(f"[ASYNC-PERM] Agent owner check passed ({elapsed:.1f}ms)")
                    return True

        elapsed = (time.perf_counter() - start_time) * 1000
        logger.debug(f"[ASYNC-PERM] Permission denied ({elapsed:.1f}ms)")
        return False

    async def filter_paths_by_permission(
        self,
        paths: list[str],
        context: OperationContext,
    ) -> list[str]:
        """Filter list of paths by read permission (async).

        Uses bulk permission checking for efficiency.

        Args:
            paths: List of file paths to filter
            context: Operation context

        Returns:
            List of paths the user has permission to read
        """
        if not paths:
            return []

        # Bypass for system/admin
        if context.is_system or context.is_admin:
            return paths

        if not self.rebac_manager:
            return paths

        tenant_id = context.tenant_id or "default"
        subject = context.get_subject()

        # Build bulk check requests
        checks = []
        for path in paths:
            object_type = self._get_object_type(path)
            checks.append((subject, "read", (object_type, path)))

        start_time = time.perf_counter()

        # Perform bulk permission check
        results = await self.rebac_manager.rebac_check_bulk(checks, tenant_id=tenant_id)

        elapsed = (time.perf_counter() - start_time) * 1000

        # Filter paths based on results
        filtered = []
        for path, check in zip(paths, checks, strict=True):
            if results.get(check, False):
                filtered.append(path)

        logger.info(
            f"[ASYNC-PERM] Bulk filter: {len(paths)} paths -> {len(filtered)} allowed ({elapsed:.1f}ms)"
        )

        return filtered

    async def _get_agent_owner(
        self,
        agent_id: str,  # noqa: ARG002
        tenant_id: str,  # noqa: ARG002
    ) -> tuple[str, str] | None:
        """Get the owner of an agent (async)."""
        # Check ReBAC for agent ownership relation
        # This is a simplified version - full implementation would query rebac_tuples
        return None

    def _get_object_type(self, path: str) -> str:
        """Determine object type from path and backend."""
        # Check if path matches a backend mount
        for mount_point, backend in self.backends.items():
            if path.startswith(mount_point):
                return getattr(backend, "rebac_object_type", "file")

        # Default to "file"
        return "file"

    def _permission_to_name(self, permission: Permission) -> str:
        """Convert Permission flag to string name."""
        if permission == Permission.READ:
            return "read"
        elif permission == Permission.WRITE:
            return "write"
        elif permission == Permission.EXECUTE:
            return "execute"
        else:
            return "read"
