"""KernelDispatch — unified two-phase VFS notification dispatch.

Single dispatch point for all kernel VFS operation notifications.
Every VFS operation (read/write/delete/rename/mkdir/rmdir) passes
through two ordered phases:

    INTERCEPT  (synchronous, ordered)
    ├── Built-in write observer (audit trail).
    │   Can abort the operation by raising AuditLogError.
    │   Error policy: audit_strict_mode controls raise-vs-log.
    └── Registered interceptor hooks (service-layer side effects).
        Can modify the operation context (e.g. filter CSV columns,
        update cache bitmaps).  Failures are caught and appended
        as OperationWarning — never abort the operation.

    OBSERVE  (fire-and-forget)
    └── Registered mutation observers receive a frozen MutationEvent.
        Used for cache invalidation, telemetry, dependency tracking.
        Failures are caught and logged.  Never abort.

Linux kernel analogy:
    INTERCEPT ≈ LSM ``call_void_hook()`` chain
    OBSERVE   ≈ ``fsnotify()`` / ``notifier_call_chain()``

Lifecycle:
    Factory constructs KernelDispatch with write_observer + audit config.
    Factory registers interceptor hooks and observers via DI.
    Kernel call sites invoke ``intercept_post_*()`` then ``notify()``.
    Empty hook lists = no-op dispatch = zero overhead when no services.

Issue #900.
"""

from __future__ import annotations

import logging
from typing import Any

from nexus.core.operation_result import OperationWarning
from nexus.contracts.vfs_hooks import (
    DeleteHookContext,
    MkdirHookContext,
    MutationEvent,
    ReadHookContext,
    RenameHookContext,
    RmdirHookContext,
    VFSDeleteHook,
    VFSMkdirHook,
    VFSObserver,
    VFSReadHook,
    VFSRenameHook,
    VFSRmdirHook,
    VFSWriteHook,
    WriteHookContext,
)

logger = logging.getLogger(__name__)


class KernelDispatch:
    """Unified two-phase VFS notification dispatch.

    Construction (factory):
        dispatch = KernelDispatch(write_observer=obs, audit_strict_mode=True)

    Registration (factory):
        dispatch.register_read_hook(DynamicViewerReadHook(...))
        dispatch.register_observer(CacheInvalidationObserver(...))

    Dispatch (kernel VFS call sites):
        dispatch.intercept_post_read(ctx)   # phase 1: INTERCEPT
        dispatch.notify(event)               # phase 2: OBSERVE
    """

    __slots__ = (
        "_write_observer",
        "_audit_strict_mode",
        "_read_hooks",
        "_write_hooks",
        "_delete_hooks",
        "_rename_hooks",
        "_mkdir_hooks",
        "_rmdir_hooks",
        "_observers",
    )

    def __init__(
        self,
        write_observer: Any | None = None,
        audit_strict_mode: bool = False,
    ) -> None:
        self._write_observer = write_observer
        self._audit_strict_mode = audit_strict_mode

        # INTERCEPT: per-operation hook lists
        self._read_hooks: list[VFSReadHook] = []
        self._write_hooks: list[VFSWriteHook] = []
        self._delete_hooks: list[VFSDeleteHook] = []
        self._rename_hooks: list[VFSRenameHook] = []
        self._mkdir_hooks: list[VFSMkdirHook] = []
        self._rmdir_hooks: list[VFSRmdirHook] = []

        # OBSERVE: generic mutation observers
        self._observers: list[VFSObserver] = []

    # ── register_intercept: per-operation INTERCEPT hooks ─────────────

    def register_intercept_read(self, hook: VFSReadHook) -> None:
        self._read_hooks.append(hook)

    def register_intercept_write(self, hook: VFSWriteHook) -> None:
        self._write_hooks.append(hook)

    def register_intercept_delete(self, hook: VFSDeleteHook) -> None:
        self._delete_hooks.append(hook)

    def register_intercept_rename(self, hook: VFSRenameHook) -> None:
        self._rename_hooks.append(hook)

    def register_intercept_mkdir(self, hook: VFSMkdirHook) -> None:
        self._mkdir_hooks.append(hook)

    def register_intercept_rmdir(self, hook: VFSRmdirHook) -> None:
        self._rmdir_hooks.append(hook)

    # ── register_observe: generic OBSERVE observers ────────────────────

    def register_observe(self, obs: VFSObserver) -> None:
        self._observers.append(obs)

    # ── INTERCEPT dispatch ─────────────────────────────────────────────

    def intercept_post_read(self, ctx: ReadHookContext) -> None:
        """INTERCEPT phase for read.  No write observer (reads are not mutations)."""
        for hook in self._read_hooks:
            try:
                hook.on_post_read(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_read hook failed: {exc}",
                    )
                )

    def intercept_post_write(self, ctx: WriteHookContext) -> None:
        """INTERCEPT phase for write.  Observer (audit) then hooks (side effects)."""
        self._dispatch_write_observer(
            "write",
            ctx.path,
            metadata=ctx.metadata,
            is_new=ctx.is_new_file,
            path=ctx.path,
            old_metadata=ctx.old_metadata,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )
        for hook in self._write_hooks:
            try:
                hook.on_post_write(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_write hook failed: {exc}",
                    )
                )

    def intercept_post_write_batch(
        self,
        items: list[tuple[Any, bool]],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """INTERCEPT phase for batch write.  Observer only (no per-file context)."""
        self._dispatch_write_observer(
            "write_batch",
            "<batch>",
            items=items,
            zone_id=zone_id,
            agent_id=agent_id,
        )

    def intercept_post_delete(self, ctx: DeleteHookContext) -> None:
        """INTERCEPT phase for delete.  Observer then hooks."""
        self._dispatch_write_observer(
            "delete",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            metadata=ctx.metadata,
        )
        for hook in self._delete_hooks:
            try:
                hook.on_post_delete(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_delete hook failed: {exc}",
                    )
                )

    def intercept_post_rename(self, ctx: RenameHookContext) -> None:
        """INTERCEPT phase for rename.  Observer then hooks."""
        self._dispatch_write_observer(
            "rename",
            ctx.old_path,
            old_path=ctx.old_path,
            new_path=ctx.new_path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            metadata=ctx.metadata,
        )
        for hook in self._rename_hooks:
            try:
                hook.on_post_rename(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_rename hook failed: {exc}",
                    )
                )

    def intercept_post_mkdir(self, ctx: MkdirHookContext) -> None:
        """INTERCEPT phase for mkdir.  Observer then hooks."""
        self._dispatch_write_observer(
            "mkdir",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
        )
        for hook in self._mkdir_hooks:
            try:
                hook.on_post_mkdir(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_mkdir hook failed: {exc}",
                    )
                )

    def intercept_post_rmdir(self, ctx: RmdirHookContext) -> None:
        """INTERCEPT phase for rmdir.  Observer then hooks."""
        self._dispatch_write_observer(
            "rmdir",
            ctx.path,
            path=ctx.path,
            zone_id=ctx.zone_id,
            agent_id=ctx.agent_id,
            recursive=ctx.recursive,
        )
        for hook in self._rmdir_hooks:
            try:
                hook.on_post_rmdir(ctx)
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_rmdir hook failed: {exc}",
                    )
                )

    # ── OBSERVE dispatch ───────────────────────────────────────────────

    def notify(self, event: MutationEvent) -> None:
        """OBSERVE phase — fire-and-forget to all registered observers."""
        for obs in self._observers:
            try:
                obs.on_mutation(event)
            except Exception as exc:
                logger.warning("Observer %s failed: %s", type(obs).__name__, exc)

    # ── Hook counts ────────────────────────────────────────────────────

    @property
    def read_hook_count(self) -> int:
        return len(self._read_hooks)

    @property
    def write_hook_count(self) -> int:
        return len(self._write_hooks)

    @property
    def delete_hook_count(self) -> int:
        return len(self._delete_hooks)

    @property
    def rename_hook_count(self) -> int:
        return len(self._rename_hooks)

    @property
    def mkdir_hook_count(self) -> int:
        return len(self._mkdir_hooks)

    @property
    def rmdir_hook_count(self) -> int:
        return len(self._rmdir_hooks)

    @property
    def observer_count(self) -> int:
        return len(self._observers)

    # ── Internal ───────────────────────────────────────────────────────

    def _dispatch_write_observer(self, operation: str, op_path: str, **kwargs: Any) -> None:
        """Built-in write observer dispatch (INTERCEPT, can abort).

        Error policy (from WriteObserverProtocol contract):
        - audit_strict_mode=True:  raise AuditLogError → aborts operation
        - audit_strict_mode=False: log critical warning → operation continues
        """
        if not self._write_observer:
            return

        try:
            method = getattr(self._write_observer, f"on_{operation}")
            method(**kwargs)
        except Exception as e:
            from nexus.contracts.exceptions import AuditLogError

            if self._audit_strict_mode:
                logger.error(
                    "AUDIT LOG FAILURE: %s on '%s' ABORTED. Error: %s. "
                    "Set audit_strict_mode=False to allow writes without audit logs.",
                    operation,
                    op_path,
                    e,
                )
                raise AuditLogError(
                    f"Operation aborted: audit logging failed for {operation}: {e}",
                    path=op_path,
                    original_error=e,
                ) from e
            else:
                logger.critical(
                    "AUDIT LOG FAILURE: %s on '%s' SUCCEEDED but audit log FAILED. "
                    "Error: %s. This creates an audit trail gap!",
                    operation,
                    op_path,
                    e,
                )
