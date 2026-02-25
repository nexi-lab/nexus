"""KernelDispatch — unified three-phase VFS dispatch.

Single dispatch point for all kernel VFS operation notifications.
Every VFS operation (read/write/delete/rename/mkdir/rmdir) passes
through three ordered phases:

    PRE-DISPATCH  (first-match short-circuit)
    └── Registered VFSPathResolver chain.
        First resolver whose ``matches(path)`` returns True handles
        the entire operation — normal VFS pipeline is skipped.
        Each resolver owns its own permission semantics.

    INTERCEPT  (synchronous, ordered — pre + post sub-phases)
    └── Registered interceptor hooks (per-operation hook lists).
        PRE:  ``intercept_pre_*()`` — hooks may abort by raising
              (e.g. PermissionError).  All exceptions propagate.
        POST: ``intercept_post_*()`` — hooks modify context or audit.
              Only AuditLogError aborts; others become warnings.

    OBSERVE  (fire-and-forget)
    └── Registered mutation observers receive a frozen FileEvent.
        Used for cache invalidation, telemetry, dependency tracking.
        Failures are caught and logged.  Never abort.

Linux kernel analogy:
    PRE-DISPATCH ≈ VFS ``file->f_op`` dispatch (procfs, sysfs, devtmpfs)
    INTERCEPT    ≈ LSM ``call_void_hook()`` chain
    OBSERVE      ≈ ``fsnotify()`` / ``notifier_call_chain()``

Lifecycle:
    Kernel creates KernelDispatch with empty callback lists at init.
    Factory registers resolvers, interceptor hooks, and observers at boot.
    Kernel call sites invoke ``resolve_*()`` then ``intercept_post_*()``
    then ``notify()``.
    Empty lists = no-op dispatch = zero overhead when no services.

Issue #900, #889.
"""

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.operation_result import OperationWarning
from nexus.contracts.vfs_hooks import (
    DeleteHookContext,
    MkdirHookContext,
    ReadHookContext,
    RenameHookContext,
    RmdirHookContext,
    VFSDeleteHook,
    VFSMkdirHook,
    VFSObserver,
    VFSReadHook,
    VFSRenameHook,
    VFSRmdirHook,
    VFSWriteBatchHook,
    VFSWriteHook,
    WriteBatchHookContext,
    WriteHookContext,
)
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import VFSPathResolver

logger = logging.getLogger(__name__)


class KernelDispatch:
    """Unified three-phase VFS dispatch (PRE-DISPATCH / INTERCEPT / OBSERVE).

    Construction (kernel __init__):
        self._dispatch = KernelDispatch()   # empty callback lists

    Registration (factory at boot — NOT kernel code):
        dispatch.register_intercept_read(some_hook)
        dispatch.register_observe(some_observer)

    Dispatch (kernel VFS call sites):
        dispatch.resolve_read(path)          # phase 0: PRE-DISPATCH
        dispatch.intercept_pre_read(ctx)     # phase 1a: INTERCEPT (pre)
        ...actual VFS operation...
        dispatch.intercept_post_read(ctx)    # phase 1b: INTERCEPT (post)
        dispatch.notify(event)               # phase 2: OBSERVE
    """

    __slots__ = (
        "_resolvers",
        "_read_hooks",
        "_write_hooks",
        "_write_batch_hooks",
        "_delete_hooks",
        "_rename_hooks",
        "_mkdir_hooks",
        "_rmdir_hooks",
        "_observers",
    )

    def __init__(self) -> None:
        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        self._resolvers: list[VFSPathResolver] = []

        # INTERCEPT: per-operation hook lists
        self._read_hooks: list[VFSReadHook] = []
        self._write_hooks: list[VFSWriteHook] = []
        self._write_batch_hooks: list[VFSWriteBatchHook] = []
        self._delete_hooks: list[VFSDeleteHook] = []
        self._rename_hooks: list[VFSRenameHook] = []
        self._mkdir_hooks: list[VFSMkdirHook] = []
        self._rmdir_hooks: list[VFSRmdirHook] = []

        # OBSERVE: generic mutation observers
        self._observers: list[VFSObserver] = []

    # ── PRE-DISPATCH: virtual path resolvers (Issue #889) ─────────────

    def register_resolver(self, resolver: "VFSPathResolver") -> None:
        """Register a PRE-DISPATCH virtual path resolver."""
        self._resolvers.append(resolver)

    def resolve_read(
        self,
        path: str,
        *,
        return_metadata: bool = False,
        context: Any = None,
    ) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for read.

        Returns (handled, result).  If handled is True the caller
        must return result and skip the normal VFS pipeline.
        """
        for r in self._resolvers:
            if r.matches(path):
                return True, r.read(path, return_metadata=return_metadata, context=context)
        return False, None

    def resolve_write(self, path: str, content: bytes) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for write."""
        for r in self._resolvers:
            if r.matches(path):
                return True, r.write(path, content)
        return False, None

    def resolve_delete(self, path: str, *, context: Any = None) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for delete."""
        for r in self._resolvers:
            if r.matches(path):
                r.delete(path, context=context)
                return True, {}
        return False, None

    @property
    def resolver_count(self) -> int:
        return len(self._resolvers)

    # ── register_intercept: per-operation INTERCEPT hooks ─────────────

    def register_intercept_read(self, hook: VFSReadHook) -> None:
        self._read_hooks.append(hook)

    def register_intercept_write(self, hook: VFSWriteHook) -> None:
        self._write_hooks.append(hook)

    def register_intercept_write_batch(self, hook: VFSWriteBatchHook) -> None:
        self._write_batch_hooks.append(hook)

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

    # ── PRE-INTERCEPT dispatch (Issue #899) ───────────────────────────
    # Reuses existing hook lists — calls on_pre_* via getattr.
    # Hooks that don't implement on_pre_* are silently skipped.
    # Exceptions propagate (abort operation) — LSM semantics.

    def intercept_pre_read(self, ctx: ReadHookContext) -> None:
        """PRE-INTERCEPT phase for read — hooks may abort by raising."""
        for hook in self._read_hooks:
            pre_fn = getattr(hook, "on_pre_read", None)
            if pre_fn is not None:
                pre_fn(ctx)

    def intercept_pre_write(self, ctx: WriteHookContext) -> None:
        """PRE-INTERCEPT phase for write — hooks may abort by raising."""
        for hook in self._write_hooks:
            pre_fn = getattr(hook, "on_pre_write", None)
            if pre_fn is not None:
                pre_fn(ctx)

    def intercept_pre_delete(self, ctx: DeleteHookContext) -> None:
        """PRE-INTERCEPT phase for delete — hooks may abort by raising."""
        for hook in self._delete_hooks:
            pre_fn = getattr(hook, "on_pre_delete", None)
            if pre_fn is not None:
                pre_fn(ctx)

    def intercept_pre_rename(self, ctx: RenameHookContext) -> None:
        """PRE-INTERCEPT phase for rename — hooks may abort by raising."""
        for hook in self._rename_hooks:
            pre_fn = getattr(hook, "on_pre_rename", None)
            if pre_fn is not None:
                pre_fn(ctx)

    def intercept_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        """PRE-INTERCEPT phase for mkdir — hooks may abort by raising."""
        for hook in self._mkdir_hooks:
            pre_fn = getattr(hook, "on_pre_mkdir", None)
            if pre_fn is not None:
                pre_fn(ctx)

    def intercept_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        """PRE-INTERCEPT phase for rmdir — hooks may abort by raising."""
        for hook in self._rmdir_hooks:
            pre_fn = getattr(hook, "on_pre_rmdir", None)
            if pre_fn is not None:
                pre_fn(ctx)

    # ── POST-INTERCEPT dispatch ────────────────────────────────────────

    def intercept_post_read(self, ctx: ReadHookContext) -> None:
        """INTERCEPT phase for read."""
        for hook in self._read_hooks:
            try:
                hook.on_post_read(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_read hook failed: {exc}",
                    )
                )

    def intercept_post_write(self, ctx: WriteHookContext) -> None:
        """INTERCEPT phase for write."""
        for hook in self._write_hooks:
            try:
                hook.on_post_write(ctx)
            except AuditLogError:
                raise
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
        items: list[tuple],
        *,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """INTERCEPT phase for batch write."""
        if not self._write_batch_hooks:
            return
        ctx = WriteBatchHookContext(items=items, zone_id=zone_id, agent_id=agent_id)
        for hook in self._write_batch_hooks:
            try:
                hook.on_post_write_batch(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_write_batch hook failed: {exc}",
                    )
                )

    def intercept_post_delete(self, ctx: DeleteHookContext) -> None:
        """INTERCEPT phase for delete."""
        for hook in self._delete_hooks:
            try:
                hook.on_post_delete(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_delete hook failed: {exc}",
                    )
                )

    def intercept_post_rename(self, ctx: RenameHookContext) -> None:
        """INTERCEPT phase for rename."""
        for hook in self._rename_hooks:
            try:
                hook.on_post_rename(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_rename hook failed: {exc}",
                    )
                )

    def intercept_post_mkdir(self, ctx: MkdirHookContext) -> None:
        """INTERCEPT phase for mkdir."""
        for hook in self._mkdir_hooks:
            try:
                hook.on_post_mkdir(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_mkdir hook failed: {exc}",
                    )
                )

    def intercept_post_rmdir(self, ctx: RmdirHookContext) -> None:
        """INTERCEPT phase for rmdir."""
        for hook in self._rmdir_hooks:
            try:
                hook.on_post_rmdir(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"post_rmdir hook failed: {exc}",
                    )
                )

    # ── OBSERVE dispatch ───────────────────────────────────────────────

    def notify(self, event: FileEvent) -> None:
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
    def write_batch_hook_count(self) -> int:
        return len(self._write_batch_hooks)

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
