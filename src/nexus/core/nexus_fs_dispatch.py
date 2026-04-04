"""DispatchMixin — three-phase VFS dispatch (collapsed from KernelDispatch).

Mixin for NexusFS. Every VFS operation passes through three ordered phases:

    PRE-DISPATCH  (first-match short-circuit)
    └── Registered VFSPathResolver chain.
        First resolver whose ``try_*(path)`` returns non-None handles
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

Issue #900, #889, #1665.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.operation_result import OperationWarning
from nexus.contracts.vfs_hooks import (
    AccessHookContext,
    CopyHookContext,
    DeleteHookContext,
    MkdirHookContext,
    MountHookContext,
    ReadHookContext,
    RenameHookContext,
    RmdirHookContext,
    StatHookContext,
    UnmountHookContext,
    VFSMountHook,
    VFSObserver,
    VFSUnmountHook,
    WriteBatchHookContext,
    WriteHookContext,
)
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import VFSPathResolver

logger = logging.getLogger(__name__)


class DispatchMixin:
    """Three-phase VFS dispatch mixin for NexusFS.

    Collapsed from KernelDispatch (PR 7c). Dispatch state lives directly
    on NexusFS — no separate object, no indirection.

    Expects ``self._kernel`` (Rust Kernel) to be set before use.
    """

    _kernel: Any  # Rust Kernel — set by NexusFS.__init__

    def _init_dispatch(self) -> None:
        """Initialize dispatch state. Called from NexusFS.__init__."""
        self._trie_resolvers: dict[int, Any] = {}
        self._fallback_resolvers: list[Any] = []
        self._next_resolver_idx: int = 0
        self._hooks_nonempty: set[str] = set()
        self._mount_hooks: list[VFSMountHook] = []
        self._unmount_hooks: list[VFSUnmountHook] = []
        self._background_tasks: set[asyncio.Task] = set()

    # ── Lifecycle (Issue #3391) ──────────────────────────────────────────

    def _on_background_task_done(self, task: asyncio.Task) -> None:
        """Done-callback for background observer tasks — log exceptions, discard ref."""
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Background observer task %s failed: %s", task.get_name(), exc)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        """Drain background observer tasks (call during kernel teardown).

        Handles cross-loop scenarios (e.g. TestClient calling aclose() on
        a different event loop than the one that spawned the tasks).
        """
        if not self._background_tasks:
            return
        _pending = set(self._background_tasks)
        try:
            done, still_pending = await asyncio.wait(_pending, timeout=timeout)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        except RuntimeError:
            # Tasks belong to a different event loop (e.g. pytest loop vs
            # TestClient loop in Python 3.13+). Best-effort cancel + discard.
            for task in _pending:
                task.cancel()
        finally:
            self._background_tasks.clear()

    # ── PRE-DISPATCH: virtual path resolvers (Issue #889, #1317) ──────

    def register_resolver(self, resolver: "VFSPathResolver") -> None:
        """Register a PRE-DISPATCH virtual path resolver.

        If the resolver declares ``TRIE_PATTERN`` (class attribute), it is
        routed via the Rust PathTrie for O(depth) lookup.  Otherwise it is
        appended to the fallback linear-scan list.
        """
        pattern: str | None = getattr(resolver, "TRIE_PATTERN", None)
        if isinstance(pattern, str) and pattern:
            idx = self._next_resolver_idx
            self._next_resolver_idx += 1
            self._kernel.trie_register(pattern, idx)
            self._trie_resolvers[idx] = resolver
        else:
            self._fallback_resolvers.append(resolver)

    def _resolve(self, path: str, method: str, **kwargs: Any) -> tuple[bool, Any]:
        """Generic PRE-DISPATCH: trie lookup → fallback scan."""
        idx = self._kernel.trie_lookup(path)
        if idx is not None:
            resolver = self._trie_resolvers.get(idx)
            if resolver is not None:
                result = getattr(resolver, method)(path, **kwargs)
                if result is not None:
                    return True, result
        for r in self._fallback_resolvers:
            result = getattr(r, method)(path, **kwargs)
            if result is not None:
                return True, result
        return False, None

    def resolve_read(self, path: str, *, context: Any = None) -> tuple[bool, bytes | None]:
        """PRE-DISPATCH: first-match resolver for read."""
        return self._resolve(path, "try_read", context=context)

    def resolve_write(self, path: str, content: bytes) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for write."""
        return self._resolve(path, "try_write", content=content)

    def resolve_delete(self, path: str, *, context: Any = None) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for delete."""
        return self._resolve(path, "try_delete", context=context)

    @property
    def resolver_count(self) -> int:
        return len(self._trie_resolvers) + len(self._fallback_resolvers)

    # ── register_intercept: per-operation INTERCEPT hooks ─────────────

    def _mark_hook(self, op: str) -> None:
        self._hooks_nonempty.add(op)
        self._sync_hook_count(op)

    def _unmark_hook(self, op: str) -> None:
        if self._kernel is not None and self._kernel.hook_count(op) == 0:
            self._hooks_nonempty.discard(op)
        self._sync_hook_count(op)

    def _sync_hook_count(self, op: str) -> None:
        """Push hook count to Rust Kernel bitmap."""
        if self._kernel is not None:
            self._kernel.set_hook_count(op, int(self._kernel.hook_count(op)))

    def register_intercept(self, op: str, hook: Any) -> None:
        """Register an INTERCEPT hook for the given operation.

        Delegates to Rust HookRegistry. Callers use named wrappers below.
        """
        self._kernel.register_hook(op, hook)
        self._mark_hook(op)

    # Named registration wrappers — preserve existing call sites.
    def register_intercept_read(self, hook: Any) -> None:
        self.register_intercept("read", hook)

    def register_intercept_write(self, hook: Any) -> None:
        self.register_intercept("write", hook)

    def register_intercept_write_batch(self, hook: Any) -> None:
        self.register_intercept("write_batch", hook)

    def register_intercept_delete(self, hook: Any) -> None:
        self.register_intercept("delete", hook)

    def register_intercept_rename(self, hook: Any) -> None:
        self.register_intercept("rename", hook)

    def register_intercept_copy(self, hook: Any) -> None:
        self.register_intercept("copy", hook)

    def register_intercept_mkdir(self, hook: Any) -> None:
        self.register_intercept("mkdir", hook)

    def register_intercept_rmdir(self, hook: Any) -> None:
        self.register_intercept("rmdir", hook)

    def register_intercept_stat(self, hook: Any) -> None:
        self.register_intercept("stat", hook)

    def register_intercept_access(self, hook: Any) -> None:
        self.register_intercept("access", hook)

    # ── unregister ─────────────────────────────────────────────────────

    def unregister_resolver(self, resolver: "VFSPathResolver") -> bool:
        """Remove a PRE-DISPATCH resolver. Returns True if found."""
        for idx, r in list(self._trie_resolvers.items()):
            if r is resolver:
                self._kernel.trie_unregister(idx)
                del self._trie_resolvers[idx]
                return True
        try:
            self._fallback_resolvers.remove(resolver)
            return True
        except ValueError:
            return False

    def unregister_intercept(self, op: str, hook: Any) -> bool:
        """Unregister an INTERCEPT hook. Returns True if found."""
        r = bool(self._kernel.unregister_hook(op, hook))
        if r:
            self._unmark_hook(op)
        return r

    # Named unregistration wrappers — preserve existing call sites.
    def unregister_intercept_read(self, hook: Any) -> bool:
        return self.unregister_intercept("read", hook)

    def unregister_intercept_write(self, hook: Any) -> bool:
        return self.unregister_intercept("write", hook)

    def unregister_intercept_write_batch(self, hook: Any) -> bool:
        return self.unregister_intercept("write_batch", hook)

    def unregister_intercept_delete(self, hook: Any) -> bool:
        return self.unregister_intercept("delete", hook)

    def unregister_intercept_rename(self, hook: Any) -> bool:
        return self.unregister_intercept("rename", hook)

    def unregister_intercept_copy(self, hook: Any) -> bool:
        return self.unregister_intercept("copy", hook)

    def unregister_intercept_mkdir(self, hook: Any) -> bool:
        return self.unregister_intercept("mkdir", hook)

    def unregister_intercept_rmdir(self, hook: Any) -> bool:
        return self.unregister_intercept("rmdir", hook)

    def unregister_intercept_stat(self, hook: Any) -> bool:
        return self.unregister_intercept("stat", hook)

    def unregister_intercept_access(self, hook: Any) -> bool:
        return self.unregister_intercept("access", hook)

    # ── register_observe: generic OBSERVE observers (Issue #1748) ───────

    def register_observe(self, obs: VFSObserver) -> None:
        from nexus.core.file_events import ALL_FILE_EVENTS

        mask = getattr(obs, "event_mask", ALL_FILE_EVENTS)
        self._kernel.register_observer(obs, mask)

    def has_hooks(self, op: str) -> bool:
        """O(1) check: any hooks registered for *op*? Avoids HookContext construction."""
        return op in self._hooks_nonempty

    def unregister_observe(self, obs: VFSObserver) -> bool:
        return bool(self._kernel.unregister_observer(obs))

    # ── PRE-INTERCEPT dispatch (Issue #899) ───────────────────────────
    # Uses HookRegistry.get_pre_hooks() — pre-filtered at registration.
    # Exceptions propagate (abort operation) — LSM semantics.

    def intercept_pre_read(self, ctx: ReadHookContext) -> None:
        """PRE-INTERCEPT phase for read — hooks may abort by raising."""
        if "read" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("read"):
            hook.on_pre_read(ctx)

    def intercept_pre_write(self, ctx: WriteHookContext) -> None:
        """PRE-INTERCEPT phase for write — hooks may abort by raising."""
        if "write" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("write"):
            hook.on_pre_write(ctx)

    def intercept_pre_delete(self, ctx: DeleteHookContext) -> None:
        """PRE-INTERCEPT phase for delete — hooks may abort by raising."""
        if "delete" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("delete"):
            hook.on_pre_delete(ctx)

    def intercept_pre_rename(self, ctx: RenameHookContext) -> None:
        """PRE-INTERCEPT phase for rename — hooks may abort by raising."""
        if "rename" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("rename"):
            hook.on_pre_rename(ctx)

    def intercept_pre_copy(self, ctx: CopyHookContext) -> None:
        """PRE-INTERCEPT phase for copy — hooks may abort by raising."""
        if self._kernel is None:
            return
        for hook in self._kernel.get_pre_hooks("copy"):
            hook.on_pre_copy(ctx)

    def intercept_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        """PRE-INTERCEPT phase for mkdir — hooks may abort by raising."""
        if "mkdir" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("mkdir"):
            hook.on_pre_mkdir(ctx)

    def intercept_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        """PRE-INTERCEPT phase for rmdir — hooks may abort by raising."""
        if "rmdir" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("rmdir"):
            hook.on_pre_rmdir(ctx)

    def intercept_pre_stat(self, ctx: StatHookContext) -> None:
        """PRE-INTERCEPT phase for stat — hooks may abort by raising."""
        if "stat" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("stat"):
            hook.on_pre_stat(ctx)

    def intercept_pre_access(self, ctx: AccessHookContext) -> None:
        """PRE-INTERCEPT phase for access — hooks may abort by raising."""
        if "access" not in self._hooks_nonempty:
            return
        for hook in self._kernel.get_pre_hooks("access"):
            hook.on_pre_access(ctx)

    # ── POST-INTERCEPT dispatch ────────────────────────────────────────

    async def _post_dispatch(self, op: str, method: str, ctx: Any, *, timeout: float = 5.0) -> None:
        """Shared POST dispatch — sync hooks serial, async hooks parallel.

        Sync hooks: direct call, fault-isolated (try/except per hook).
        Async hooks: ``asyncio.gather`` with per-hook timeout.
        Only ``AuditLogError`` aborts; other exceptions become warnings.
        """
        if op not in self._hooks_nonempty:
            return
        sync_hooks, async_hooks = self._kernel.get_post_hooks(op)

        # Sync: serial, fault-isolated
        for hook in sync_hooks:
            try:
                getattr(hook, method)(ctx)
            except AuditLogError:
                raise
            except Exception as exc:
                ctx.warnings.append(
                    OperationWarning(
                        severity="degraded",
                        component=hook.name,
                        message=f"{method} hook failed: {exc}",
                    )
                )

        # Async: parallel with per-hook timeout
        if async_hooks:
            tasks = [
                asyncio.create_task(asyncio.wait_for(getattr(h, method)(ctx), timeout=timeout))
                for h in async_hooks
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for hook, result in zip(async_hooks, results, strict=True):
                if isinstance(result, AuditLogError):
                    raise result
                elif isinstance(result, Exception):
                    ctx.warnings.append(
                        OperationWarning(
                            severity="degraded",
                            component=hook.name,
                            message=f"{method} hook failed: {result}",
                        )
                    )

    async def intercept_post_read(self, ctx: ReadHookContext) -> None:
        await self._post_dispatch("read", "on_post_read", ctx)

    async def intercept_post_write(self, ctx: WriteHookContext) -> None:
        await self._post_dispatch("write", "on_post_write", ctx)

    async def intercept_post_write_batch(
        self,
        items: list[tuple],
        *,
        context: Any = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
    ) -> None:
        """INTERCEPT phase for batch write."""
        if self._kernel.hook_count("write_batch") == 0:
            return
        ctx = WriteBatchHookContext(
            items=items, context=context, zone_id=zone_id, agent_id=agent_id
        )
        await self._post_dispatch("write_batch", "on_post_write_batch", ctx)

    async def intercept_post_delete(self, ctx: DeleteHookContext) -> None:
        await self._post_dispatch("delete", "on_post_delete", ctx)

    async def intercept_post_rename(self, ctx: RenameHookContext) -> None:
        await self._post_dispatch("rename", "on_post_rename", ctx)

    async def intercept_post_copy(self, ctx: CopyHookContext) -> None:
        await self._post_dispatch("copy", "on_post_copy", ctx)

    async def intercept_post_mkdir(self, ctx: MkdirHookContext) -> None:
        await self._post_dispatch("mkdir", "on_post_mkdir", ctx)

    async def intercept_post_rmdir(self, ctx: RmdirHookContext) -> None:
        await self._post_dispatch("rmdir", "on_post_rmdir", ctx)

    # ── OBSERVE dispatch (Issue #1812, #1748, #3391) ──────────────────────

    async def notify(self, event: FileEvent) -> None:
        """OBSERVE phase — hybrid inline/deferred dispatch.

        Inline observers (``OBSERVE_INLINE=True``, default): run via
        ``asyncio.gather`` on the caller's path — suited for fast,
        in-process work (e.g. resolving FileWatcher futures).

        Deferred observers (``OBSERVE_INLINE=False``): spawned as tracked
        background tasks — true fire-and-forget from the caller's
        perspective (e.g. EventBus publish to Redis/NATS).

        Issue #3391: inspired by DFUSE (arXiv:2503.18191) — eliminate
        I/O round-trips from the write critical path.
        """
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        event_type = event.type if isinstance(event.type, FileEventType) else None
        bit = FILE_EVENT_BIT.get(event_type, 0) if event_type else 0
        if not bit:
            return
        observers = self._kernel.get_matching_observers(bit)
        if not observers:
            return

        inline: list[tuple[Any, str]] = []
        deferred: list[tuple[Any, str]] = []
        for obs, name in observers:
            if getattr(obs, "OBSERVE_INLINE", True):
                inline.append((obs, name))
            else:
                deferred.append((obs, name))

        async def _safe(obs: Any, name: str) -> None:
            from nexus.lib.lock_order import enter_observer_context, exit_observer_context

            enter_observer_context()
            try:
                await obs.on_mutation(event)
            except Exception as exc:
                logger.warning("Observer %s failed: %s", name, exc)
            finally:
                exit_observer_context()

        # Inline: await on caller's path (fast observers)
        if inline:
            await asyncio.gather(*(_safe(obs, name) for obs, name in inline))

        # Deferred: fire-and-forget background tasks (I/O-bound observers)
        for obs, name in deferred:
            task = asyncio.create_task(_safe(obs, name), name=f"observe-{name}")
            self._background_tasks.add(task)
            task.add_done_callback(self._on_background_task_done)

    # ── MOUNT/UNMOUNT hooks (Issue #1811) ──────────────────────────────

    def register_mount_hook(self, hook: VFSMountHook) -> None:
        self._mount_hooks.append(hook)

    def register_unmount_hook(self, hook: VFSUnmountHook) -> None:
        self._unmount_hooks.append(hook)

    def unregister_mount_hook(self, hook: VFSMountHook) -> bool:
        try:
            self._mount_hooks.remove(hook)
            return True
        except ValueError:
            return False

    def unregister_unmount_hook(self, hook: VFSUnmountHook) -> bool:
        try:
            self._unmount_hooks.remove(hook)
            return True
        except ValueError:
            return False

    def notify_mount(self, mount_point: str, backend: Any) -> None:
        """Fire-and-forget mount notification to all registered hooks."""
        if not self._mount_hooks:
            return
        ctx = MountHookContext(mount_point=mount_point, backend=backend)
        for hook in self._mount_hooks:
            try:
                hook.on_mount(ctx)
            except Exception as exc:
                logger.warning("Mount hook %s failed: %s", type(hook).__name__, exc)

    def notify_unmount(self, mount_point: str, backend: Any) -> None:
        """Fire-and-forget unmount notification to all registered hooks."""
        if not self._unmount_hooks:
            return
        ctx = UnmountHookContext(mount_point=mount_point, backend=backend)
        for hook in self._unmount_hooks:
            try:
                hook.on_unmount(ctx)
            except Exception as exc:
                logger.warning("Unmount hook %s failed: %s", type(hook).__name__, exc)

    # ── Hook counts — delegate to Rust Kernel ────────────────────────

    def _hook_count(self, op: str) -> int:
        return int(self._kernel.hook_count(op))

    # Properties for backward compat with existing callers.
    read_hook_count = property(lambda self: self._hook_count("read"))
    write_hook_count = property(lambda self: self._hook_count("write"))
    write_batch_hook_count = property(lambda self: self._hook_count("write_batch"))
    delete_hook_count = property(lambda self: self._hook_count("delete"))
    rename_hook_count = property(lambda self: self._hook_count("rename"))
    copy_hook_count = property(lambda self: self._hook_count("copy"))
    mkdir_hook_count = property(lambda self: self._hook_count("mkdir"))
    rmdir_hook_count = property(lambda self: self._hook_count("rmdir"))
    stat_hook_count = property(lambda self: self._hook_count("stat"))
    access_hook_count = property(lambda self: self._hook_count("access"))
    observer_count = property(lambda self: int(self._kernel.observer_count()))
    mount_hook_count = property(lambda self: len(self._mount_hooks))
    unmount_hook_count = property(lambda self: len(self._unmount_hooks))
