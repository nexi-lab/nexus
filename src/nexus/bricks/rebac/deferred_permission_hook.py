"""VFS hooks for deferred permission buffer (Issue #1773, #1682).

Wraps ``DeferredPermissionBuffer.queue_hierarchy()`` /
``queue_owner_grant()`` as proper KernelDispatch hooks, eliminating
direct kernel coupling to the deferred permission buffer.

Data mapping:
    ctx.path          → path
    ctx.zone_id       → zone_id (default "root")
    ctx.context       → OperationContext (user_id, is_system)
    ctx.is_new_file   → only queue_owner_grant for new files

For rename, we call ``rebac_manager.update_object_path()`` directly
(not via the deferred buffer) because path updates must be immediate.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.operation_result import OperationWarning

if TYPE_CHECKING:
    from nexus.contracts.protocols.service_hooks import HookSpec
    from nexus.contracts.vfs_hooks import (
        MkdirHookContext,
        RenameHookContext,
        WriteBatchHookContext,
        WriteHookContext,
    )

logger = logging.getLogger(__name__)


class DeferredPermissionHook:
    """Post-write/mkdir/write_batch/rename hook that queues hierarchy + owner grants."""

    name = "deferred_permission"
    __slots__ = ("_buf", "_rebac")

    # ── Hook spec (duck-typed) (Issue #1773) ──────────────────────────

    def hook_spec(self) -> HookSpec:
        from nexus.contracts.protocols.service_hooks import HookSpec

        return HookSpec(
            write_hooks=(self,),
            mkdir_hooks=(self,),
            write_batch_hooks=(self,),
            rename_hooks=(self,),
        )

    def __init__(self, deferred_buffer: Any, rebac_manager: Any | None = None) -> None:
        self._buf = deferred_buffer
        self._rebac = rebac_manager

    # ── Hook callbacks ────────────────────────────────────────────────

    def on_post_write(self, ctx: WriteHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
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

    def on_post_mkdir(self, ctx: MkdirHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        try:
            self._buf.queue_hierarchy(ctx.path, zone)
            if ctx.context:
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

    def on_post_write_batch(self, ctx: WriteBatchHookContext) -> None:
        zone = ctx.zone_id or ROOT_ZONE_ID
        try:
            for meta, is_new in ctx.items:
                self._buf.queue_hierarchy(meta.path, zone)
                if is_new and ctx.context:
                    user = ctx.context.user_id
                    if user and not ctx.context.is_system:
                        self._buf.queue_owner_grant(user, meta.path, zone)
        except Exception as e:
            ctx.warnings.append(
                OperationWarning(
                    severity="degraded",
                    component="deferred_permission",
                    message=f"queue failed: {e}",
                )
            )

    def on_post_rename(self, ctx: RenameHookContext) -> None:
        if self._rebac is None:
            return
        try:
            self._rebac.update_object_path(
                old_path=ctx.old_path,
                new_path=ctx.new_path,
                object_type="file",
                is_directory=ctx.is_directory,
            )
        except Exception as e:
            ctx.warnings.append(
                OperationWarning(
                    severity="degraded",
                    component="deferred_permission",
                    message=f"update_object_path failed: {e}",
                )
            )
