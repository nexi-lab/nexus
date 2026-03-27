"""KernelDispatch — unified three-phase VFS dispatch.

Single dispatch point for all kernel VFS operation notifications.
Every VFS operation (read/write/delete/rename/mkdir/rmdir) passes
through three ordered phases:

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

import asyncio
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from nexus.contracts.exceptions import AuditLogError
from nexus.contracts.operation_result import OperationWarning

try:
    from nexus_fast import HookRegistry as _HookRegistry
    from nexus_fast import ObserverRegistry as _ObserverRegistry
    from nexus_fast import PathTrie as _PathTrie
except ImportError:  # pragma: no cover — Rust extension not built
    _PathTrie = None
    _HookRegistry = None
    _ObserverRegistry = None

from nexus.contracts.vfs_hooks import (
    AccessHookContext,
    DeleteHookContext,
    MkdirHookContext,
    MountHookContext,
    ReadHookContext,
    RenameHookContext,
    RmdirHookContext,
    StatHookContext,
    UnmountHookContext,
    VFSAccessHook,
    VFSDeleteHook,
    VFSMkdirHook,
    VFSMountHook,
    VFSObserver,
    VFSReadHook,
    VFSRenameHook,
    VFSRmdirHook,
    VFSStatHook,
    VFSUnmountHook,
    VFSWriteBatchHook,
    VFSWriteHook,
    WriteBatchHookContext,
    WriteHookContext,
)
from nexus.core.file_events import FileEvent

if TYPE_CHECKING:
    from nexus.contracts.vfs_hooks import VFSPathResolver

logger = logging.getLogger(__name__)


class _PythonHookRegistry:
    """Pure-Python fallback when ``nexus_fast`` is unavailable."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Any]] = defaultdict(list)
        # Bitmap for O(1) "any hooks registered?" check.
        # Updated on register/unregister. Callers check this before
        # constructing HookContext objects — saves ~300-700ns per syscall
        # when no hooks are registered for a given operation.
        self._nonempty: set[str] = set()

    def register(self, op: str, hook: Any) -> None:
        self._hooks[op].append(hook)
        self._nonempty.add(op)

    def unregister(self, op: str, hook: Any) -> bool:
        hooks = self._hooks.get(op, [])
        try:
            hooks.remove(hook)
            if not hooks:
                self._nonempty.discard(op)
            return True
        except ValueError:
            return False

    def count(self, op: str) -> int:
        return len(self._hooks.get(op, []))

    def get_pre_hooks(self, op: str) -> list[Any]:
        method = f"on_pre_{op}"
        return [hook for hook in self._hooks.get(op, []) if hasattr(hook, method)]

    def get_post_hooks(self, op: str) -> tuple[list[Any], list[Any]]:
        method = f"on_post_{op}"
        sync_hooks: list[Any] = []
        async_hooks: list[Any] = []
        for hook in self._hooks.get(op, []):
            fn = getattr(hook, method, None)
            if fn is None:
                continue
            if asyncio.iscoroutinefunction(fn):
                async_hooks.append(hook)
            else:
                sync_hooks.append(hook)
        return sync_hooks, async_hooks


class _PythonObserverRegistry:
    """Pure-Python fallback when Rust ``nexus_fast.ObserverRegistry`` is unavailable."""

    def __init__(self) -> None:
        self._entries: list[tuple[Any, str, int]] = []

    def register(self, obs: Any, event_mask: int) -> None:
        name = type(obs).__name__
        self._entries.append((obs, name, event_mask))

    def unregister(self, obs: Any) -> bool:
        for i, (o, _, _) in enumerate(self._entries):
            if o is obs:
                self._entries.pop(i)
                return True
        return False

    def get_matching(self, event_type_bit: int) -> list[tuple[Any, str]]:
        return [(obs, name) for obs, name, mask in self._entries if mask & event_type_bit]

    def count(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"_PythonObserverRegistry(count={len(self._entries)})"


class KernelDispatch:
    """Unified three-phase VFS dispatch (PRE-DISPATCH / INTERCEPT / OBSERVE).

    Construction (kernel __init__):
        self._dispatch = KernelDispatch()   # empty callback lists

    Registration (factory at boot — NOT kernel code):
        dispatch.register_intercept_read(some_hook)
        dispatch.register_observe(some_observer)

    Dispatch (kernel VFS call sites):
        dispatch.resolve_read(path)          # phase 0: PRE-DISPATCH (try_read)
        dispatch.intercept_pre_read(ctx)     # phase 1a: INTERCEPT (pre)
        ...actual VFS operation...
        dispatch.intercept_post_read(ctx)    # phase 1b: INTERCEPT (post)
        await dispatch.notify(event)         # phase 2: OBSERVE
    """

    __slots__ = (
        "_trie",
        "_trie_resolvers",
        "_fallback_resolvers",
        "_next_resolver_idx",
        "_registry",
        "_observer_registry",
        "_mount_hooks",
        "_unmount_hooks",
    )

    def __init__(self) -> None:
        # PRE-DISPATCH: trie for O(depth) routing + fallback list (Issue #1317)
        self._trie = _PathTrie() if _PathTrie is not None else None
        self._trie_resolvers: dict[int, VFSPathResolver] = {}
        self._fallback_resolvers: list[VFSPathResolver] = []
        self._next_resolver_idx: int = 0

        # INTERCEPT: prefer Rust HookRegistry, fall back to pure Python for source checkouts.
        self._registry: Any = (
            _HookRegistry() if _HookRegistry is not None else _PythonHookRegistry()
        )

        # OBSERVE: Rust ObserverRegistry with event-type bitmask filtering (Issue #1748).
        self._observer_registry: Any = (
            _ObserverRegistry() if _ObserverRegistry is not None else _PythonObserverRegistry()
        )

        # MOUNT/UNMOUNT: driver lifecycle hooks (Issue #1811)
        self._mount_hooks: list[VFSMountHook] = []
        self._unmount_hooks: list[VFSUnmountHook] = []

    # ── PRE-DISPATCH: virtual path resolvers (Issue #889, #1317) ──────

    def register_resolver(self, resolver: "VFSPathResolver") -> None:
        """Register a PRE-DISPATCH virtual path resolver.

        If the resolver declares ``TRIE_PATTERN`` (class attribute), it is
        routed via the Rust PathTrie for O(depth) lookup.  Otherwise it is
        appended to the fallback linear-scan list.
        """
        pattern: str | None = getattr(resolver, "TRIE_PATTERN", None)
        if isinstance(pattern, str) and pattern and self._trie is not None:
            idx = self._next_resolver_idx
            self._next_resolver_idx += 1
            self._trie.register(pattern, idx)
            self._trie_resolvers[idx] = resolver
        else:
            self._fallback_resolvers.append(resolver)

    def resolve_read(
        self,
        path: str,
        *,
        context: Any = None,
    ) -> tuple[bool, bytes | None]:
        """PRE-DISPATCH: first-match resolver for read (#1665).

        Returns (handled, result):
            handled=True,  result=content — resolver handled the read.
            handled=False, result=None    — no resolver matched.

        Trie resolvers are checked first (~50ns), then fallback list.
        """
        # Phase 1: Rust trie lookup
        if self._trie is not None:
            idx = self._trie.lookup(path)
            if idx is not None:
                resolver = self._trie_resolvers.get(idx)
                if resolver is not None:
                    result = resolver.try_read(path, context=context)
                    if result is not None:
                        return True, result
        # Phase 2: fallback linear scan
        for r in self._fallback_resolvers:
            result = r.try_read(path, context=context)
            if result is not None:
                return True, result
        return False, None

    def resolve_write(self, path: str, content: bytes) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for write (#1665)."""
        if self._trie is not None:
            idx = self._trie.lookup(path)
            if idx is not None:
                resolver = self._trie_resolvers.get(idx)
                if resolver is not None:
                    result = resolver.try_write(path, content)
                    if result is not None:
                        return True, result
        for r in self._fallback_resolvers:
            result = r.try_write(path, content)
            if result is not None:
                return True, result
        return False, None

    def resolve_delete(self, path: str, *, context: Any = None) -> tuple[bool, Any]:
        """PRE-DISPATCH: first-match resolver for delete (#1665)."""
        if self._trie is not None:
            idx = self._trie.lookup(path)
            if idx is not None:
                resolver = self._trie_resolvers.get(idx)
                if resolver is not None:
                    result = resolver.try_delete(path, context=context)
                    if result is not None:
                        return True, result
        for r in self._fallback_resolvers:
            result = r.try_delete(path, context=context)
            if result is not None:
                return True, result
        return False, None

    @property
    def resolver_count(self) -> int:
        return len(self._trie_resolvers) + len(self._fallback_resolvers)

    # ── register_intercept: per-operation INTERCEPT hooks ─────────────

    def register_intercept_read(self, hook: VFSReadHook) -> None:
        self._registry.register("read", hook)

    def register_intercept_write(self, hook: VFSWriteHook) -> None:
        self._registry.register("write", hook)

    def register_intercept_write_batch(self, hook: VFSWriteBatchHook) -> None:
        self._registry.register("write_batch", hook)

    def register_intercept_delete(self, hook: VFSDeleteHook) -> None:
        self._registry.register("delete", hook)

    def register_intercept_rename(self, hook: VFSRenameHook) -> None:
        self._registry.register("rename", hook)

    def register_intercept_mkdir(self, hook: VFSMkdirHook) -> None:
        self._registry.register("mkdir", hook)

    def register_intercept_rmdir(self, hook: VFSRmdirHook) -> None:
        self._registry.register("rmdir", hook)

    def register_intercept_stat(self, hook: VFSStatHook) -> None:
        self._registry.register("stat", hook)

    def register_intercept_access(self, hook: VFSAccessHook) -> None:
        self._registry.register("access", hook)

    # ── unregister ─────────────────────────────────────────────────────

    def unregister_resolver(self, resolver: "VFSPathResolver") -> bool:
        """Remove a PRE-DISPATCH resolver. Returns True if found."""
        for idx, r in list(self._trie_resolvers.items()):
            if r is resolver:
                if self._trie is not None:
                    self._trie.unregister(idx)
                del self._trie_resolvers[idx]
                return True
        try:
            self._fallback_resolvers.remove(resolver)
            return True
        except ValueError:
            return False

    def unregister_intercept_read(self, hook: VFSReadHook) -> bool:
        return bool(self._registry.unregister("read", hook))

    def unregister_intercept_write(self, hook: VFSWriteHook) -> bool:
        return bool(self._registry.unregister("write", hook))

    def unregister_intercept_write_batch(self, hook: VFSWriteBatchHook) -> bool:
        return bool(self._registry.unregister("write_batch", hook))

    def unregister_intercept_delete(self, hook: VFSDeleteHook) -> bool:
        return bool(self._registry.unregister("delete", hook))

    def unregister_intercept_rename(self, hook: VFSRenameHook) -> bool:
        return bool(self._registry.unregister("rename", hook))

    def unregister_intercept_mkdir(self, hook: VFSMkdirHook) -> bool:
        return bool(self._registry.unregister("mkdir", hook))

    def unregister_intercept_rmdir(self, hook: VFSRmdirHook) -> bool:
        return bool(self._registry.unregister("rmdir", hook))

    def unregister_intercept_stat(self, hook: VFSStatHook) -> bool:
        return bool(self._registry.unregister("stat", hook))

    def unregister_intercept_access(self, hook: VFSAccessHook) -> bool:
        return bool(self._registry.unregister("access", hook))

    # ── register_observe: generic OBSERVE observers (Issue #1748) ───────

    def register_observe(self, obs: VFSObserver) -> None:
        from nexus.core.file_events import ALL_FILE_EVENTS

        mask = getattr(obs, "event_mask", ALL_FILE_EVENTS)
        self._observer_registry.register(obs, mask)

    def has_hooks(self, op: str) -> bool:
        """O(1) check: any hooks registered for *op*? Avoids HookContext construction."""
        return op in self._registry._nonempty

    def unregister_observe(self, obs: VFSObserver) -> bool:
        return bool(self._observer_registry.unregister(obs))

    # ── PRE-INTERCEPT dispatch (Issue #899) ───────────────────────────
    # Uses HookRegistry.get_pre_hooks() — pre-filtered at registration.
    # Exceptions propagate (abort operation) — LSM semantics.

    def intercept_pre_read(self, ctx: ReadHookContext) -> None:
        """PRE-INTERCEPT phase for read — hooks may abort by raising."""
        if "read" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("read"):
            hook.on_pre_read(ctx)

    def intercept_pre_write(self, ctx: WriteHookContext) -> None:
        """PRE-INTERCEPT phase for write — hooks may abort by raising."""
        if "write" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("write"):
            hook.on_pre_write(ctx)

    def intercept_pre_delete(self, ctx: DeleteHookContext) -> None:
        """PRE-INTERCEPT phase for delete — hooks may abort by raising."""
        if "delete" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("delete"):
            hook.on_pre_delete(ctx)

    def intercept_pre_rename(self, ctx: RenameHookContext) -> None:
        """PRE-INTERCEPT phase for rename — hooks may abort by raising."""
        if "rename" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("rename"):
            hook.on_pre_rename(ctx)

    def intercept_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        """PRE-INTERCEPT phase for mkdir — hooks may abort by raising."""
        if "mkdir" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("mkdir"):
            hook.on_pre_mkdir(ctx)

    def intercept_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        """PRE-INTERCEPT phase for rmdir — hooks may abort by raising."""
        if "rmdir" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("rmdir"):
            hook.on_pre_rmdir(ctx)

    def intercept_pre_stat(self, ctx: StatHookContext) -> None:
        """PRE-INTERCEPT phase for stat — hooks may abort by raising."""
        if "stat" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("stat"):
            hook.on_pre_stat(ctx)

    def intercept_pre_access(self, ctx: AccessHookContext) -> None:
        """PRE-INTERCEPT phase for access — hooks may abort by raising."""
        if "access" not in self._registry._nonempty:
            return
        for hook in self._registry.get_pre_hooks("access"):
            hook.on_pre_access(ctx)

    # ── POST-INTERCEPT dispatch ────────────────────────────────────────

    async def _post_dispatch(self, op: str, method: str, ctx: Any, *, timeout: float = 5.0) -> None:
        """Shared POST dispatch — sync hooks serial, async hooks parallel.

        Sync hooks: direct call, fault-isolated (try/except per hook).
        Async hooks: ``asyncio.gather`` with per-hook timeout.
        Only ``AuditLogError`` aborts; other exceptions become warnings.
        """
        if op not in self._registry._nonempty:
            return
        sync_hooks, async_hooks = self._registry.get_post_hooks(op)

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
        if self._registry.count("write_batch") == 0:
            return
        ctx = WriteBatchHookContext(
            items=items, context=context, zone_id=zone_id, agent_id=agent_id
        )
        await self._post_dispatch("write_batch", "on_post_write_batch", ctx)

    async def intercept_post_delete(self, ctx: DeleteHookContext) -> None:
        await self._post_dispatch("delete", "on_post_delete", ctx)

    async def intercept_post_rename(self, ctx: RenameHookContext) -> None:
        await self._post_dispatch("rename", "on_post_rename", ctx)

    async def intercept_post_mkdir(self, ctx: MkdirHookContext) -> None:
        await self._post_dispatch("mkdir", "on_post_mkdir", ctx)

    async def intercept_post_rmdir(self, ctx: RmdirHookContext) -> None:
        await self._post_dispatch("rmdir", "on_post_rmdir", ctx)

    # ── OBSERVE dispatch (Issue #1812, #1748) ────────────────────────────

    async def notify(self, event: FileEvent) -> None:
        """OBSERVE phase — fire-and-forget to all registered observers.

        Rust ``ObserverRegistry`` filters by ``event_mask`` bitmask before
        crossing to Python.  Matching observers run concurrently via
        ``gather(return_exceptions=True)`` — fault-isolated, no ordering.
        """
        from nexus.core.file_events import FILE_EVENT_BIT, FileEventType

        event_type = event.type if isinstance(event.type, FileEventType) else None
        bit = FILE_EVENT_BIT.get(event_type, 0) if event_type else 0
        if not bit:
            return
        observers = self._observer_registry.get_matching(bit)
        if not observers:
            return

        async def _safe(obs: Any, name: str) -> None:
            try:
                await obs.on_mutation(event)
            except Exception as exc:
                logger.warning("Observer %s failed: %s", name, exc)

        await asyncio.gather(*(_safe(obs, name) for obs, name in observers))

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

    # ── Hook counts ────────────────────────────────────────────────────

    @property
    def read_hook_count(self) -> int:
        return int(self._registry.count("read"))

    @property
    def write_hook_count(self) -> int:
        return int(self._registry.count("write"))

    @property
    def write_batch_hook_count(self) -> int:
        return int(self._registry.count("write_batch"))

    @property
    def delete_hook_count(self) -> int:
        return int(self._registry.count("delete"))

    @property
    def rename_hook_count(self) -> int:
        return int(self._registry.count("rename"))

    @property
    def mkdir_hook_count(self) -> int:
        return int(self._registry.count("mkdir"))

    @property
    def rmdir_hook_count(self) -> int:
        return int(self._registry.count("rmdir"))

    @property
    def stat_hook_count(self) -> int:
        return int(self._registry.count("stat"))

    @property
    def access_hook_count(self) -> int:
        return int(self._registry.count("access"))

    @property
    def observer_count(self) -> int:
        return int(self._observer_registry.count())

    @property
    def mount_hook_count(self) -> int:
        return len(self._mount_hooks)

    @property
    def unmount_hook_count(self) -> int:
        return len(self._unmount_hooks)
