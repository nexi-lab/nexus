# §11 Phase 22 — dispatch cleanup
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
        POST: ``dispatch_post_hooks(op, ctx)`` — hooks modify context or audit.
              Dispatched via Rust (fire-and-forget, fault-isolated).

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
    Kernel call sites invoke ``resolve_*()`` then ``dispatch_post_hooks()``
    then ``notify()``.
    Empty lists = no-op dispatch = zero overhead when no services.

Issue #900, #889, #1665.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.vfs_hooks import (
    VFSMountHook,
    VFSObserver,
    VFSUnmountHook,
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
        self._mount_hooks: list[tuple[Any, Any]] = []  # (hook, adapter) pairs
        self._unmount_hooks: list[tuple[Any, Any]] = []  # (hook, adapter) pairs
        # Observer registry — pure Python list (§11 Phase 22: eliminated Rust ObserverRegistry)
        self._observers: list[tuple[Any, str, int, bool]] = []  # (obs, name, mask, is_inline)
        import concurrent.futures

        self._observer_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="observe"
        )

    # ── Lifecycle (Issue #3391) ──────────────────────────────────────────

    def shutdown(self) -> None:
        """Shutdown observer executor (call during kernel teardown)."""
        self._observer_executor.shutdown(wait=False)

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
        if self._kernel is None:
            return False, None
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

    def _sync_hook_count(self, op: str) -> None:
        """Push hook count to Rust Kernel bitmap."""
        if self._kernel is not None:
            self._kernel.set_hook_count(op, int(self._kernel.hook_count(op)))

    def register_intercept(self, op: str, hook: Any) -> None:
        """Register an INTERCEPT hook for the given operation.

        Delegates to Rust HookRegistry. Callers use named wrappers below.
        """
        self._kernel.register_hook(op, hook)
        self._sync_hook_count(op)

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
            self._sync_hook_count(op)
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
        """Register OBSERVE observer (§11 Phase 22: pure Python registry)."""
        from nexus.core.file_events import ALL_FILE_EVENTS

        mask = getattr(obs, "event_mask", ALL_FILE_EVENTS)
        is_inline_attr = getattr(obs, "OBSERVE_INLINE", True)
        is_inline = bool(is_inline_attr) if isinstance(is_inline_attr, (bool, int)) else True
        name = getattr(obs, "__class__", type(obs)).__name__
        self._observers.append((obs, name, mask, is_inline))

    def has_hooks(self, op: str) -> bool:
        """O(1) check: any hooks registered for *op*? Delegates to Rust Kernel."""
        return bool(self._kernel.hook_count(op) > 0)

    def unregister_observe(self, obs: VFSObserver) -> bool:
        """Unregister OBSERVE observer by identity."""
        for i, (o, _name, _mask, _inline) in enumerate(self._observers):
            if o is obs:
                self._observers.pop(i)
                return True
        return False

    @property
    def observer_count(self) -> int:
        """Total registered observers."""
        return len(self._observers)

    # ── PRE-INTERCEPT dispatch ──────────────────────────────────────────
    # ALL pre-hook dispatch now goes through Rust InterceptHook trait via
    # self._kernel.dispatch_pre_hooks(op, ctx).  See PR 19 / PR 20.
    # Tier 1 syscalls (sys_read/write/unlink/rename/mkdir/rmdir) dispatch
    # pre-hooks internally.  Tier 2 methods call dispatch_pre_hooks() directly.

    # ── POST-INTERCEPT dispatch ────────────────────────────────────────
    # ALL post-hook dispatch goes through Rust via
    # self._kernel.dispatch_post_hooks(op, ctx).
    # Sync post-hooks: serial in Rust (fire-and-forget).

    # ── Event dispatch (DLC backward compat) ────────────────────────────

    def dispatch_event(self, event_type: str, path: str) -> None:
        """Dispatch a FileEvent through the OBSERVE pipeline.

        Creates a ``FileEvent`` from the event_type string and path, then
        delegates to ``notify()``.  Called by DriverLifecycleCoordinator
        for mount/unmount events.
        """
        from nexus.core.file_events import FileEventType

        try:
            fe_type: FileEventType | str = FileEventType(event_type)
        except ValueError:
            fe_type = event_type
        self.notify(FileEvent(type=fe_type, path=path))

    # ── OBSERVE dispatch (Issue #1812, #1748, #3391) ──────────────────────

    def notify(self, event: FileEvent) -> None:
        """OBSERVE phase — pure Python dispatch (§11 Phase 22).

        Iterates Python observer list directly (no Rust ObserverRegistry).
        Inline observers: synchronous on caller's thread.
        Deferred observers: submitted to ThreadPoolExecutor.
        """
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        event_type = event.type if isinstance(event.type, FileEventType) else None
        bit = FILE_EVENT_BIT.get(event_type, 0) if event_type else 0
        if not bit or not self._observers:
            return

        def _run_observer(obs: Any, name: str) -> None:
            try:
                obs.on_mutation(event)
            except Exception as exc:
                logger.warning("Observer %s failed: %s", name, exc)

        for obs, name, mask, is_inline in self._observers:
            if mask & bit == 0:
                continue
            if is_inline:
                _run_observer(obs, name)
            else:
                self._observer_executor.submit(_run_observer, obs, name)

    # ── MOUNT/UNMOUNT hooks (unified into OBSERVE phase) ────────────────
    #
    # Mount/unmount hooks are registered as observers with MOUNT/UNMOUNT
    # event_mask bits. Legacy API preserved — wraps hooks as observers.

    def register_mount_hook(self, hook: VFSMountHook) -> None:
        """Register a mount hook as an OBSERVE observer with MOUNT event mask."""
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        # Wrap VFSMountHook as an observer with on_mutation
        class _MountObserverAdapter:
            def __init__(self, h: VFSMountHook):
                self._hook = h
                self.event_mask = FILE_EVENT_BIT[FileEventType.MOUNT]

            def on_mutation(self, event: Any) -> None:
                from nexus.contracts.vfs_hooks import MountHookContext

                ctx = MountHookContext(
                    mount_point=event.path, backend=getattr(event, "_backend", None)
                )
                self._hook.on_mount(ctx)

        adapter = _MountObserverAdapter(hook)
        self._mount_hooks.append((hook, adapter))
        self.register_observe(adapter)

    def register_unmount_hook(self, hook: VFSUnmountHook) -> None:
        """Register an unmount hook as an OBSERVE observer with UNMOUNT event mask."""
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        class _UnmountObserverAdapter:
            def __init__(self, h: VFSUnmountHook):
                self._hook = h
                self.event_mask = FILE_EVENT_BIT[FileEventType.UNMOUNT]

            def on_mutation(self, event: Any) -> None:
                from nexus.contracts.vfs_hooks import UnmountHookContext

                ctx = UnmountHookContext(
                    mount_point=event.path, backend=getattr(event, "_backend", None)
                )
                self._hook.on_unmount(ctx)

        adapter = _UnmountObserverAdapter(hook)
        self._unmount_hooks.append((hook, adapter))
        self.register_observe(adapter)

    def unregister_mount_hook(self, hook: VFSMountHook) -> bool:
        for i, (h, adapter) in enumerate(self._mount_hooks):
            if h is hook:
                self._mount_hooks.pop(i)
                self.unregister_observe(adapter)
                return True
        return False

    def unregister_unmount_hook(self, hook: VFSUnmountHook) -> bool:
        for i, (h, adapter) in enumerate(self._unmount_hooks):
            if h is hook:
                self._unmount_hooks.pop(i)
                self.unregister_observe(adapter)
                return True
        return False

    def notify_mount(self, mount_point: str, backend: Any) -> None:
        """Fire-and-forget mount notification (synchronous)."""
        from nexus.contracts.vfs_hooks import MountHookContext

        if not self._mount_hooks:
            return
        ctx = MountHookContext(mount_point=mount_point, backend=backend)
        for hook, _adapter in self._mount_hooks:
            try:
                hook.on_mount(ctx)
            except Exception as exc:
                logger.warning("Mount hook %s failed: %s", type(hook).__name__, exc)

    def notify_unmount(self, mount_point: str, backend: Any) -> None:
        """Fire-and-forget unmount notification (synchronous)."""
        from nexus.contracts.vfs_hooks import UnmountHookContext

        if not self._unmount_hooks:
            return
        ctx = UnmountHookContext(mount_point=mount_point, backend=backend)
        for hook, _adapter in self._unmount_hooks:
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
    mount_hook_count = property(lambda self: len(self._mount_hooks))
    unmount_hook_count = property(lambda self: len(self._unmount_hooks))
