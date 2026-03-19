"""VFS write hook for deferred permission buffer (Issue #1773).

Wraps ``DeferredPermissionBuffer.queue_hierarchy()`` /
``queue_owner_grant()`` as a proper KernelDispatch hook, eliminating
the kernel's getattr() calls to
``_system_services.deferred_permission_buffer``.

Data mapping:
    ctx.path          → path
    ctx.zone_id       → zone_id (default "root")
    ctx.context       → OperationContext (user_id, is_system)
    ctx.is_new_file   → only queue_owner_grant for new files
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.operation_result import OperationWarning

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import WriteHookContext

logger = logging.getLogger(__name__)


class DeferredPermissionHook:
    """Post-write hook that queues hierarchy + owner grants in background."""

    name = "deferred_permission"
    __slots__ = ("_buf",)

    # ── HotSwappable protocol (Issue #1773) ────────────────────────────

    def hook_spec(self) -> HookSpec:
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(write_hooks=(self,))

    async def drain(self) -> None:
        pass

    async def activate(self) -> None:
        pass

    def __init__(self, deferred_buffer: Any) -> None:
        self._buf = deferred_buffer

    # ── Hook callback ──────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        zone = ctx.zone_id or "root"
        try:
            self._buf.queue_hierarchy(ctx.path, zone)
            if ctx.is_new_file and ctx.context:
                user = ctx.context.user_id
                if user and not ctx.context.is_system:
                    self._buf.queue_owner_grant(user, ctx.path, zone)
        except Exception as e:
            ctx.warnings.append(
                OperationWarning(
                    severity="degraded",
                    component="deferred_permission",
                    message=f"queue failed: {e}",
                )
            )
