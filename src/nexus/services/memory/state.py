"""Memory state management service (#1498).

Extracts state transition operations (delete, approve, deactivate,
invalidate, revalidate) from the Memory god class into a composable,
independently testable class.

Usage:
    state_mgr = MemoryStateManager(memory_router, permission_enforcer, context)
    state_mgr.approve("mem_123")
    state_mgr.invalidate("mem_123", invalid_at="2026-01-15")
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from nexus.bricks.rebac.memory_permission_enforcer import MemoryPermissionEnforcer
from nexus.contracts.types import OperationContext, Permission
from nexus.core.temporal import parse_datetime
from nexus.services.memory.memory_router import MemoryViewRouter

logger = logging.getLogger(__name__)


class MemoryStateManager:
    """State transition operations for memories (#368, #1183, #1188).

    Manages memory lifecycle: approve, deactivate, delete, invalidate,
    revalidate — including batch variants.
    """

    def __init__(
        self,
        memory_router: MemoryViewRouter,
        permission_enforcer: MemoryPermissionEnforcer,
        context: OperationContext,
    ) -> None:
        self._memory_router = memory_router
        self._permission_enforcer = permission_enforcer
        self._context = context

    def delete(
        self,
        memory_id: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Delete a memory (#1188: soft-delete, preserves row).

        Args:
            memory_id: Memory ID to delete.
            context: Optional operation context to override identity.

        Returns:
            True if deleted, False if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        check_context = context or self._context
        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, check_context):
            return False

        return self._memory_router.delete_memory(memory_id)

    def approve(self, memory_id: str) -> bool:
        """Approve a memory (activate it) (#368).

        Args:
            memory_id: Memory ID to approve.

        Returns:
            True if approved, False if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, self._context):
            return False

        result = self._memory_router.approve_memory(memory_id)
        return result is not None

    def deactivate(self, memory_id: str) -> bool:
        """Deactivate a memory (make it inactive) (#368).

        Args:
            memory_id: Memory ID to deactivate.

        Returns:
            True if deactivated, False if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, self._context):
            return False

        result = self._memory_router.deactivate_memory(memory_id)
        return result is not None

    def invalidate(
        self,
        memory_id: str,
        invalid_at: datetime | str | None = None,
    ) -> bool:
        """Invalidate a memory (mark as no longer valid) (#1183).

        Temporal soft-delete that marks when a fact became false without
        removing the historical record.

        Args:
            memory_id: Memory ID to invalidate.
            invalid_at: When the fact became invalid. Defaults to now().

        Returns:
            True if invalidated, False if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, self._context):
            return False

        invalid_at_dt: datetime = datetime.now(UTC)
        if invalid_at is not None:
            if isinstance(invalid_at, str):
                parsed = parse_datetime(invalid_at)
                if parsed is not None:
                    invalid_at_dt = parsed
            else:
                invalid_at_dt = invalid_at

        result = self._memory_router.invalidate_memory(memory_id, invalid_at_dt)
        return result is not None

    def revalidate(self, memory_id: str) -> bool:
        """Revalidate a memory (clear invalid_at timestamp) (#1183).

        Use when a previously invalidated fact becomes true again.

        Args:
            memory_id: Memory ID to revalidate.

        Returns:
            True if revalidated, False if not found or no permission.
        """
        memory = self._memory_router.get_memory_by_id(memory_id)
        if not memory:
            return False

        if not self._permission_enforcer.check_memory(memory, Permission.WRITE, self._context):
            return False

        result = self._memory_router.revalidate_memory(memory_id)
        return result is not None

    def approve_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Approve multiple memories at once (#368)."""
        return self._batch_operation(memory_ids, self.approve, success_key="approved")

    def deactivate_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Deactivate multiple memories at once (#368)."""
        return self._batch_operation(memory_ids, self.deactivate, success_key="deactivated")

    def delete_batch(self, memory_ids: list[str]) -> dict[str, Any]:
        """Delete multiple memories at once (#368)."""
        return self._batch_operation(memory_ids, self.delete, success_key="deleted")

    def invalidate_batch(
        self, memory_ids: list[str], invalid_at: datetime | str | None = None
    ) -> dict[str, Any]:
        """Invalidate multiple memories at once (#1183)."""
        return self._batch_operation(
            memory_ids,
            lambda mid: self.invalidate(mid, invalid_at=invalid_at),
            success_key="invalidated",
        )

    def _batch_operation(
        self,
        memory_ids: list[str],
        operation: Callable[[str], bool],
        success_key: str = "success",
    ) -> dict[str, Any]:
        """Execute a batch operation with success/failure tracking."""
        success_ids: list[str] = []
        failed_ids: list[str] = []

        for memory_id in memory_ids:
            if operation(memory_id):
                success_ids.append(memory_id)
            else:
                failed_ids.append(memory_id)

        return {
            success_key: len(success_ids),
            "failed": len(failed_ids),
            f"{success_key}_ids": success_ids,
            "failed_ids": failed_ids,
        }
