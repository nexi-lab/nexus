"""VFS Hook Pipeline — kernel notification dispatch mechanism.

Aggregates registered VFS hooks and dispatches them in order after
each kernel VFS operation.  This is the kernel's dispatch mechanism
(like Linux's ``security_hook_heads`` + ``fsnotify_group``).

Architecture (KERNEL-ARCHITECTURE.md §3 "Kernel Notification Dispatch"):
- Kernel **knows** (has callback list attributes)
- Kernel does **not construct** (factory registers callbacks via DI)
- Empty lists = no-op dispatch = kernel operates with zero services

Contracts (context dataclasses + hook protocols) live in
``contracts/vfs_hooks.py`` (tier-neutral, like ``include/linux/notifier.h``).
Concrete implementations live in ``services/hooks/`` (policy).

Issue #625: Context types + protocols extracted to ``contracts/vfs_hooks.py``.
Pipeline dispatch stays in kernel (``core/vfs_hooks.py``).
"""

from __future__ import annotations

from nexus.contracts.types import OperationWarning

# Re-export contracts for backward compatibility — existing code imports
# from nexus.core.vfs_hooks.  New code should import from nexus.contracts.vfs_hooks.
from nexus.contracts.vfs_hooks import (  # noqa: F401
    DeleteHookContext,
    ReadHookContext,
    RenameHookContext,
    VFSDeleteHook,
    VFSReadHook,
    VFSRenameHook,
    VFSWriteHook,
    WriteHookContext,
)


class VFSHookPipeline:
    """Aggregates and dispatches VFS hooks in registration order.

    Kernel dispatch mechanism — like Linux's ``call_void_hook()`` loop.
    Hooks are registered at init time (not discovered at runtime).
    Each hook failure is caught, logged, and added as a warning —
    the core operation is never aborted by a hook failure.

    Usage (in factory/orchestrator.py):
        pipeline = VFSHookPipeline()
        pipeline.register_read_hook(DynamicViewerHook(...))
        pipeline.register_write_hook(AutoParseHook(...))
        pipeline.register_rename_hook(TigerCacheHook(...))
        # inject via SystemServices.hook_pipeline
    """

    def __init__(self) -> None:
        self._read_hooks: list[VFSReadHook] = []
        self._write_hooks: list[VFSWriteHook] = []
        self._delete_hooks: list[VFSDeleteHook] = []
        self._rename_hooks: list[VFSRenameHook] = []

    # --- Registration ---

    def register_read_hook(self, hook: VFSReadHook) -> None:
        self._read_hooks.append(hook)

    def register_write_hook(self, hook: VFSWriteHook) -> None:
        self._write_hooks.append(hook)

    def register_delete_hook(self, hook: VFSDeleteHook) -> None:
        self._delete_hooks.append(hook)

    def register_rename_hook(self, hook: VFSRenameHook) -> None:
        self._rename_hooks.append(hook)

    # --- Dispatch ---

    def run_post_read(self, ctx: ReadHookContext) -> None:
        """Run all registered post-read hooks in order."""
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

    def run_post_write(self, ctx: WriteHookContext) -> None:
        """Run all registered post-write hooks in order."""
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

    def run_post_delete(self, ctx: DeleteHookContext) -> None:
        """Run all registered post-delete hooks in order."""
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

    def run_post_rename(self, ctx: RenameHookContext) -> None:
        """Run all registered post-rename hooks in order."""
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
