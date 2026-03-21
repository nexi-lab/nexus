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
    from nexus_fast import PathTrie as _PathTrie
except ImportError:  # pragma: no cover — Rust extension not built
    _PathTrie = None
    _HookRegistry = None

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


class _PythonHookRegistry:
    """Pure-Python fallback when ``nexus_fast`` is unavailable."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Any]] = defaultdict(list)

    def register(self, op: str, hook: Any) -> None:
        self._hooks[op].append(hook)

    def unregister(self, op: str, hook: Any) -> bool:
        hooks = self._hooks.get(op, [])
        try:
            hooks.remove(hook)
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
        dispatch.notify(event)               # phase 2: OBSERVE
    """

    __slots__ = (
        "_trie",
        "_trie_resolvers",
        "_fallback_resolvers",
        "_next_resolver_idx",
        "_registry",
        "_observers",
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

        # OBSERVE: generic mutation observers
        self._observers: list[VFSObserver] = []

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
        return_metadata: bool = False,
        context: Any = None,
    ) -> tuple[bool, Any]:
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
                    result = resolver.try_read(
                        path, return_metadata=return_metadata, context=context
                    )
                    if result is not None:
                        return True, result
        # Phase 2: fallback linear scan
        for r in self._fallback_resolvers:
            result = r.try_read(path, return_metadata=return_metadata, context=context)
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

    # ── register_observe: generic OBSERVE observers ────────────────────

    def register_observe(self, obs: VFSObserver) -> None:
        self._observers.append(obs)

    def unregister_observe(self, obs: VFSObserver) -> bool:
        try:
            self._observers.remove(obs)
            return True
        except ValueError:
            return False

    # ── PRE-INTERCEPT dispatch (Issue #899) ───────────────────────────
    # Uses HookRegistry.get_pre_hooks() — pre-filtered at registration.
    # Exceptions propagate (abort operation) — LSM semantics.

    def intercept_pre_read(self, ctx: ReadHookContext) -> None:
        """PRE-INTERCEPT phase for read — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("read"):
            hook.on_pre_read(ctx)

    def intercept_pre_write(self, ctx: WriteHookContext) -> None:
        """PRE-INTERCEPT phase for write — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("write"):
            hook.on_pre_write(ctx)

    def intercept_pre_delete(self, ctx: DeleteHookContext) -> None:
        """PRE-INTERCEPT phase for delete — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("delete"):
            hook.on_pre_delete(ctx)

    def intercept_pre_rename(self, ctx: RenameHookContext) -> None:
        """PRE-INTERCEPT phase for rename — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("rename"):
            hook.on_pre_rename(ctx)

    def intercept_pre_mkdir(self, ctx: MkdirHookContext) -> None:
        """PRE-INTERCEPT phase for mkdir — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("mkdir"):
            hook.on_pre_mkdir(ctx)

    def intercept_pre_rmdir(self, ctx: RmdirHookContext) -> None:
        """PRE-INTERCEPT phase for rmdir — hooks may abort by raising."""
        for hook in self._registry.get_pre_hooks("rmdir"):
            hook.on_pre_rmdir(ctx)

    # ── POST-INTERCEPT dispatch ────────────────────────────────────────

    async def _post_dispatch(self, op: str, method: str, ctx: Any, *, timeout: float = 5.0) -> None:
        """Shared POST dispatch — sync hooks serial, async hooks parallel.

        Sync hooks: direct call, fault-isolated (try/except per hook).
        Async hooks: ``asyncio.gather`` with per-hook timeout.
        Only ``AuditLogError`` aborts; other exceptions become warnings.
        """
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

    # ── OBSERVE dispatch ───────────────────────────────────────────────

    async def notify(self, event: FileEvent) -> None:
        """OBSERVE phase — fire-and-forget to all registered observers."""
        for obs in self._observers:
            try:
                obs.on_mutation(event)
            except Exception as exc:
                logger.warning("Observer %s failed: %s", type(obs).__name__, exc)

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
    def observer_count(self) -> int:
        return len(self._observers)
