"""Unified filesystem implementation for Nexus."""
# Kernel interface unification — see KERNEL-ARCHITECTURE.md §4.5

import builtins
import contextlib
import logging
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime
from typing import Any, NamedTuple

from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    BackendError,
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
)
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
from nexus.contracts.metadata import DT_DIR, FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)
from nexus.core.file_events import FileEvent, FileEventType
from nexus.core.hash_fast import hash_content
from nexus.core.metastore import MetastoreABC
from nexus.core.router import PathRouter
from nexus.lib.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

_SENTINEL = object()  # default for _meta param in _check_is_directory


class _WriteContentResult(NamedTuple):
    """Result of content-only write phase."""

    content_hash: str
    size: int
    metadata: "FileMetadata"  # Built metadata ready for metastore
    new_version: int
    is_new: bool  # True if file didn't exist before
    old_etag: str | None
    old_metadata: "FileMetadata | None"  # Pre-write metadata for post-write hooks
    context: "OperationContext"  # Augmented context (with backend_path/virtual_path)
    zone_id: str | None
    agent_id: str | None
    is_remote: bool  # Remote backend — skip local metadata.put
    is_external: bool  # ExternalRouteResult path


class NexusFS(  # type: ignore[misc]
    NexusFilesystemABC,
):
    """
    Unified filesystem for Nexus.

    Provides file operations (read, write, delete) with metadata tracking
    using content-addressable storage (CAS) for automatic deduplication.

    Works with any backend (local, GCS, S3, etc.) that implements the Backend interface.

    All backends use CAS by default for:
    - Automatic deduplication (same content stored once)
    - Content integrity (hash verification)
    - Efficient storage
    """

    def __init__(
        self,
        metadata_store: MetastoreABC,
        record_store: RecordStoreABC | None = None,
        cache_store: CacheStoreABC | None = None,
        *,
        cache: CacheConfig | None = None,
        permissions: PermissionConfig | None = None,
        distributed: DistributedConfig | None = None,
        memory: MemoryConfig | None = None,
        parsing: ParseConfig | None = None,
        router: Any = None,
        init_cred: OperationContext | None = None,
    ):
        """Initialize NexusFS kernel.

        Kernel boots with MetastoreABC (inode layer) and an optional router.
        Backends are mounted externally via ``router.add_mount()`` — like
        Linux VFS, no global backend.

        Args:
            router: PathRouter instance for VFS routing. When None, a default
                router is created from metadata_store.
            init_cred: Kernel process credential — like Linux ``init_task.cred``.
                Used as fallback identity for internal operations (audit pipe
                writes, service bootstrap mkdir). Immutable after construction.
                Pass ``None`` only for bare-kernel tests that never call syscalls.
        """
        # Config defaults
        cache = cache or CacheConfig()
        permissions = permissions or PermissionConfig()
        distributed = distributed or DistributedConfig()
        memory = memory or MemoryConfig()
        parsing = parsing or ParseConfig()

        # Per-instance VFS revision counter (H21: must not be class-level)
        import threading as _threading

        self._vfs_revision: int = 0
        self._vfs_revision_lock = _threading.Lock()

        self._cache_config = cache
        self._perm_config = permissions
        self._distributed_config = distributed
        self._memory_config_obj = memory
        self._parse_config = parsing
        self._config: Any | None = None

        # Map config fields to flat attributes
        self._enable_memory_paging = memory.enable_paging
        self._memory_main_capacity = memory.main_capacity
        self._memory_recall_max_age_hours = memory.recall_max_age_hours
        # _enforce_permissions removed — permission enforcement is fully delegated
        # to KernelDispatch INTERCEPT hooks. PermissionCheckHook holds the flag
        # internally. No hook = no check = zero overhead (~20ns set lookup).
        self._enforce_zone_isolation = permissions.enforce_zone_isolation
        self.allow_admin_bypass = permissions.allow_admin_bypass

        # Three pillars: metadata (required), record store, cache store
        # No self.backend — all I/O goes through router.route().backend
        self.metadata: MetastoreABC = metadata_store
        self._record_store = record_store
        self._sql_engine: Any = None
        self._db_session_factory: Any = None
        self.SessionLocal: Any = None
        if record_store is not None:
            self._sql_engine = record_store.engine
            self._db_session_factory = record_store.session_factory
            self.SessionLocal = self._db_session_factory

        # Initialize cache store (Task #22: Fourth Pillar)
        self.cache_store: CacheStoreABC = (
            cache_store if cache_store is not None else NullCacheStore()
        )

        # Path router (metastore-backed mount table)
        if router is not None:
            self.router = router
        else:
            self.router = PathRouter(metadata_store)

        # Issue #1801: kernel process credential — like Linux init_task.cred.
        # Immutable after construction. Used as fallback identity for internal
        # operations. External callers should pass explicit context= to syscalls.
        self._init_cred: OperationContext | None = init_cred

        # ── Kernel-owned primitives (always present, created here) ──────
        # See KERNEL-ARCHITECTURE.md §1 DI patterns table.

        from nexus.core.lock_fast import create_vfs_lock_manager

        self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        from nexus.lib.distributed_lock import LocalLockManager
        from nexus.lib.semaphore import create_vfs_semaphore

        self._lock_manager: Any = LocalLockManager(
            create_vfs_semaphore(),
            zone_id=ROOT_ZONE_ID,
            vfs_lock_manager=self._vfs_lock_manager,
        )

        from nexus.core.kernel_dispatch import KernelDispatch

        self._dispatch: KernelDispatch = KernelDispatch()

        import os as _os_ipc

        from nexus.core.pipe_manager import PipeManager
        from nexus.core.stream_manager import StreamManager

        _ipc_self_addr = _os_ipc.environ.get("NEXUS_ADVERTISE_ADDR")

        from nexus.grpc.channel_pool import PeerChannelPool as _PeerChannelPool

        self._channel_pool: _PeerChannelPool | None = None
        if _ipc_self_addr:
            self._channel_pool = _PeerChannelPool()

        self._pipe_manager = PipeManager(
            metadata_store,
            self_address=_ipc_self_addr,
            channel_pool=self._channel_pool,
        )
        self._stream_manager = StreamManager(
            metadata_store,
            self_address=_ipc_self_addr,
            channel_pool=self._channel_pool,
        )

        from nexus.core.file_watcher import FileWatcher

        self._file_watcher = FileWatcher()

        logger.info(
            "IPC primitives initialized: PipeManager + StreamManager + FileWatcher (self_address=%s)",
            _ipc_self_addr or "none/single-node",
        )

        from nexus.core.service_registry import ServiceRegistry

        self._service_registry: ServiceRegistry = ServiceRegistry(dispatch=self._dispatch)

        from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator

        self._driver_coordinator: DriverLifecycleCoordinator = DriverLifecycleCoordinator(
            self.router, self._dispatch, metastore=metadata_store
        )

        # ── Kernel-knows (sentinel None, injected by factory) ───────────
        # See KERNEL-ARCHITECTURE.md §1 DI patterns table.
        # None = graceful degrade (like Linux LSM: no module loaded = no check).

        self._event_bus: Any = None
        self._overlay_resolver = None
        self._token_manager = None
        self._sandbox_manager: Any = None
        self._coordination_client: Any = None
        self._event_client: Any = None

        # Wire metastore into pre-existing mounts (added to router before __init__)
        for _mp in self.router.get_mount_points():
            _mi = self.router.get_mount(_mp)
            if _mi is not None and hasattr(_mi.backend, "set_metastore"):
                _mi.backend.set_metastore(metadata_store)
            elif _mi is not None and hasattr(_mi.backend, "_metastore"):
                _mi.backend._metastore = metadata_store

        # Lifecycle state — set by link() / initialize() / bootstrap()
        self._linked: bool = False
        self._initialized: bool = False
        self._bootstrapped: bool = False
        self._close_callbacks: list[
            Callable[[], None]
        ] = []  # Issue #1793: factory-registered service close
        self._runtime_closeables: list[Any] = []

    # =====================================================================
    # Lifecycle methods: link() → initialize() → bootstrap()
    #
    # Linearized in PR #3371 Phase 2: create_nexus_fs() calls
    # _wire_services() / _initialize_services() directly and sets the
    # flags. These methods are now flag-only no-ops for backward compat.
    # =====================================================================

    async def link(
        self,
        *,
        enabled_bricks: "frozenset[str] | None" = None,
        parsing: Any = None,
        workflow_engine: Any = None,
    ) -> None:
        """Phase 1: Wire service topology — flag-only no-op.

        Actual wiring is done by factory._lifecycle._wire_services(),
        called directly from create_nexus_fs(). This method exists for
        backward compatibility (tests, manual construction).
        """
        if self._linked:
            return
        self._linked = True

    async def initialize(self) -> None:
        """Phase 2: One-time side effects — flag-only no-op.

        Actual initialization is done by factory._lifecycle._initialize_services(),
        called directly from create_nexus_fs(). This method exists for
        backward compatibility (tests, manual construction).
        """
        if self._initialized:
            return
        if not self._linked:
            await self.link()
        self._initialized = True

    async def bootstrap(self) -> None:
        """Phase 3: Start async tasks.  Server/Worker only.

        Auto-starts all PersistentService instances (ZoneLifecycleService,
        EventDeliveryWorker, DeferredPermissionBuffer, etc.) via
        ServiceRegistry.start_persistent_services().

        Idempotent — guarded by ``_bootstrapped`` flag.
        """
        if self._bootstrapped:
            return
        if not self._initialized:
            await self.initialize()
        # Auto-lifecycle: start PersistentService instances (Issue #1580)
        coord = self.service_coordinator
        if coord is not None:
            await coord.start_persistent_services()
            coord.mark_bootstrapped()  # future enlist() calls auto-start
        self._bootstrapped = True

    def _register_runtime_closeable(self, resource: Any) -> None:
        """Register a process-local resource to close with the filesystem.

        Used for runtime-owned handles that are not persisted in metadata
        and are not discoverable through the normal service graph, such as
        the REMOTE client's shared RPC transport.
        """
        self._runtime_closeables.append(resource)

    # -- Service registry accessors (Issue #1452) ---------------------------

    def service(self, name: str) -> Any | None:
        """Look up a registered service by canonical name.

        Returns the service instance, or ``None`` if not registered.
        """
        return self._service_registry.service(name)

    @property
    def service_registry(self) -> Any:
        """Read-only access to the kernel ServiceRegistry.  Factory / diagnostics."""
        return self._service_registry

    # Services accessed via self.service("name") → ServiceRegistry (Issue #1452).
    # Registered by factory via enlist_wired_services() at link().

    @property
    def service_coordinator(self) -> Any:
        """ServiceRegistry with integrated lifecycle (formerly ServiceLifecycleCoordinator)."""
        return self._service_registry

    async def swap_service(self, name: str, new_instance: Any, **kwargs: Any) -> None:
        """Hot-swap a service — all quadrants supported (#1452)."""
        await self._service_registry.swap_service(name, new_instance, **kwargs)

    def _upgrade_lock_manager(self, lock_manager: Any) -> None:
        """Hot-swap LocalLockManager → RaftLockManager at link time.

        Like FileWatcher.set_remote_watcher() — kernel owns the hook point,
        federation injects the distributed implementation.
        """
        logger.info(
            "Lock manager upgraded: %s → %s",
            type(self._lock_manager).__name__,
            type(lock_manager).__name__,
        )
        self._lock_manager = lock_manager

    @property
    def namespace_manager(self) -> Any | None:
        """Public accessor for the NamespaceManager (via ServiceRegistry)."""
        _pe = self.service("permission_enforcer")
        if _pe is not None:
            return getattr(_pe, "namespace_manager", None)
        return None

    @property
    def config(self) -> Any | None:
        """Public accessor for the runtime configuration object."""
        return self._config

    def _resolve_cred(self, context: OperationContext | None) -> OperationContext:
        """Return *context* or the kernel init_cred; raise if neither available.

        Issue #1801: kernel never fabricates identity — like Linux VFS,
        every syscall requires credentials from the caller.  Renamed from
        ``_require_context`` to reflect its role: resolve the credential
        chain (explicit → init_cred → error).
        """
        if context is not None:
            return context
        if self._init_cred is not None:
            return self._init_cred
        raise ValueError(
            "No operation context provided and no init_cred configured. "
            "Use factory create_nexus_fs(init_cred=...) or pass context= to each syscall."
        )

    def _get_context_identity(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context."""
        if context is None:
            ctx = self._resolve_cred(None)
            return (ctx.zone_id, ctx.agent_id, ctx.is_admin)
        if isinstance(context, dict):
            fallback = self._resolve_cred(None)
            return (
                context.get("zone_id", fallback.zone_id),
                context.get("agent_id", fallback.agent_id),
                context.get("is_admin", fallback.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", False)

    # Issue #1790: _check_zone_writable() deleted — now handled by
    def _build_write_metadata(
        self,
        *,
        path: str,
        backend_name: str,
        content_hash: str,
        size: int,
        existing_meta: "FileMetadata | None",
        now: datetime,
        zone_id: str | None,
        context: "OperationContext | None",
    ) -> "FileMetadata":
        """Build FileMetadata for a sys_write result.

        Shared by both the external-route and VFS-locked write paths
        to avoid duplicating version calculation, owner resolution,
        and field mapping.
        """
        new_version = (existing_meta.version + 1) if existing_meta else 1
        ctx = self._resolve_cred(context)
        owner_id = existing_meta.owner_id if existing_meta else (ctx.subject_id or ctx.user_id)
        _ttl = getattr(context, "ttl_seconds", None) or 0.0
        return FileMetadata(
            path=path,
            backend_name=backend_name,
            physical_path=content_hash,
            size=size,
            etag=content_hash,
            created_at=existing_meta.created_at if existing_meta else now,
            modified_at=now,
            version=new_version,
            zone_id=zone_id or "root",
            owner_id=owner_id,
            ttl_seconds=_ttl,
        )

    # ZoneWriteGuardHook (pre-intercept on all write-like operations).

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

    def _ensure_context_ttl(self, context: OperationContext | None, ttl: float) -> OperationContext:
        """Ensure context exists and has ttl_seconds set (Issue #3405)."""
        if context is not None:
            return _dc_replace(context, ttl_seconds=ttl)
        return OperationContext(user_id="anonymous", groups=[], ttl_seconds=ttl)

    def _validate_path(self, path: str, allow_root: bool = False) -> str:
        """Validate and normalize virtual path. Delegates to lib/path_utils."""
        return validate_path(path, allow_root=allow_root)

    def _get_parent_path(self, path: str) -> str | None:
        """Get parent directory path, or None if root."""
        if path == "/":
            return None

        # Remove trailing slash if present
        path = path.rstrip("/")

        # Find last slash
        last_slash = path.rfind("/")
        if last_slash == 0:
            # Parent is root
            return "/"
        elif last_slash > 0:
            return path[:last_slash]
        else:
            # No parent (shouldn't happen for valid paths)
            return None

    def _ensure_parent_directories(self, path: str, ctx: OperationContext) -> None:
        """Create metadata entries for all parent directories that don't exist.

        Walks from the immediate parent of *path* upward toward ``/``, collecting
        every path that has no metastore entry, then creates directory metadata
        entries from top to bottom (shallowest first) so that ``sys_readdir``
        lists them correctly.

        This is factored out of ``mkdir`` so it can be called both on the
        normal code-path *and* on the early-return path when the target path
        already exists (e.g. a DT_MOUNT entry written by ``PathRouter.add_mount``).
        """
        parent_path = self._get_parent_path(path)
        parents_to_create: list[str] = []

        while parent_path and parent_path != "/":
            if not self.metadata.exists(parent_path):
                parents_to_create.append(parent_path)
            else:
                break
            parent_path = self._get_parent_path(parent_path)

        for parent_dir in reversed(parents_to_create):
            self._setattr_create(
                parent_dir,
                DT_DIR,
                {
                    "zone_id": ctx.zone_id or ROOT_ZONE_ID,
                },
            )

    @rpc_expose(description="Remove directory")
    async def sys_rmdir(
        self,
        path: str,
        recursive: bool = False,
        *,
        context: OperationContext | None = None,
    ) -> None:
        """Tier 2: convenience wrapper — delegates to sys_unlink(recursive=).

        Preserves None return type for backward compat (sys_unlink returns dict).
        """
        await self.sys_unlink(path, recursive=recursive, context=context)

    async def _check_is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        _meta: Any = _SENTINEL,
    ) -> bool:
        """Internal: check if path is a directory (explicit or implicit).

        Used by sys_stat. is_directory is a Tier 2 wrapper over sys_stat.

        Args:
            _meta: Pre-fetched FileMetadata from caller (avoids duplicate
                metadata.get). Pass ``None`` to indicate "already looked up,
                not found". Omit to let this method fetch it.
        """
        try:
            path = self._validate_path(path)
            ctx = self._resolve_cred(context)

            # Check if it's an implicit directory first (for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Permission check via KernelDispatch INTERCEPT hook.
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._dispatch.intercept_pre_stat(
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    )
                )
            except PermissionDeniedError:
                return False

            # Use pre-fetched meta if provided, otherwise fetch
            meta = self.metadata.get(path) if _meta is _SENTINEL else _meta
            if meta is not None and (meta.is_dir or meta.is_mount):
                return True

            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                is_admin=ctx.is_admin,
                check_write=False,
            )
            if route.backend.is_directory(route.backend_path):
                return True
            return is_implicit_dir
        except (InvalidPathError, Exception):
            return False

    @rpc_expose(description="Check if path is a directory")
    async def is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bool:
        """Tier 2: convenience wrapper — derives from sys_stat.

        Equivalent to ``(await sys_stat(path)).get("is_directory", False)``.
        """
        try:
            stat = await self.sys_stat(path, context=context)
            return stat is not None and stat.get("is_directory", False)
        except Exception:
            return False

    # ── Locking (POSIX flock equivalent) ──────────────────────────

    @rpc_expose(description="Acquire advisory lock on a path")
    async def sys_lock(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: OperationContext | None = None,
    ) -> str | None:
        """Acquire advisory lock (POSIX flock(2)). Returns lock_id or None.

        Tier 1 syscall — single try-acquire, returns immediately.
        Use Tier 2 ``lock()`` for blocking wait with retry.
        """
        path = self._validate_path(path)
        return await self._lock_manager.acquire(
            path,
            mode=mode,
            ttl=ttl,
            max_holders=max_holders,
            timeout=0,  # try-once, no blocking
        )

    @rpc_expose(description="Release advisory lock")
    async def sys_unlock(
        self,
        path: str,
        lock_id: str,
        *,
        context: OperationContext | None = None,
    ) -> bool:
        """Release advisory lock. Returns True if released."""
        path = self._validate_path(path)
        return await self._lock_manager.release(lock_id, path)

    # ── Watch (inotify equivalent) ────────────────────────────────

    @rpc_expose(description="Wait for file changes on a path")
    async def sys_watch(
        self,
        path: str,
        timeout: float = 30.0,
        *,
        recursive: bool = False,  # noqa: ARG002
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Wait for file changes (inotify(7)). Returns FileEvent dict or None on timeout.

        Delegates to kernel FileWatcher which races local OBSERVE + optional
        remote watcher (federation) via FIRST_COMPLETED.
        """
        path = validate_path(path, allow_root=True)
        ctx = self._resolve_cred(context)
        zone_id = getattr(ctx, "zone_id", None) or ROOT_ZONE_ID
        event = await self._file_watcher.wait(path, timeout=timeout, zone_id=zone_id)
        if event is None:
            return None
        return event.to_dict()

    @rpc_expose(description="Get advisory lock info for a path")
    async def lock_info(
        self,
        path: str,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any] | None:
        """Get lock info for path (Tier 2 admin query)."""
        path = self._validate_path(path)
        info = await self._lock_manager.get_lock_info(path)
        if info is None:
            return None
        return {
            "path": info.path,
            "mode": info.mode,
            "max_holders": info.max_holders,
            "fence_token": info.fence_token,
            "holders": [
                {
                    "lock_id": h.lock_id,
                    "holder_info": h.holder_info,
                    "acquired_at": h.acquired_at,
                    "expires_at": h.expires_at,
                }
                for h in info.holders
            ],
        }

    @rpc_expose(description="List active advisory locks")
    async def lock_list(
        self,
        pattern: str = "",
        limit: int = 100,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """List active advisory locks (Tier 2 admin query)."""
        locks = await self._lock_manager.list_locks(pattern=pattern, limit=limit)
        return {
            "locks": [await self.lock_info(lk.path) for lk in locks],
            "count": len(locks),
        }

    @rpc_expose(description="Extend advisory lock TTL (heartbeat)")
    async def lock_extend(
        self,
        lock_id: str,
        path: str,
        ttl: float = 60.0,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Extend lock TTL / heartbeat (Tier 2)."""
        path = self._validate_path(path)
        result = await self._lock_manager.extend(lock_id, path, ttl=ttl)
        return {
            "success": result.success,
            "lock_info": (await self.lock_info(path)) if result.lock_info else None,
        }

    @rpc_expose(description="Force-release all holders of a lock (admin)")
    async def lock_force_release(
        self,
        path: str,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Force-release all holders (Tier 2 admin operation)."""
        path = self._validate_path(path)
        released = await self._lock_manager.force_release(path)
        return {"released": released}

    @rpc_expose(description="Release a lock (normal or force)")
    async def lock_release(
        self,
        path: str,
        lock_id: str | None = None,
        force: bool = False,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> dict[str, Any]:
        """Release a lock — dispatches to sys_unlock or lock_force_release.

        CLI-friendly: single method handles both normal and force release.
        """
        if force:
            return await self.lock_force_release(path)
        if not lock_id:
            raise ValueError("lock_id is required for non-force release")
        released = await self.sys_unlock(path, lock_id)
        return {"released": released}

    @rpc_expose(description="Get available namespaces")
    def get_top_level_mounts(self, context: OperationContext | None = None) -> builtins.list[str]:
        """Return top-level mount names visible to the current user.

        Reads DT_MOUNT entries from metastore (kernel's single source of
        truth for mount points). Admin-only filtering uses the runtime
        mount table which carries mount options.
        """
        ctx = self._resolve_cred(context)
        # Build admin_only set from runtime mount table (mount options)
        admin_only = {m.mount_point for m in self.router.list_mounts() if m.admin_only}

        names: set[str] = set()
        for meta in self.metadata.list("/"):
            if not meta.is_mount:
                continue
            top = meta.path.lstrip("/").split("/")[0]
            if not top:
                continue
            if meta.path in admin_only and not ctx.is_admin:
                continue
            names.add(top)
        return sorted(names)

    @rpc_expose(description="Get file metadata for FUSE operations")
    async def sys_stat(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr)."""
        ctx = self._resolve_cred(context)
        normalized = self._validate_path(path, allow_root=True)

        # Fetch metadata once, share with _check_is_directory to avoid duplicate lookup
        file_meta = self.metadata.get(normalized)

        # Check if it's a directory (pass pre-fetched meta to avoid second metadata.get)
        is_dir = await self._check_is_directory(normalized, context=ctx, _meta=file_meta)

        if is_dir:
            if file_meta is not None:
                # Explicit directory metadata exists — use it (preserves custom attrs from sys_setattr)
                return {
                    "path": file_meta.path,
                    "backend_name": file_meta.backend_name,
                    "physical_path": file_meta.physical_path,
                    "size": file_meta.size or 4096,
                    "etag": file_meta.etag,
                    "mime_type": file_meta.mime_type or "inode/directory",
                    "created_at": file_meta.created_at.isoformat()
                    if file_meta.created_at
                    else None,
                    "modified_at": file_meta.modified_at.isoformat()
                    if file_meta.modified_at
                    else None,
                    "is_directory": True,
                    "entry_type": file_meta.entry_type,
                    "owner": ctx.user_id,
                    "group": ctx.user_id,
                    "mode": 0o755,  # drwxr-xr-x
                    "version": file_meta.version,
                    "zone_id": file_meta.zone_id,
                }
            # Synthesize for implicit directories (no explicit metadata)
            return {
                "path": normalized,
                "backend_name": "",
                "physical_path": "",
                "size": 4096,  # Standard directory size
                "mime_type": "inode/directory",
                "created_at": None,
                "modified_at": None,
                "is_directory": True,
                "entry_type": 1,
                "owner": ctx.user_id,
                "group": ctx.user_id,
                "mode": 0o755,  # drwxr-xr-x
            }

        if file_meta is None:
            return None

        return {
            "path": file_meta.path,
            "backend_name": file_meta.backend_name,
            "physical_path": file_meta.physical_path,
            "size": file_meta.size or 0,
            "etag": file_meta.etag,
            "mime_type": file_meta.mime_type or "application/octet-stream",
            "created_at": file_meta.created_at.isoformat() if file_meta.created_at else None,
            "modified_at": file_meta.modified_at.isoformat() if file_meta.modified_at else None,
            "is_directory": False,
            "owner": ctx.user_id,
            "group": ctx.user_id,
            "mode": 0o644,  # -rw-r--r--
            "version": file_meta.version,
            "zone_id": file_meta.zone_id,
        }

    @rpc_expose(description="Upsert file metadata attributes")
    async def sys_setattr(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        """Upsert file metadata (chmod/chown/utimensat + mknod analog).

        Upsert semantics — create-on-write for metadata:
        - Path missing + entry_type provided → CREATE inode
        - Path missing + no entry_type → NexusFileNotFoundError
        - Path exists + no entry_type → UPDATE mutable fields
        - Path exists + same entry_type (DT_PIPE/DT_STREAM) → IDEMPOTENT OPEN (recover buffer)
        - Path exists + different entry_type → ValueError (immutable after creation)

        Args:
            path: Virtual file path. Paths under ``/__sys__/`` are kernel
            management operations (service/hook registration), not filesystem
            metadata.
            context: Operation context.
            **attrs: Metadata attributes. Include ``entry_type`` to create.

        Returns:
            Dict with path, created flag, and type-specific fields.
        """
        # ── /__sys__/ kernel management dispatch ──────────────────────
        # Service and hook registration via syscall. These paths bypass
        # the normal metastore path — kernel routes them to ServiceRegistry
        # or KernelDispatch instead.
        if path.startswith("/__sys__/services/"):
            name = path.rsplit("/", 1)[-1]
            service = attrs.get("service")
            if service is None:
                raise ValueError(
                    f"sys_setattr(/__sys__/services/{name}) requires 'service' attribute"
                )
            exports = attrs.get("exports", ())
            allow_overwrite = attrs.get("allow_overwrite", False)
            await self._service_registry.enlist(
                name, service, exports=exports, allow_overwrite=allow_overwrite
            )
            return {"path": path, "registered": True, "service": name}

        if path.startswith("/__sys__/hooks/"):
            # Standalone hook registration. Services with hook_spec use
            # /__sys__/services/ instead (enlist auto-detects hooks).
            # TODO: Add KernelDispatch.register_hook(name, hook) for
            # standalone hooks (debug tracers, temporary observers).
            raise NotImplementedError(
                "Standalone hook registration via /__sys__/hooks/ not yet supported. "
                "Use /__sys__/services/ with a service that declares hook_spec()."
            )

        path = self._validate_path(path)

        meta = self.metadata.get(path)

        # --- CREATE path (inode doesn't exist + entry_type provided) ---
        if meta is None:
            entry_type = attrs.get("entry_type")
            if entry_type is None:
                raise NexusFileNotFoundError(path)
            return self._setattr_create(path, entry_type, attrs)

        # --- IDEMPOTENT OPEN: same entry_type → recover buffer ---
        if "entry_type" in attrs:
            from nexus.contracts.metadata import DT_MOUNT, DT_PIPE, DT_STREAM

            requested_type = attrs["entry_type"]
            if meta.entry_type == requested_type and requested_type == DT_MOUNT:
                return {"path": path, "created": False, "entry_type": requested_type}
            if meta.entry_type == requested_type and requested_type == DT_PIPE:
                self._pipe_manager.open(path, capacity=attrs.get("capacity", 65_536))
                return {"path": path, "created": False, "entry_type": requested_type}
            if meta.entry_type == requested_type and requested_type == DT_STREAM:
                self._stream_manager.open(path, capacity=attrs.get("capacity", 65_536))
                return {"path": path, "created": False, "entry_type": requested_type}
            if meta.entry_type == requested_type and requested_type == DT_DIR:
                return {"path": path, "created": False, "entry_type": requested_type}
            raise ValueError(
                f"entry_type is immutable (cannot change {meta.entry_type} → {requested_type})"
            )

        # --- UPDATE path (existing inode, mutable fields only) ---
        from dataclasses import replace

        _MUTABLE_FIELDS = frozenset({"mime_type", "modified_at"})
        valid_attrs = {k: v for k, v in attrs.items() if k in _MUTABLE_FIELDS}
        invalid_attrs = {k for k in attrs if k not in _MUTABLE_FIELDS and k != "entry_type"}
        if invalid_attrs and not valid_attrs:
            raise ValueError(f"Cannot update immutable fields: {invalid_attrs}")
        if not valid_attrs:
            return {"path": path, "created": False, "updated": []}

        new_meta = replace(meta, **valid_attrs)
        self.metadata.put(new_meta)
        return {"path": path, "created": False, "updated": list(valid_attrs.keys())}

    def _setattr_create(self, path: str, entry_type: int, attrs: dict[str, Any]) -> dict[str, Any]:
        """Create an inode via sys_setattr upsert — dispatches by entry_type."""
        from nexus.contracts.metadata import DT_MOUNT, DT_PIPE, DT_STREAM

        capacity = attrs.get("capacity", 65_536)
        owner_id = attrs.get("owner_id")

        if entry_type == DT_MOUNT:
            # Mount a backend to this path via DriverLifecycleCoordinator.
            # Accepts a pre-constructed backend instance (kernel module API)
            # or backend_type + config for service-level construction (future).
            backend = attrs.get("backend")
            if backend is None:
                raise ValueError(
                    "sys_setattr(entry_type=DT_MOUNT) requires 'backend' attribute "
                    "(pre-constructed ObjectStoreABC instance)"
                )
            readonly = attrs.get("readonly", False)
            admin_only = attrs.get("admin_only", False)
            io_profile = attrs.get("io_profile", "balanced")
            zone_id = attrs.get("zone_id", ROOT_ZONE_ID)
            target_zone_id = attrs.get("target_zone_id")

            self._driver_coordinator.mount(
                path,
                backend,
                readonly=readonly,
                admin_only=admin_only,
                io_profile=io_profile,
            )

            # Write DT_MOUNT metadata to metastore
            now = datetime.now(UTC)
            metadata = FileMetadata(
                path=path,
                backend_name=backend.name,
                physical_path="",
                size=0,
                entry_type=DT_MOUNT,
                mime_type="inode/mount",
                created_at=now,
                modified_at=now,
                version=1,
                zone_id=zone_id,
                target_zone_id=target_zone_id,
            )
            self.metadata.put(metadata)
            return {
                "path": path,
                "created": True,
                "entry_type": entry_type,
                "backend": backend.name,
            }

        if entry_type == DT_PIPE:
            from nexus.core.pipe import PipeError

            try:
                self._pipe_manager.create(path, capacity=capacity, owner_id=owner_id)
            except PipeError as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

        if entry_type == DT_STREAM:
            from nexus.core.stream import StreamError

            # Check if mount provides a custom stream backend factory
            # (e.g. CAS-backed or WAL-backed streams). Default: in-memory StreamBuffer.
            _mount_entry = self.router.get_mount_entry_for_path(path)
            _factory = _mount_entry.stream_backend_factory if _mount_entry else None

            try:
                if _factory is not None:
                    backend = _factory(path, capacity)
                    self._stream_manager.create_from_backend(path, backend, owner_id=owner_id)
                else:
                    self._stream_manager.create(path, capacity=capacity, owner_id=owner_id)
            except StreamError as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

        if entry_type == DT_DIR:
            now = datetime.now(UTC)
            empty_hash = hash_content(b"")
            route = self.router.route(path, is_admin=True)
            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,
                physical_path=empty_hash,
                size=0,
                etag=empty_hash,
                entry_type=DT_DIR,
                mime_type="inode/directory",
                created_at=now,
                modified_at=now,
                version=1,
                zone_id=attrs.get("zone_id", ROOT_ZONE_ID),
            )
            self.metadata.put(metadata)
            return {"path": path, "created": True, "entry_type": entry_type}

        raise ValueError(f"sys_setattr create not supported for entry_type={entry_type}")

    @rpc_expose(description="Get ETag (content hash) for HTTP caching")
    def get_etag(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> str | None:
        """Get content hash for HTTP If-None-Match checks."""
        _ = context  # Reserved for future use
        normalized = self._validate_path(path, allow_root=False)

        # Get file metadata (lightweight - doesn't read content)
        file_meta = self.metadata.get(normalized)
        if file_meta is None:
            return None

        # Return the etag (content_hash) from metadata
        return file_meta.etag

    def _get_backend_directory_entries(
        self, path: str, context: OperationContext | None = None
    ) -> set[str]:
        """Get directory entries from backend for empty directory detection."""
        directories = set()

        try:
            # For root path, try routing "/" to find the root mount's backend
            if not path or path == "/":
                try:
                    zone_id, _agent_id, is_admin = self._get_context_identity(context)
                    root_route = self.router.route("/", is_admin=is_admin, check_write=False)
                    entries = root_route.backend.list_dir(root_route.backend_path)
                    for entry in entries:
                        if entry.endswith("/"):  # Directory marker
                            dir_name = entry.rstrip("/")
                            dir_path = "/" + dir_name
                            directories.add(dir_path)
                except (NotImplementedError, Exception):
                    # No root mount, backend doesn't support list_dir, or other error
                    pass
            else:
                # Non-root path - use router with context
                zone_id, _agent_id, is_admin = self._get_context_identity(context)
                route = self.router.route(
                    path.rstrip("/"),
                    is_admin=is_admin,
                    check_write=False,
                )
                backend_path = route.backend_path

                try:
                    entries = route.backend.list_dir(backend_path)
                    for entry in entries:
                        if entry.endswith("/"):  # Directory marker
                            dir_name = entry.rstrip("/")
                            dir_path = path + dir_name if path != "/" else "/" + dir_name
                            directories.add(dir_path)
                except NotImplementedError:
                    # Backend doesn't support list_dir - skip
                    pass
                except (OSError, PermissionError, TypeError):
                    # I/O, permission, or type errors - skip silently (best-effort directory listing)
                    pass

        except (ValueError, AttributeError, KeyError):
            # Ignore routing errors - directory detection is best-effort
            pass

        return directories

    # =================================================================
    # Core VFS File Operations (Issue #899)
    # =================================================================

    def _get_overlay_config(self, path: str) -> Any:
        """Get overlay config for a path, if overlay is active.

        Issue #1801: Reads workspace_registry from service registry.

        Returns:
            OverlayConfig if overlay active for this path, None otherwise
        """
        ws_reg = self.service("workspace_registry")
        if ws_reg is None:
            return None
        ws_config = ws_reg.find_workspace_for_path(path)
        if ws_config is None:
            return None
        overlay_data = ws_config.metadata.get("overlay_config")
        if overlay_data is None:
            return None
        from nexus.contracts.overlay_config import OverlayConfig

        return OverlayConfig(
            enabled=overlay_data.get("enabled", False),
            base_manifest_hash=overlay_data.get("base_manifest_hash"),
            workspace_path=ws_config.path,
            agent_id=overlay_data.get("agent_id"),
        )

    # =========================================================================
    # VFS I/O Lock — kernel-internal path-level read/write protection
    # =========================================================================

    _VFS_LOCK_TIMEOUT_MS = 5000  # 5s — generous for kernel I/O serialization

    def _vfs_acquire(self, path: str, mode: str) -> int:
        """Acquire VFS I/O lock, raising LockTimeout on failure.

        Args:
            path: Virtual path to lock.
            mode: "read" (shared) or "write" (exclusive).

        Returns:
            Lock handle (positive int) for release.

        Raises:
            LockTimeout: If lock cannot be acquired within timeout.
        """
        from nexus.lib.lock_order import L1_VFS, assert_can_acquire, mark_acquired

        assert_can_acquire(L1_VFS)
        handle = self._vfs_lock_manager.acquire(path, mode, timeout_ms=self._VFS_LOCK_TIMEOUT_MS)
        if handle == 0:
            from nexus.contracts.exceptions import LockTimeout

            raise LockTimeout(
                path=path,
                timeout=self._VFS_LOCK_TIMEOUT_MS / 1000,
                message=f"VFS {mode} lock timeout on {path}",
            )
        mark_acquired(L1_VFS)
        return handle

    @contextlib.contextmanager
    def _vfs_locked(self, path: str, mode: str) -> Generator[int, None, None]:
        """Context manager for VFS I/O lock — symmetric acquire/release.

        Usage::

            with self._vfs_locked(path, "write"):
                backend.write_content(...)
                metadata.put(...)
            # Event emission AFTER lock release (like Linux inotify after i_rwsem)
        """
        handle = self._vfs_acquire(path, mode)
        try:
            yield handle
        finally:
            self._vfs_lock_manager.release(handle)
            from nexus.lib.lock_order import L1_VFS, mark_released

            mark_released(L1_VFS)

    # ── Distributed lock helpers (sync bridge for write(lock=True)) ──

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: OperationContext | None,
    ) -> str | None:
        """Acquire advisory lock synchronously via kernel _lock_manager.

        This method bridges sync write() with async lock operations.
        For async contexts, use `async with nx.locked()` instead.
        """
        import asyncio

        from nexus.contracts.exceptions import LockTimeout

        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "write(lock=True) cannot be used from async context (event loop detected). "
                "Use `async with nx.locked(path):` and `write(lock=False)` instead."
            )
        except RuntimeError as e:
            if "event loop detected" in str(e):
                raise

        async def acquire_lock() -> str | None:
            return await self._lock_manager.acquire(path=path, timeout=timeout)

        from nexus.lib.sync_bridge import run_sync

        lock_id = run_sync(acquire_lock())

        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)

        return lock_id

    def _release_lock_sync(
        self,
        lock_id: str,
        path: str,
        context: OperationContext | None,
    ) -> None:
        """Release advisory lock synchronously via kernel _lock_manager."""
        if not lock_id:
            return

        async def release_lock() -> None:
            await self._lock_manager.release(lock_id, path)

        from nexus.lib.sync_bridge import run_sync

        try:
            run_sync(release_lock())
        except Exception as e:
            logger.error(f"Failed to release lock {lock_id} for {path}: {e}")

    async def _resolve_and_read(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> tuple[bytes, Any, Any, str | None, str | None]:
        """Core read pipeline: validate, resolve, route, read, post-hooks.

        Returns (content, meta, route, zone_id, agent_id).
        Extracted from sys_read() so that read_range() can share the logic
        without duplicating virtual-path dispatch, overlay resolution,
        hook invocation, and dynamic connector bypass.
        """
        # DT_PIPE fast-path: skip validate/resolve/intercept/route
        if self._pipe_manager is not None and path in self._pipe_manager._buffers:
            content = self._pipe_manager._get_buffer(path).read_nowait()
            return (content, None, None, None, None)
        # DT_STREAM fast-path: same rationale — StreamManager._buffers is authoritative.
        if path in self._stream_manager._buffers:
            content, _ = self._stream_manager.stream_read_at(path, 0)
            return (content, None, None, None, None)

        path = self._validate_path(path)
        context = self._parse_context(context)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _resolve_hint = self._dispatch.resolve_read(path, context=context)
        if _handled:
            return (_resolve_hint or b"", None, None, None, None)

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        perm_check_start = time.time()
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))
        perm_check_elapsed = time.time() - perm_check_start

        if perm_check_elapsed > 0.010:
            logger.warning(
                "[READ-PERF] SLOW pre-intercept for %s: %.1fms",
                path,
                perm_check_elapsed * 1000,
            )

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(path, is_admin=is_admin, check_write=False)

        # DT_PIPE / DT_STREAM bypass (sync — range reads not applicable)
        from nexus.core.router import ExternalRouteResult, PipeRouteResult, StreamRouteResult

        if isinstance(route, PipeRouteResult | StreamRouteResult):
            # Range reads not applicable; use sync non-blocking path
            if isinstance(route, PipeRouteResult):
                content = self._pipe_manager._get_buffer(path).read_nowait()
            else:
                content, _ = self._stream_manager.stream_read_at(path, 0)
            return (content, None, route, zone_id, agent_id)

        from dataclasses import replace

        if context:
            read_context = replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            read_context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )

        # DT_EXTERNAL_STORAGE: backend manages own content — skip metastore lookup
        if isinstance(route, ExternalRouteResult):
            content = route.backend.read_content("", context=read_context)
            return (content, None, route, zone_id, agent_id)

        # VFS I/O Lock
        with self._vfs_locked(path, "read"):
            meta = _resolve_hint if _resolve_hint is not None else self.metadata.get(path)

            if (meta is None or meta.etag is None) and getattr(self, "_overlay_resolver", None):
                overlay_config = self._get_overlay_config(path)
                if overlay_config:
                    meta = self._overlay_resolver.resolve_read(path, overlay_config)

            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(path)

            if getattr(self, "_overlay_resolver", None) and self._overlay_resolver.is_whiteout(
                meta
            ):
                raise NexusFileNotFoundError(path)

            content = route.backend.read_content(meta.etag, context=read_context)

        # Post-read hooks
        if self._dispatch.read_hook_count > 0:
            from nexus.contracts.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=meta,
                content=content,
                content_hash=meta.etag,
            )
            await self._dispatch.intercept_post_read(_read_ctx)
            content = _read_ctx.content or content

        return (content, meta, route, zone_id, agent_id)

    @rpc_expose(description="Read file content")
    async def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread(2)).

        Kernel primitive — always returns raw bytes. For metadata or parsed
        content, use the convenience ``read()`` method.

        Args:
            path: Virtual path to read (supports memory virtual paths).
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Optional operation context for permission checks.

        Returns:
            File content as bytes.

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied based on zone isolation
            PermissionError: If user doesn't have read permission
        """
        # DT_PIPE fast-path: skip validate/resolve/intercept/route (~400ns vs ~20+μs)
        # Hot path: try sync read_nowait (no Lock, no await) — matches sys_write perf.
        # Cold path (empty pipe): fall through to async _pipe_read for blocking wait.
        _pbuf = self._pipe_manager._buffers.get(path) if self._pipe_manager is not None else None
        if _pbuf is not None:
            from nexus.core.pipe import PipeClosedError, PipeEmptyError

            try:
                data = _pbuf.read_nowait()
            except PipeEmptyError:
                return await self._pipe_read(path, count=count, offset=offset)
            except PipeClosedError:
                raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
            if offset or count is not None:
                data = data[offset : offset + count] if count is not None else data[offset:]
            return data
        # DT_STREAM fast-path: same rationale — StreamManager._buffers is authoritative.
        # Fully inlined: single dict.get → buffer.read_at (Rust). No wrapper calls.
        _sbuf = self._stream_manager._buffers.get(path)
        if _sbuf is not None:
            from nexus.core.stream import StreamClosedError, StreamEmptyError

            try:
                if count is not None and count > 1:
                    items, _ = await _sbuf.read_batch_blocking(offset, count, blocking=True)
                    return b"".join(items)
                data, _ = await _sbuf.read(offset, blocking=True)
                return data
            except StreamEmptyError:
                # Blocking read handles this internally; only non-blocking raises
                raise NexusFileNotFoundError(path, f"Stream empty at offset {offset}") from None
            except StreamClosedError:
                raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

        path = self._validate_path(path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _resolve_hint = self._dispatch.resolve_read(path, context=context)
        if _handled:
            content = _resolve_hint or b""
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        perm_check_start = time.time()
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))
        perm_check_elapsed = time.time() - perm_check_start

        # Log slow pre-intercept
        if perm_check_elapsed > 0.010:  # >10ms
            logger.warning(
                f"[READ-PERF] SLOW pre-intercept for {path}: {perm_check_elapsed * 1000:.1f}ms"
            )

        # Normal file path - proceed with regular read
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        # DT_PIPE / DT_STREAM: kernel-native IPC dispatch (§4.2)
        from nexus.core.router import ExternalRouteResult, PipeRouteResult, StreamRouteResult

        if isinstance(route, PipeRouteResult):
            return await self._pipe_read(path, count=count, offset=offset)
        if isinstance(route, StreamRouteResult):
            return await self._stream_read(path, count=count, offset=offset)

        # Add backend_path to context for path-based connectors
        from dataclasses import replace

        if context:
            read_context = replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            # Create minimal context with just backend_path for connectors
            from nexus.contracts.types import OperationContext

            read_context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )

        # DT_EXTERNAL_STORAGE: backend manages own content — skip metastore lookup
        if isinstance(route, ExternalRouteResult):
            content = route.backend.read_content("", context=read_context)
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        # VFS I/O Lock: shared read lock around metadata check + backend read.
        # Prevents reading while a concurrent writer mutates the same path.
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        with self._vfs_locked(path, "read"):
            # Check if file exists in metadata (for regular backends)
            # _resolve_hint may carry prefetched metadata from a resolver
            meta = _resolve_hint if _resolve_hint is not None else self.metadata.get(path)

            # Issue #1264: Overlay resolution — check base layer if upper layer has no entry
            if (meta is None or meta.etag is None) and getattr(self, "_overlay_resolver", None):
                overlay_config = self._get_overlay_config(path)
                if overlay_config:
                    meta = self._overlay_resolver.resolve_read(path, overlay_config)

            if meta is None:
                raise NexusFileNotFoundError(path)

            # Issue #3194: Path-based backends (path_local, path_gcs, path_s3)
            # store metadata without content_hash/etag. Reads go through
            # backend_path in the OperationContext, not CAS.
            if meta.etag is None and not (read_context and read_context.backend_path):
                raise NexusFileNotFoundError(path)

            # Issue #1264: Reject whiteout markers (file was deleted in overlay)
            if getattr(self, "_overlay_resolver", None) and self._overlay_resolver.is_whiteout(
                meta
            ):
                raise NexusFileNotFoundError(path)

            content = route.backend.read_content(meta.etag or "", context=read_context)

        # --- Lock released — post-read processing (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified INTERCEPT for read (dynamic viewer, tracking, etc.)
        if self._dispatch.read_hook_count > 0:
            from nexus.contracts.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=meta,
                content=content,
                content_hash=meta.etag,
            )
            await self._dispatch.intercept_post_read(_read_ctx)
            content = _read_ctx.content or content  # hooks may have filtered content

        # --- Agent lineage: record read into session accumulator (Issue #3417) ---
        # Non-blocking, in-memory only. Records path + version + etag so the
        # lineage hook can attribute this read to the agent's next write.
        # Gates:
        #   1. Registered agent only (subject_type="agent", via agent API key)
        #   2. Explicit scope must be active (agent called POST /scope/begin)
        #      No default capture — if no scope is active, reads are not tracked.
        _is_registered_agent = (
            agent_id and context is not None and getattr(context, "subject_type", "user") == "agent"
        )
        if _is_registered_agent and meta is not None:
            try:
                from nexus.storage.session_read_accumulator import DEFAULT_SCOPE, get_accumulator

                _acc = get_accumulator()
                _gen = getattr(context, "agent_generation", None) if context else None
                # Only record if agent has an explicit scope active (not default)
                if _acc.get_active_scope(agent_id, _gen) != DEFAULT_SCOPE:
                    _acc.record_read(
                        agent_id,
                        _gen,
                        path,
                        version=getattr(meta, "version", 0) or 0,
                        etag=getattr(meta, "etag", "") or "",
                        access_type="content",
                    )
            except Exception:
                logger.debug("Lineage read tracking failed (non-critical)", exc_info=True)

        # Apply count/offset slicing (POSIX pread semantics)
        if offset or count is not None:
            content = content[offset : offset + count] if count is not None else content[offset:]

        return content

    @rpc_expose(description="Read multiple files in a single RPC call")
    def read_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        return_metadata: bool = False,
        skip_errors: bool = True,
    ) -> dict[str, bytes | dict[str, Any] | None]:
        """
        Read multiple files in a single RPC call for improved performance.

        This method is optimized for bulk operations like grep, where many files
        need to be read. It batches permission checks and reduces RPC overhead.

        Args:
            paths: List of virtual paths to read
            context: Optional operation context for permission checks
            return_metadata: If True, return dicts with content and metadata
            skip_errors: If True, skip files that can't be read and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> content (or None if skip_errors=True and read failed)
            If return_metadata=False: {path: bytes}
            If return_metadata=True: {path: {content, etag, version, ...}}

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Reduced network round trips
            - Expected speedup: 2-5x for 50+ files

        Examples:
            >>> # Read multiple files at once
            >>> results = nx.read_bulk(["/file1.txt", "/file2.txt", "/file3.txt"])
            >>> print(results["/file1.txt"])  # b'content'
            >>> print(results["/file2.txt"])  # b'content' or None if failed

            >>> # With metadata
            >>> results = nx.read_bulk(["/file1.txt"], return_metadata=True)
            >>> print(results["/file1.txt"]["content"])
            >>> print(results["/file1.txt"]["etag"])
        """
        import time

        bulk_start = time.time()
        results: dict[str, bytes | dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in read_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check via KernelDispatch INTERCEPT hook.
        # No hook = no check = all paths allowed.
        perm_start = time.time()
        allowed_set: set[str]
        try:
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.types import OperationContext
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            ctx = self._resolve_cred(context)
            assert isinstance(ctx, OperationContext), "Context must be OperationContext"
            allowed: list[str] = []
            for p in validated_paths:
                try:
                    self._dispatch.intercept_pre_stat(_SHC(path=p, context=ctx, permission="READ"))
                    allowed.append(p)
                except PermissionDeniedError:
                    pass
            allowed_set = set(allowed)
        except Exception as e:
            logger.error("[READ-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
                # If skip_errors, assume no files are allowed
                allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[READ-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Read allowed files
        read_start = time.time()
        zone_id, agent_id, is_admin = self._get_context_identity(context)

        # Group paths by backend for potential bulk optimization
        # Use get_batch for metadata lookup (single query instead of N queries)
        path_info: dict[str, tuple[FileMetadata, Any]] = {}  # path -> (meta, route)
        backend_paths: dict[Any, list[str]] = {}  # backend -> [paths]

        # Batch metadata lookup
        meta_start = time.time()
        batch_meta = self.metadata.get_batch(list(allowed_set))
        meta_elapsed = (time.time() - meta_start) * 1000
        logger.info(
            f"[READ-BULK] Batch metadata lookup: {len(batch_meta)} paths in {meta_elapsed:.1f}ms"
        )

        # Process metadata and group by backend
        route_start = time.time()
        for path in allowed_set:
            try:
                meta = batch_meta.get(path)
                if meta is None or meta.etag is None:
                    if skip_errors:
                        results[path] = None
                        continue
                    raise NexusFileNotFoundError(path)

                route = self.router.route(
                    path,
                    is_admin=is_admin,
                    check_write=False,
                )
                path_info[path] = (meta, route)

                # Group by backend
                backend = route.backend
                if backend not in backend_paths:
                    backend_paths[backend] = []
                backend_paths[backend].append(path)
            except Exception as e:
                logger.warning("[READ-BULK] Failed to route %s: %s: %s", path, type(e).__name__, e)
                if skip_errors:
                    results[path] = None
                else:
                    raise

        route_elapsed = (time.time() - route_start) * 1000
        logger.info("[READ-BULK] Routing: %d paths in %.1fms", len(path_info), route_elapsed)

        # Try bulk read for backends that support it (CacheConnectorMixin)
        for backend, paths_for_backend in backend_paths.items():
            if hasattr(backend, "read_bulk_from_cache") and len(paths_for_backend) > 1:
                # Use bulk cache lookup
                logger.info(
                    f"[READ-BULK] Using bulk cache for {len(paths_for_backend)} files on {type(backend).__name__}"
                )
                try:
                    cache_entries = backend.read_bulk_from_cache(paths_for_backend, original=True)

                    # Process cache hits
                    paths_needing_backend: list[str] = []
                    for path in paths_for_backend:
                        entry = cache_entries.get(path)
                        if entry and not entry.stale and entry.content_binary:
                            content = entry.content_binary
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        else:
                            paths_needing_backend.append(path)

                    # Fall back to individual reads for cache misses
                    for path in paths_needing_backend:
                        try:
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            read_context = context
                            if context:
                                from dataclasses import replace

                                read_context = replace(context, backend_path=route.backend_path)
                            content = route.backend.read_content(meta.etag, context=read_context)
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        except Exception as e:
                            logger.warning(
                                f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}"
                            )
                            if skip_errors:
                                results[path] = None
                            else:
                                raise
                except Exception as e:
                    logger.warning(
                        f"[READ-BULK] Bulk cache failed, falling back to individual reads: {e}"
                    )
                    # Fall back to individual reads
                    for path in paths_for_backend:
                        try:
                            meta, route = path_info[path]
                            assert meta.etag is not None  # Guaranteed by check above
                            read_context = context
                            if context:
                                from dataclasses import replace

                                read_context = replace(context, backend_path=route.backend_path)
                            content = route.backend.read_content(meta.etag, context=read_context)
                            if return_metadata:
                                results[path] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[path] = content
                        except Exception as e:
                            logger.warning(
                                f"[READ-BULK] Failed to read {path}: {type(e).__name__}: {e}"
                            )
                            if skip_errors:
                                results[path] = None
                            else:
                                raise
            else:
                # Batch read via ObjectStoreABC.batch_read_content() —
                # CASLocalBackend overrides with Rust parallel mmap;
                # other backends use default sequential fallback.
                content_ids = []
                id_to_path: dict[str, tuple[str, Any]] = {}
                for path in paths_for_backend:
                    meta, route = path_info[path]
                    assert meta.etag is not None
                    content_ids.append(meta.etag)
                    id_to_path[meta.etag] = (path, meta)

                bulk = backend.batch_read_content(content_ids, context=context)

                for cid, content in bulk.items():
                    vpath, meta = id_to_path[cid]
                    if content is None:
                        if skip_errors:
                            results[vpath] = None
                        else:
                            raise NexusFileNotFoundError(vpath)
                    elif return_metadata:
                        results[vpath] = {
                            "content": content,
                            "etag": meta.etag,
                            "version": meta.version,
                            "modified_at": meta.modified_at,
                            "size": len(content),
                        }
                    else:
                        results[vpath] = content

                # Handle any missing ids not in bulk result
                for path in paths_for_backend:
                    if path not in results:
                        if skip_errors:
                            results[path] = None
                        else:
                            raise NexusFileNotFoundError(path)

        read_elapsed = time.time() - read_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[READ-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, read={read_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Read a byte range from a file")
    async def read_range(
        self,
        path: str,
        start: int,
        end: int,
        context: OperationContext | None = None,
    ) -> bytes:
        """
        Read a specific byte range from a file.

        This method enables memory-efficient streaming by allowing clients to
        fetch file content in chunks without loading the entire file into memory.

        Args:
            path: Virtual path to read
            start: Start byte offset (inclusive, 0-indexed)
            end: End byte offset (exclusive)
            context: Optional operation context for permission checks

        Returns:
            bytes: Content from start to end (exclusive)

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If read operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission
            ValueError: If start/end are invalid (negative, start > end, etc.)

        Example:
            >>> # Read first 1MB of a large file
            >>> chunk = nx.read_range("/workspace/large.bin", 0, 1024 * 1024)

            >>> # Stream a file in chunks
            >>> offset = 0
            >>> chunk_size = 65536
            >>> while True:
            ...     chunk = nx.read_range("/workspace/large.bin", offset, offset + chunk_size)
            ...     if not chunk:
            ...         break
            ...     process(chunk)
            ...     offset += len(chunk)
        """
        # Validate range parameters
        if start < 0:
            raise ValueError(f"start must be non-negative, got {start}")
        if end < start:
            raise ValueError(f"end ({end}) must be >= start ({start})")

        path = self._validate_path(path)
        context = self._parse_context(context)

        # FAST PATH: check virtual path resolvers first
        _handled, _resolve_hint = self._dispatch.resolve_read(path, context=context)
        if _handled:
            return (_resolve_hint or b"")[start:end]

        # OPTIMISED PATH: no post-read hooks + backend has read_content_range
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        has_post_hooks = self._dispatch.read_hook_count > 0

        if not has_post_hooks:
            self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

            zone_id, agent_id, is_admin = self._get_context_identity(context)
            route = self.router.route(path, is_admin=is_admin, check_write=False)

            meta = self.metadata.get(path)

            if (meta is None or meta.etag is None) and getattr(self, "_overlay_resolver", None):
                overlay_config = self._get_overlay_config(path)
                if overlay_config:
                    meta = self._overlay_resolver.resolve_read(path, overlay_config)

            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(path)

            if getattr(self, "_overlay_resolver", None) and self._overlay_resolver.is_whiteout(
                meta
            ):
                raise NexusFileNotFoundError(path)

            if hasattr(route.backend, "read_content_range"):
                from dataclasses import replace as _replace

                read_context = (
                    _replace(context, backend_path=route.backend_path) if context else None
                )
                return route.backend.read_content_range(meta.etag, start, end, context=read_context)

        # FALLBACK: full read via _resolve_and_read + slice
        content, _meta, _route, _zone_id, _agent_id = await self._resolve_and_read(path, context)
        return content[start:end]

    @rpc_expose(description="Stream file content in chunks")
    def stream(
        self, path: str, chunk_size: int = 65536, context: OperationContext | None = None
    ) -> Any:
        """
        Stream file content in chunks without loading entire file into memory.

        This is a memory-efficient alternative to read() for large files.
        Yields chunks as an iterator, allowing processing of files larger than RAM.

        Args:
            path: Virtual path to stream
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If stream operation fails
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> # Stream large file efficiently
            >>> for chunk in nx.stream("/workspace/large_file.bin"):
            ...     process(chunk)  # Memory usage = chunk_size, not file_size

            >>> # Stream to output
            >>> import sys
            >>> for chunk in nx.stream("/workspace/video.mp4", chunk_size=1024*1024):  # 1MB chunks
            ...     sys.stdout.buffer.write(chunk)
        """
        path = self._validate_path(path)

        # PRE-INTERCEPT: pre-read hooks (Issue #899)
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        # Route to backend with access control
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        # Check if file exists in metadata
        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        # Stream from routed backend using content hash
        yield from route.backend.stream_content(meta.etag, chunk_size=chunk_size, context=context)

    @rpc_expose(description="Stream a byte range of file content")
    def stream_range(
        self,
        path: str,
        start: int,
        end: int,
        chunk_size: int = 65536,
        context: OperationContext | None = None,
    ) -> Any:
        """Stream a byte range [start, end] of file content.

        This is the kernel-level range streaming method.  HTTP routers use
        this (via ``build_range_response``) to implement RFC 9110 Range
        requests without bypassing the ObjectStore abstraction.

        Args:
            path: Virtual path to stream
            start: Start byte offset (inclusive)
            end: End byte offset (inclusive)
            chunk_size: Size of each chunk in bytes (default: 8KB)
            context: Optional operation context for permission checks

        Yields:
            bytes: Chunks of file content within the requested range
        """
        path = self._validate_path(path)
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
        )

        meta = self.metadata.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        yield from route.backend.stream_range(
            meta.etag, start, end, chunk_size=chunk_size, context=context
        )

    @rpc_expose(description="Write file content from stream")
    async def write_stream(
        self,
        path: str,
        chunks: Iterator[bytes],
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """
        Write file content from an iterator of chunks.

        This is a memory-efficient alternative to write() for large files.
        Accepts chunks as an iterator, computing hash incrementally.

        Args:
            path: Virtual path to write
            chunks: Iterator yielding byte chunks
            context: Optional operation context for permission checks

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only or user doesn't have write permission

        Example:
            >>> # Stream large file without loading into memory
            >>> def file_chunks(path, chunk_size=8192):
            ...     with open(path, 'rb') as f:
            ...         while chunk := f.read(chunk_size):
            ...             yield chunk
            >>> result = nx.write_stream("/workspace/large.bin", file_chunks("/tmp/large.bin"))
        """
        path = self._validate_path(path)

        # Route to backend with write access check
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(_WHC(path=path, content=b"", context=context))

        # Get existing metadata for version tracking
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # Add backend_path to context for path-based connectors
        if context:
            context = _dc_replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )

        # Write content via streaming
        write_result = route.backend.write_stream(chunks, context=context)
        content_hash = write_result.content_id

        # WriteResult carries the byte count to avoid a redundant
        # get_content_size() round-trip after streaming writes.
        size = write_result.size
        if size <= 0:
            try:
                size = route.backend.get_content_size(content_hash, context=context)
            except Exception as e:
                logger.debug("Failed to get content size for %s: %s", content_hash, e)

        # Update metadata
        new_version = (meta.version + 1) if meta else 1
        new_meta = FileMetadata(
            path=path,
            backend_name=route.backend.name,
            physical_path=content_hash,  # CAS: hash is the "physical" location
            etag=content_hash,
            size=size,
            version=new_version,
            created_at=meta.created_at if meta else now,
            modified_at=now,
            zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
        )

        self.metadata.put(new_meta)

        # Issue #3391: OBSERVE dispatch was missing for write_stream — add it.
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path=path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                etag=content_hash,
                size=size,
                version=new_version,
                is_new=(meta is None),
                old_etag=meta.etag if meta else None,
            )
        )

        # Issue #900: Unified INTERCEPT for write_stream
        from nexus.contracts.vfs_hooks import WriteHookContext

        _ws_ctx = WriteHookContext(
            path=path,
            content=b"",  # stream — content not available in single buffer
            context=None,
            zone_id=zone_id,
            is_new_file=(meta is None),
            metadata=new_meta,
        )
        await self._dispatch.intercept_post_write(_ws_ctx)

        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now.isoformat(),
            "size": size,
        }

    @rpc_expose(description="Write file content")
    async def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        """Write content to a file (POSIX write(2)).

        Tier 1 kernel primitive — content-only (SRP). Metadata consistency
        is controlled by Tier 2 write(consistency=). File must exist.

        Args:
            path: Virtual path to write.
            buf: File content as bytes or str (str will be UTF-8 encoded).
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset for partial write (POSIX pwrite semantics, 0=whole-file).
            context: Optional operation context for permission checks.
            ttl: TTL in seconds for ephemeral content (Issue #3405).
                Routes to TTL-bucketed volume; None = permanent.

        Returns:
            Dict with path and bytes_written.

        Raises:
            NexusFileNotFoundError: If file does not exist.
            InvalidPathError: If path is invalid.
            BackendError: If write operation fails.
            AccessDeniedError: If access is denied (zone isolation or read-only namespace).
            PermissionError: If path is read-only or user doesn't have write permission.
        """
        # DT_PIPE fast-path: skip ALL preprocessing + validate/metastore/dispatch.
        # Pipe is a byte FIFO — callers always pass bytes, count/offset are file concepts.
        # Fully inlined: single dict lookup → Rust write_nowait. No wrapper calls.
        _pm = self._pipe_manager
        if _pm is not None:
            _buf = _pm._buffers.get(path)
            if _buf is not None:
                n = _buf.write_nowait(buf if isinstance(buf, bytes) else buf.encode("utf-8"))
                return {"path": path, "bytes_written": n}
        # DT_STREAM fast-path: same rationale — StreamManager._buffers is authoritative.
        # Fully inlined: single dict.get → buffer.write_nowait (Rust). No wrapper calls.
        _sbuf = self._stream_manager._buffers.get(path)
        if _sbuf is not None:
            if isinstance(buf, str):
                buf = buf.encode("utf-8")
            _off = _sbuf.write_nowait(buf)
            return {"path": path, "bytes_written": len(buf), "offset": _off}

        # Auto-convert str to bytes for convenience
        if isinstance(buf, str):
            buf = buf.encode("utf-8")

        # Apply count slicing if specified
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            base = {"path": path, "bytes_written": len(buf)}
            if isinstance(_result, dict):
                base.update(_result)
            return base

        # DT_PIPE / DT_STREAM: kernel-native IPC dispatch (§4.2)
        _meta = self.metadata.get(path)
        if _meta is not None and _meta.is_pipe:
            # Fallback for pipes not in PipeManager (e.g. federation remote pipes)
            n = self._pipe_write(path, buf)
            return {"path": path, "bytes_written": n}
        if _meta is not None and _meta.is_stream:
            offset = self._stream_write(path, buf)
            return {"path": path, "bytes_written": len(buf), "offset": offset}

        if _meta is None:
            raise NexusFileNotFoundError(
                path, "sys_write requires existing file — use write() for create-on-write"
            )
        # Thread TTL into context (Issue #3405)
        if ttl is not None and ttl > 0:
            context = self._ensure_context_ttl(context, ttl)
        await self._write_internal(path=path, content=buf, offset=offset, context=context)
        return {"path": path, "bytes_written": len(buf)}

    # ── Tier 2 overrides (NexusFS-specific) ───────────────────────

    @rpc_expose(description="Create directory")
    async def mkdir(
        self,
        path: str,
        parents: bool = True,
        exist_ok: bool = True,
        *,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory (Tier 2 convenience over sys_setattr).

        Defaults: parents=True, exist_ok=True (mkdir -p semantics).
        Uses _setattr_create(DT_DIR) for metadata creation.
        """
        path = self._validate_path(path)
        ctx = self._resolve_cred(context)

        # PRE-INTERCEPT: pre-mkdir hooks (Issue #899)
        from nexus.contracts.vfs_hooks import MkdirHookContext

        self._dispatch.intercept_pre_mkdir(MkdirHookContext(path=path, context=ctx))

        # Route to backend with write access check
        route = self.router.route(path, is_admin=ctx.is_admin, check_write=True)

        if route.readonly:
            raise PermissionError(f"Cannot create directory in read-only path: {path}")

        # Check if directory already exists
        existing = self.metadata.get(path)
        is_implicit_dir = existing is None and self.metadata.is_implicit_directory(path)

        if existing is not None or is_implicit_dir:
            if not exist_ok and not parents:
                raise FileExistsError(f"Directory already exists: {path}")
            # DT_MOUNT entries are created by PathRouter.add_mount() *before*
            # mkdir is called, so parent dirs may still need metadata.
            if existing is not None:
                if parents:
                    self._ensure_parent_directories(path, ctx)
                return

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True, context=ctx)

        # Create parent directory metadata
        if parents:
            self._ensure_parent_directories(path, ctx)

        # Create directory inode via _setattr_create (DT_DIR)
        self._setattr_create(
            path,
            DT_DIR,
            {
                "zone_id": ctx.zone_id or ROOT_ZONE_ID,
            },
        )

        # Issue #900/#3391: Unified two-phase dispatch — OBSERVE then INTERCEPT
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_CREATE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )
        await self._dispatch.intercept_post_mkdir(
            MkdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
        )

    async def rmdir(
        self,
        path: str,
        recursive: bool = True,
        context: OperationContext | None = None,
    ) -> None:
        """Remove a directory with lenient defaults (Tier 2 convenience).

        Unlike sys_rmdir (recursive=False), this defaults to recursive=True
        — rm -rf semantics. Delegates to sys_rmdir.
        """
        await self.sys_rmdir(path, recursive=recursive, context=context)

    @rpc_expose(description="Read file with optional metadata")
    async def read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        return_metadata: bool = False,
    ) -> bytes | dict[str, Any]:
        """Read with optional metadata (VFS convenience).

        Composes sys_stat + sys_read.  POSIX pread semantics.

        Args:
            path: Virtual file path.
            count: Max bytes to read (None = entire file).
            offset: Byte offset to start reading from.
            context: Operation context.
            return_metadata: If True, return dict with content + metadata.

        Returns:
            bytes if return_metadata=False, else dict with content + metadata.
        """
        content = await self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Compose with sys_stat for metadata
        meta_dict = await self.sys_stat(path, context=context)
        result: dict[str, Any] = {"content": content}
        if meta_dict:
            result.update(
                {
                    "etag": meta_dict.get("etag"),
                    "version": meta_dict.get("version"),
                    "modified_at": meta_dict.get("modified_at"),
                    "size": len(content),
                }
            )
        return result

    @rpc_expose(description="Write file with metadata return")
    async def write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
        consistency: str | None = None,
        ttl: float | None = None,
    ) -> dict[str, Any]:
        """Write with metadata return (Tier 2 convenience).

        Overrides ABC default. Returns dict with metadata
        (etag, version, modified_at, size).

        OCC (if_match, if_none_match) is NOT here — use ``lib.occ.occ_write()``
        to compose OCC + write at the caller level (RPC handler, CLI, SDK).

        Distributed locking is NOT here — use ``lock()``/``unlock()`` or
        ``async with locked(path)`` to compose locking at the caller level.
        See Issue #1323.

        Args:
            path: Virtual file path.
            buf: File content as bytes or str.
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset for partial write (POSIX pwrite semantics, 0=whole-file).
            context: Operation context.
            consistency: Metadata consistency mode — ``"sc"`` (strong, Raft
                consensus) or ``"ec"`` (eventual, fire-and-forget).
                Defaults to ``"sc"``.  Use ``"ec"`` for low-latency writes
                where immediate durability is not required.
            ttl: TTL in seconds for ephemeral content (Issue #3405).
                Routes to TTL-bucketed volume; None = permanent.

        Returns:
            Dict with metadata (etag, version, modified_at, size).
        """
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)
        _consistency = consistency or "sc"

        # PRE-DISPATCH: virtual path resolvers
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            return _result

        # Thread TTL into context (Issue #3405)
        if ttl is not None and ttl > 0:
            context = self._ensure_context_ttl(context, ttl)

        return await self._write_internal(
            path=path, content=buf, offset=offset, context=context, consistency=_consistency
        )

    async def _write_internal(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        consistency: str = "sc",
        offset: int = 0,
    ) -> dict[str, Any]:
        """Kernel write implementation — OCC-free.

        Thin composition of _write_content (locked I/O) + _dispatch_write_events
        (async event dispatch).

        OCC checks (if_match, if_none_match) are done by callers
        (write() convenience method or RPC handlers) BEFORE calling this.

        Used by both sys_write (returns int) and write() (returns dict).

        Issue #1323: OCC params removed from kernel write path.
        Issue #1829: Split into _write_content + _dispatch_write_events (SRP).
        """
        wr = self._write_content(path, content, context, offset=offset, consistency=consistency)
        return await self._dispatch_write_events(path, wr, content)

    def _write_content(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
        offset: int = 0,
        consistency: str = "sc",
    ) -> _WriteContentResult:
        """Content write + metadata commit (locked, synchronous).

        Handles routing, pre-write hooks, backend write, metadata build+put.
        Both ExternalRouteResult and standard VFS paths.

        The VFS lock wraps both content write AND metadata put for atomicity.

        Returns:
            _WriteContentResult for async event dispatch by _dispatch_write_events.
        """
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # Route to backend with write access check FIRST (to check zone/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        zone_id, agent_id, is_admin = self._get_context_identity(context)

        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = self.metadata.get(path)

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        # Hook handles existing-file (owner fast-path) vs new-file (parent check)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(
            _WHC(
                path=path,
                content=content,
                context=context,
                old_metadata=meta,
            )
        )

        # Add backend_path to context for path-based connectors
        from dataclasses import replace

        if context:
            context = replace(context, backend_path=route.backend_path, virtual_path=path)
        else:
            from nexus.contracts.types import OperationContext

            context = OperationContext(
                user_id="anonymous", groups=[], backend_path=route.backend_path, virtual_path=path
            )
        context = replace(context, existing_metadata=meta)

        # DT_EXTERNAL_STORAGE: backend manages own content storage.
        # Remote backends (RPC-based) also persist metadata on the remote server,
        # so we skip local metadata.put() to avoid overwriting.
        # Local external backends (e.g. LocalConnector) write content
        # to disk but do NOT manage metadata — we must persist metadata locally.
        from nexus.core.router import ExternalRouteResult

        _is_remote = hasattr(route.backend, "_rpc_client") or "remote" in route.backend.name
        _is_external = isinstance(route, ExternalRouteResult)
        # VFS I/O Lock: exclusive write lock around backend write + metadata put.
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        # Applies to ALL backends (external and CAS) to prevent concurrent write interleave.
        with self._vfs_locked(path, "write"):
            if _is_external:
                wr = route.backend.write_content(
                    content,
                    content_id=meta.physical_path if (offset > 0 and meta) else "",
                    offset=offset,
                    context=context,
                )
                content_hash = wr.content_id
                metadata = self._build_write_metadata(
                    path=path,
                    backend_name=route.backend.name,
                    content_hash=content_hash,
                    size=wr.size if offset > 0 else len(content),
                    existing_meta=meta,
                    now=now,
                    zone_id=zone_id,
                    context=context,
                )
                new_version = metadata.version
                # Local external backends need metadata persisted locally
                if not _is_remote:
                    self.metadata.put(metadata, consistency=consistency)
            else:
                _wr = route.backend.write_content(
                    content,
                    content_id=meta.physical_path if (offset > 0 and meta) else "",
                    offset=offset,
                    context=context,
                )
                content_hash = _wr.content_id

                # NOTE: sys_write does NOT release old content on overwrite.
                # HDFS/GFS pattern: content cleanup is async via background GC.
                # See: docs/architecture/federation-memo.md §7f Caveat 4.

                # Driver persists metadata inside write_content() (metastore
                # injected at mount time via DLC).
                _post_meta = self.metadata.get(path)
                if _post_meta is None:
                    raise BackendError(
                        f"write_content() completed but metadata not found for {path}. "
                        "Driver must persist metadata during write_content()."
                    )
                metadata = _post_meta
                new_version = metadata.version

        return _WriteContentResult(
            content_hash=content_hash,
            size=metadata.size,
            metadata=metadata,
            new_version=new_version,
            is_new=(meta is None),
            old_etag=meta.etag if meta else None,
            old_metadata=meta,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            is_remote=_is_remote,
            is_external=_is_external,
        )

    async def _dispatch_write_events(
        self,
        path: str,
        result: _WriteContentResult,
        content: bytes,
    ) -> dict[str, Any]:
        """Post-write event dispatch (async, outside lock).

        Fires FileEvent notify (OBSERVE) + intercept_post_write hooks (INTERCEPT).
        Uses the augmented context from _write_content (stored in result).

        Returns:
            Dict with metadata {etag, version, modified_at, size}.
        """
        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path=path,
                zone_id=result.zone_id or ROOT_ZONE_ID,
                agent_id=result.agent_id,
                etag=result.content_hash,
                size=result.size,
                version=result.new_version,
                is_new=result.is_new,
                old_etag=result.old_etag,
            )
        )

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        from nexus.contracts.vfs_hooks import WriteHookContext

        _write_ctx = WriteHookContext(
            path=path,
            content=content,
            context=result.context,
            zone_id=result.zone_id,
            agent_id=result.agent_id,
            is_new_file=result.is_new,
            content_hash=result.content_hash,
            metadata=result.metadata,
            old_metadata=result.old_metadata,
            new_version=result.new_version,
        )
        await self._dispatch.intercept_post_write(_write_ctx)

        # Return metadata for optimistic concurrency control
        return {
            "etag": result.content_hash,
            "version": result.new_version,
            "modified_at": result.metadata.modified_at,
            "size": result.size,
        }

    async def atomic_update(
        self,
        path: str,
        update_fn: Callable[[bytes], bytes],
        context: OperationContext | None = None,
        timeout: float = 30.0,
        ttl: float = 30.0,
    ) -> dict[str, Any]:
        """Atomically read-modify-write a file with distributed locking.

        This is the recommended API for concurrent file updates where you need
        to read existing content, modify it, and write back atomically.

        The operation:
        1. Acquires distributed lock on the path
        2. Reads current file content
        3. Applies your update function
        4. Writes modified content
        5. Releases lock (even on failure)

        For multiple operations within one lock, use `async with locked()` instead.

        Args:
            path: Virtual path to update
            update_fn: Function that transforms content (bytes -> bytes).
                      Receives current file content, returns new content.
            context: Operation context (optional)
            timeout: Maximum time to wait for lock in seconds (default: 30.0)
            ttl: Lock TTL in seconds (default: 30.0)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash of the new content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            LockTimeout: If lock cannot be acquired within timeout
            NexusFileNotFoundError: If file doesn't exist
            BackendError: If read or write operation fails

        Example:
            >>> # Increment a counter atomically
            >>> import json
            >>> await nx.atomic_update(
            ...     "/counters/visits.json",
            ...     lambda c: json.dumps({"count": json.loads(c)["count"] + 1}).encode()
            ... )

            >>> # Append to a log file atomically
            >>> await nx.atomic_update(
            ...     "/logs/access.log",
            ...     lambda c: c + b"New log entry\\n"
            ... )

            >>> # Update config safely across multiple agents
            >>> await nx.atomic_update(
            ...     "/shared/config.json",
            ...     lambda c: json.dumps({**json.loads(c), "version": 2}).encode()
            ... )
        """
        async with self.locked(path, timeout=timeout, ttl=ttl, context=context) as lock_id:  # noqa: F841
            content = await self.sys_read(path, context=context)
            new_content = update_fn(content)
            return await self.write(path, new_content, context=context)

    @rpc_expose(description="Append content to an existing file or create if it doesn't exist")
    async def append(
        self,
        path: str,
        content: bytes | str,
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """
        Append content to an existing file or create a new file if it doesn't exist.

        This is an efficient way to add content to files without reading the entire
        file separately, particularly useful for:
        - Writing JSONL (JSON Lines) logs incrementally
        - Appending to log files
        - Building append-only data structures
        - Streaming data collection

        Args:
            path: Virtual path to append to
            content: Content to append as bytes or str (str will be UTF-8 encoded)
            context: Optional operation context for permission checks (uses default if not provided)
            if_match: Optional etag for optimistic concurrency control.
                     If provided, append only succeeds if current file etag matches this value.
                     Prevents concurrent modification conflicts.
            force: If True, skip version check and append unconditionally (dangerous!)

        Returns:
            Dict with metadata about the written file:
                - etag: Content hash (SHA-256) of the final content (after append)
                - version: New version number
                - modified_at: Modification timestamp
                - size: Final file size in bytes

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If append operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
            ConflictError: If if_match is provided and doesn't match current etag
            NexusFileNotFoundError: If file doesn't exist during read (should not happen in normal flow)

        Examples:
            >>> # Append to a log file
            >>> nx.append("/workspace/app.log", "New log entry\\n")

            >>> # Build JSONL file incrementally
            >>> import json
            >>> for record in records:
            ...     line = json.dumps(record) + "\\n"
            ...     nx.append("/workspace/data.jsonl", line)

            >>> # Append with optimistic concurrency control
            >>> result = nx.read("/workspace/log.txt", return_metadata=True)
            >>> try:
            ...     nx.append("/workspace/log.txt", "New entry\\n", if_match=result['etag'])
            ... except ConflictError:
            ...     print("File was modified by another process!")

            >>> # Create new file if doesn't exist
            >>> nx.append("/workspace/new.txt", "First line\\n")
        """
        # Auto-convert str to bytes for convenience
        if isinstance(content, str):
            content = content.encode("utf-8")

        path = self._validate_path(path)

        # Try to read existing content if file exists
        # For non-existent files, we'll create them (existing_content stays empty)
        existing_content = b""
        try:
            result = await self.read(path, context=context, return_metadata=True)
            # Tier 2 read(return_metadata=True) always returns dict
            assert isinstance(result, dict), "Expected dict when return_metadata=True"

            existing_content = result["content"]

            # If if_match is provided, verify it matches current etag
            # (the write call will also check, but we check here to fail fast)
            if if_match is not None and not force:
                current_etag = result.get("etag")
                if current_etag != if_match:
                    from nexus.contracts.exceptions import ConflictError

                    raise ConflictError(
                        path=path,
                        expected_etag=if_match,
                        current_etag=current_etag or "(no etag)",
                    )
        except Exception as e:
            # If file doesn't exist, treat as empty (will create new file)
            from nexus.contracts.exceptions import NexusFileNotFoundError

            if not isinstance(e, NexusFileNotFoundError):
                # Re-raise unexpected errors (including PermissionError)
                raise
            # For FileNotFoundError, continue with empty content
            # write() will check if user has permission to create the file

        # Combine existing content with new content
        final_content = existing_content + content

        # Use the existing write method to handle all the complexity:
        # - Permission checking
        # - Version management
        # - Audit logging
        # - Workflow triggers
        # - Parent tuple creation
        # OCC check already done above (line 2985-2996), so just write.
        return await self.write(
            path,
            final_content,
            context=context,
        )

    @rpc_expose(description="Apply surgical search/replace edits to a file")
    async def edit(
        self,
        path: str,
        edits: list[tuple[str, str]] | list[dict[str, Any]] | list[Any],
        *,
        context: OperationContext | None = None,
        if_match: str | None = None,
        fuzzy_threshold: float = 0.85,
        preview: bool = False,
    ) -> dict[str, Any]:
        """
        Apply surgical search/replace edits to a file.

        This enables precise file modifications without rewriting entire files,
        reducing token cost and errors when used with LLMs.

        Issue #800: Add edit engine with search/replace for surgical file edits.

        Uses a layered matching strategy:
        1. Exact match (fast path)
        2. Whitespace-normalized match
        3. Fuzzy match (Levenshtein similarity)

        Args:
            path: Virtual path to edit
            edits: List of edit operations. Each edit can be:
                - Tuple: (old_str, new_str) - simple search/replace
                - Dict: {"old_str": str, "new_str": str, "hint_line": int | None,
                         "allow_multiple": bool} - full control
                - EditOperation: Direct EditOperation instance
            context: Optional operation context for permission checks
            if_match: Optional etag for optimistic concurrency control.
                If provided, edit fails if file changed since read.
            fuzzy_threshold: Similarity threshold (0.0-1.0) for fuzzy matching.
                Default 0.85. Use 1.0 for exact matching only.
            preview: If True, return preview without writing. Default False.

        Returns:
            Dict containing:
                - success: bool - True if all edits applied
                - diff: str - Unified diff of changes
                - matches: list[dict] - Info about each match (type, line, similarity)
                - applied_count: int - Number of edits applied
                - etag: str - New etag (if not preview)
                - version: int - New version (if not preview)
                - errors: list[str] - Error messages if any edits failed

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If path is read-only
            ConflictError: If if_match doesn't match current etag

        Examples:
            >>> # Simple search/replace
            >>> result = nx.edit("/code/main.py", [
            ...     ("def foo():", "def bar():"),
            ...     ("return x", "return x + 1"),
            ... ])
            >>> print(result['diff'])

            >>> # With optimistic concurrency
            >>> content = nx.read("/code/main.py", return_metadata=True)
            >>> result = nx.edit(
            ...     "/code/main.py",
            ...     [("old_text", "new_text")],
            ...     if_match=content['etag']
            ... )

            >>> # Preview without writing
            >>> result = nx.edit("/code/main.py", edits, preview=True)
            >>> if result['success']:
            ...     print(result['diff'])

            >>> # With fuzzy matching
            >>> result = nx.edit("/code/main.py", [
            ...     {"old_str": "def foo():", "new_str": "def bar():", "hint_line": 42}
            ... ], fuzzy_threshold=0.8)
        """
        from nexus.utils.edit_engine import EditEngine
        from nexus.utils.edit_engine import EditOperation as EditOp

        path = self._validate_path(path)

        # Read current content with metadata (via Tier 2 convenience)
        result = await self.read(path, context=context, return_metadata=True)
        assert isinstance(result, dict), "Expected dict when return_metadata=True"

        content_bytes: bytes = result["content"]
        current_etag = result.get("etag")

        # Check etag if provided (optimistic concurrency control)
        if if_match is not None and current_etag != if_match:
            raise ConflictError(
                path=path,
                expected_etag=if_match,
                current_etag=current_etag or "(no etag)",
            )

        # Decode content to string for editing
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            return {
                "success": False,
                "diff": "",
                "matches": [],
                "applied_count": 0,
                "errors": [f"File is not valid UTF-8 text: {e}"],
            }

        # Convert edits to EditOperation instances
        edit_operations: list[EditOp] = []
        for edit in edits:
            if isinstance(edit, EditOp):
                edit_operations.append(edit)
            elif isinstance(edit, tuple | list) and len(edit) >= 2:
                # Handle both tuple and list (JSON deserializes tuples as lists)
                edit_operations.append(EditOp(old_str=edit[0], new_str=edit[1]))
            elif isinstance(edit, dict):
                edit_operations.append(
                    EditOp(
                        old_str=edit["old_str"],
                        new_str=edit["new_str"],
                        hint_line=edit.get("hint_line"),
                        allow_multiple=edit.get("allow_multiple", False),
                    )
                )
            else:
                return {
                    "success": False,
                    "diff": "",
                    "matches": [],
                    "applied_count": 0,
                    "errors": [
                        f"Invalid edit format: expected tuple (old, new), dict, or EditOperation, got {type(edit)}"
                    ],
                }

        # Apply edits
        engine = EditEngine(
            fuzzy_threshold=fuzzy_threshold,
            enable_fuzzy=fuzzy_threshold < 1.0,
        )
        edit_result = engine.apply_edits(content, edit_operations)

        # Convert matches to serializable dicts
        matches_list = [
            {
                "edit_index": m.edit_index,
                "match_type": m.match_type,
                "similarity": m.similarity,
                "line_start": m.line_start,
                "line_end": m.line_end,
                "original_text": m.original_text[:200] if m.original_text else "",
                "search_strategy": m.search_strategy,
                "match_count": m.match_count,
            }
            for m in edit_result.matches
        ]

        # If edits failed, return error without writing
        if not edit_result.success:
            return {
                "success": False,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "errors": edit_result.errors,
            }

        # If preview mode, return without writing
        if preview:
            return {
                "success": True,
                "diff": edit_result.diff,
                "matches": matches_list,
                "applied_count": edit_result.applied_count,
                "preview": True,
                "new_content": edit_result.content,
            }

        # Write the edited content. OCC check already done above (line 3117-3123).
        new_content_bytes = edit_result.content.encode("utf-8")
        write_result = await self.write(
            path,
            new_content_bytes,
            context=context,
        )

        return {
            "success": True,
            "diff": edit_result.diff,
            "matches": matches_list,
            "applied_count": edit_result.applied_count,
            "etag": write_result.get("etag"),
            "version": write_result.get("version"),
            "size": write_result.get("size"),
            "modified_at": write_result.get("modified_at"),
        }

    @rpc_expose(description="Write multiple files in a single transaction")
    async def write_batch(
        self, files: list[tuple[str, bytes]], context: OperationContext | None = None
    ) -> list[dict[str, Any]]:
        """
        Write multiple files in a single transaction for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        All files are written atomically - either all succeed or all fail.

        Args:
            files: List of (path, content) tuples to write
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            List of metadata dicts for each file (in same order as input):
                - etag: Content hash (SHA-256) of the written content
                - version: New version number
                - modified_at: Modification timestamp
                - size: File size in bytes

        Raises:
            InvalidPathError: If any path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If any path is read-only or user doesn't have write permission

        Examples:
            >>> # Write 100 small files in a single batch (13x faster!)
            >>> files = [(f"/logs/file_{i}.txt", b"log data") for i in range(100)]
            >>> results = nx.write_batch(files)
            >>> print(f"Wrote {len(results)} files")

            >>> # Atomic batch write - all or nothing
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
        """
        if not files:
            return []

        # Validate all paths first
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            validated_files.append((validated_path, content))

        # Route all paths and check write access
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        routes = []
        for path, _ in validated_files:
            route = self.router.route(
                path,
                is_admin=is_admin,
                check_write=True,
            )
            # Check if path is read-only
            if route.readonly:
                raise PermissionError(f"Path is read-only: {path}")
            routes.append(route)

        # Get existing metadata for all paths (single query)
        paths = [path for path, _ in validated_files]
        existing_metadata = self.metadata.get_batch(paths)

        # PRE-INTERCEPT: pre-write hooks per file in batch
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        for path in paths:
            meta = existing_metadata.get(path)
            self._dispatch.intercept_pre_write(
                _WHC(
                    path=path,
                    content=b"",
                    context=context,
                    old_metadata=meta,
                )
            )

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        # Write all content to backend (deduplicated automatically for CAS)
        for (path, content), route in zip(validated_files, routes, strict=False):
            # Add backend_path to context for path-based backends
            if context:
                _write_ctx = _dc_replace(
                    context, backend_path=route.backend_path, virtual_path=path
                )
            else:
                _write_ctx = OperationContext(
                    user_id="anonymous",
                    groups=[],
                    backend_path=route.backend_path,
                    virtual_path=path,
                )
            content_hash = route.backend.write_content(content, context=_write_ctx).content_id

            # Get existing metadata for this file
            meta = existing_metadata.get(path)

            # UNIX permissions removed - all access control via ReBAC

            # Calculate new version number (increment if updating)
            new_version = (meta.version + 1) if meta else 1

            # Build metadata for batch insert
            # Note: UNIX permissions (owner/group/mode) removed - use ReBAC instead
            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,  # FIX: Use routed backend name, not default backend
                physical_path=content_hash,  # CAS: hash is the "physical" location
                size=len(content),
                etag=content_hash,  # SHA-256 hash for integrity
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
            )
            metadata_list.append(metadata)

            # Build result dict
            results.append(
                {
                    "etag": content_hash,
                    "version": new_version,
                    "modified_at": now,
                    "size": len(content),
                }
            )

        # Store all metadata in a single transaction (with version history)
        self.metadata.put_batch(metadata_list)

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        items = [
            (metadata, existing_metadata.get(metadata.path) is None) for metadata in metadata_list
        ]
        await self._dispatch.intercept_post_write_batch(
            items,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
        )

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        for metadata in metadata_list:
            old_meta = existing_metadata.get(metadata.path)
            is_new = old_meta is None
            await self._dispatch.notify(
                FileEvent(
                    type=FileEventType.FILE_WRITE,
                    path=metadata.path,
                    zone_id=zone_id or ROOT_ZONE_ID,
                    agent_id=agent_id,
                    etag=metadata.etag,
                    size=metadata.size,
                    version=metadata.version,
                    is_new=is_new,
                    old_etag=old_meta.etag if old_meta else None,
                )
            )

        # Issue #1682: Hierarchy tuples + owner grants moved to post_write_batch hooks.

        return results

    @rpc_expose(description="Delete file")
    async def sys_unlink(
        self,
        path: str,
        *,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Remove a file or directory entry.

        Unified delete syscall — handles both files and directories.
        For directories, set ``recursive=True`` to delete non-empty dirs.

        Args:
            path: Virtual path to delete (supports memory, pipe, stream paths).
            recursive: If True and target is a directory, delete all children
                first (rm -rf). If False and directory is non-empty, raises
                OSError(ENOTEMPTY). Ignored for regular files.
            context: Optional operation context for permission checks.

        Returns:
            Dict on success. When operating in overlay mode and the file
            exists only in the base layer, creates a whiteout marker instead
            of deleting. Returns ``{"overlay_whiteout": True}`` in that case.

        Raises:
            NexusFileNotFoundError: If file doesn't exist.
            InvalidPathError: If path is invalid.
            BackendError: If delete operation fails.
            OSError(ENOTEMPTY): If directory is non-empty and recursive=False.
            PermissionError: If path is read-only or user doesn't have write permission.
        """
        # ── /__sys__/ kernel management dispatch ──────────────────────
        if path.startswith("/__sys__/services/"):
            name = path.rsplit("/", 1)[-1]
            await self._service_registry.unregister_service_full(name)
            return {"path": path, "unregistered": True, "service": name}

        if path.startswith("/__sys__/hooks/"):
            raise NotImplementedError(
                "Standalone hook removal via /__sys__/hooks/ not yet supported."
            )

        # DT_PIPE fast-path: skip validate/zone_check/resolve/metastore.get
        if self._pipe_manager is not None and path in self._pipe_manager._buffers:
            return self._pipe_destroy(path)
        # DT_STREAM fast-path: same rationale — StreamManager._buffers is authoritative.
        if path in self._stream_manager._buffers:
            return self._stream_destroy(path)

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self._dispatch.resolve_delete(path, context=context)
        if _handled:
            return _result

        # DT_PIPE / DT_STREAM: kernel-native IPC destroy (§4.2)
        _ipc_meta = self.metadata.get(path)
        if _ipc_meta is not None and _ipc_meta.is_pipe:
            return self._pipe_destroy(path)
        if _ipc_meta is not None and _ipc_meta.is_stream:
            return self._stream_destroy(path)

        # Route to backend with write access check FIRST (to check zone/agent isolation)
        # This must happen before permission check so AccessDeniedError is raised before PermissionError
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Check if file exists in metadata.
        # Use prefetched hint from resolve_delete() if available (#1311)
        meta = _result if _result is not None else self.metadata.get(path)

        # Issue #1264: If file exists only in base layer, create whiteout instead of deleting
        if meta is None and getattr(self, "_overlay_resolver", None):
            overlay_config = self._get_overlay_config(path)
            if overlay_config:
                base_meta = self._overlay_resolver.resolve_read(path, overlay_config)
                if base_meta is not None and not self._overlay_resolver.is_whiteout(base_meta):
                    self._overlay_resolver.create_whiteout(path, overlay_config)
                    return {"deleted": path, "overlay_whiteout": True}

        if meta is None:
            raise NexusFileNotFoundError(path)

        # ── Directory branch: rmdir logic ────────────────────────────
        if meta.is_dir or meta.is_mount:
            return await self._unlink_directory(
                path, meta=meta, route=route, recursive=recursive, context=context
            )

        # ── File branch: regular unlink ──────────────────────────────

        # PRE-INTERCEPT: pre-delete hooks (Issue #899)
        from nexus.contracts.vfs_hooks import DeleteHookContext as _DHC

        self._dispatch.intercept_pre_delete(_DHC(path=path, context=context))

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        from nexus.contracts.vfs_hooks import DeleteHookContext

        _delete_ctx = DeleteHookContext(
            path=path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            metadata=meta,
        )
        await self._dispatch.intercept_post_delete(_delete_ctx)

        # VFS I/O Lock: exclusive write lock around CAS delete + metadata delete.
        with self._vfs_locked(path, "write"):
            self.metadata.delete(path)

        # --- Lock released — event dispatch ---
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_DELETE,
                path=path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                etag=meta.etag,
                size=meta.size,
            )
        )

        return {}

    async def _unlink_directory(
        self,
        path: str,
        *,
        meta: "FileMetadata",
        route: Any,
        recursive: bool,
        context: OperationContext | None,
    ) -> dict[str, Any]:
        """Internal: directory delete logic (extracted from former sys_rmdir).

        Handles DT_MOUNT unmount, ENOTEMPTY check, recursive child delete,
        backend rmdir, sparse index cleanup, and rmdir hook dispatch.
        """
        import errno

        ctx = self._resolve_cred(context)

        from nexus.contracts.vfs_hooks import RmdirHookContext

        self._dispatch.intercept_pre_rmdir(RmdirHookContext(path=path, context=ctx))

        # DT_MOUNT: unmount via DriverLifecycleCoordinator + delete metadata
        if meta.is_mount:
            removed = self._driver_coordinator.unmount(path)
            if removed:
                self.metadata.delete(path)
                logger.info("sys_unlink: unmounted %s", path)
            return {}

        # Check if directory contains any files
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir:
            if not recursive:
                raise OSError(errno.ENOTEMPTY, f"Directory not empty: {path}")
            # Recursive: batch delete all children
            file_paths = [file_meta.path for file_meta in files_in_dir]
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (suppress errors — CAS may not have physical dir)
        with contextlib.suppress(NexusFileNotFoundError, BackendError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Delete directory's own metadata entry
        try:
            self.metadata.delete(path)
        except Exception as e:
            logger.debug("Failed to delete directory metadata for %s: %s", path, e)

        # Clean up sparse directory index entries
        if hasattr(self.metadata, "delete_directory_entries_recursive"):
            try:
                self.metadata.delete_directory_entries_recursive(path)
            except Exception as e:
                logger.debug("Failed to clean up directory index for %s: %s", path, e)

        # OBSERVE then INTERCEPT (Issue #3391)
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_DELETE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )
        await self._dispatch.intercept_post_rmdir(
            RmdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                recursive=recursive,
            )
        )

        return {}

    @rpc_expose(description="Rename/move file")
    async def sys_rename(
        self, old_path: str, new_path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """
        Rename/move a file by updating its path in metadata.

        This is a metadata-only operation that does NOT copy file content.
        The file's content remains in the same location in CAS storage,
        only the virtual path is updated in the metadata database.

        This makes rename/move operations instant, regardless of file size.

        Args:
            old_path: Current virtual path
            new_path: New virtual path
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (zone isolation)

        Example:
            >>> await nx.sys_rename('/workspace/old.txt', '/workspace/new.txt')
            >>> await nx.sys_rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
        """
        old_path = self._validate_path(old_path)
        new_path = self._validate_path(new_path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # Route both paths
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        old_route = self.router.route(
            old_path,
            is_admin=is_admin,
            check_write=True,  # Need write access to source
        )
        new_route = self.router.route(
            new_path,
            is_admin=is_admin,
            check_write=True,  # Need write access to destination
        )

        # Check if paths are read-only
        if old_route.readonly:
            raise PermissionError(f"Cannot rename from read-only path: {old_path}")
        if new_route.readonly:
            raise PermissionError(f"Cannot rename to read-only path: {new_path}")

        # ── Fast-fail (unlocked, optimization only) ──
        # Avoids lock acquisition for the common "file not found" error case.
        # Not authoritative — re-checked under lock below.
        if not self.metadata.exists(old_path) and not self.metadata.is_implicit_directory(old_path):
            raise NexusFileNotFoundError(old_path)

        meta = self.metadata.get(old_path)
        is_directory = (
            meta and meta.mime_type == "inode/directory"
        ) or self.metadata.is_implicit_directory(old_path)

        # ── PRE-INTERCEPT (unlocked — hooks may be slow) ──
        from nexus.contracts.vfs_hooks import RenameHookContext

        _rename_ctx = RenameHookContext(
            old_path=old_path,
            new_path=new_path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            is_directory=bool(is_directory),
            metadata=meta,
        )
        self._dispatch.intercept_pre_rename(_rename_ctx)

        # ── VFS I/O Lock: exclusive write lock on BOTH paths ──
        # Sorted order = deadlock-free (like Linux i_rwsem on both inodes).
        _first, _second = sorted([old_path, new_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # ── Authoritative checks (under lock, TOCTOU-safe) ──
                is_implicit_dir = not self.metadata.exists(
                    old_path
                ) and self.metadata.is_implicit_directory(old_path)
                if not self.metadata.exists(old_path) and not is_implicit_dir:
                    raise NexusFileNotFoundError(old_path)

                meta = self.metadata.get(old_path)
                is_directory = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                # Check destination — use backend.file_exists() for PAS backends
                if self.metadata.exists(new_path):
                    if hasattr(new_route.backend, "file_exists"):
                        if new_route.backend.file_exists(new_route.backend_path):
                            raise FileExistsError(f"Destination path already exists: {new_path}")
                        # Stale metadata — file gone from backend, clean up
                        logger.warning(
                            "Cleaning up stale metadata for %s (file not in backend storage)",
                            new_path,
                        )
                        self.metadata.delete(new_path)
                    else:
                        raise FileExistsError(f"Destination path already exists: {new_path}")

                # ── Metadata rename (under lock) ──
                # Rename is a pure metadata operation — get/put/delete on
                # MetastoreABC primitives. Put-first for crash safety (#3062).
                from dataclasses import replace as _replace

                _old_meta = self.metadata.get(old_path)
                if _old_meta is not None:
                    # Single entry (file or explicit directory)
                    _new_meta = _replace(_old_meta, path=new_path)
                    self.metadata.put(_new_meta)
                    self.metadata.delete(old_path)
                elif not is_directory:
                    raise NexusFileNotFoundError(old_path)

                # Rename children (for directories — explicit or implicit)
                if is_directory:
                    _prefix = old_path.rstrip("/") + "/"
                    for child in self.metadata.list(_prefix, recursive=True):
                        _child_new = new_path + child.path[len(old_path) :]
                        _child_new_meta = _replace(child, path=_child_new)
                        self.metadata.put(_child_new_meta)
                        self.metadata.delete(child.path)
            finally:
                if _h2:
                    self._vfs_lock_manager.release(_h2)
                    from nexus.lib.lock_order import L1_VFS, mark_released

                    mark_released(L1_VFS)
        finally:
            self._vfs_lock_manager.release(_h1)
            from nexus.lib.lock_order import L1_VFS, mark_released

            mark_released(L1_VFS)

        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_RENAME,
                path=old_path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                new_path=new_path,
            )
        )

        # Issue #1682: ReBAC path update moved to post_rename hooks.

        # POST-INTERCEPT: post-rename hooks (Issue #900)
        await self._dispatch.intercept_post_rename(_rename_ctx)

        return {}

    # ------------------------------------------------------------------
    # sys_copy — Issue #3329 (Workstream 3: native copy/move)
    # ------------------------------------------------------------------

    @rpc_expose(description="Copy file with native backend support")
    async def sys_copy(
        self, src_path: str, dst_path: str, *, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """Copy a file from src_path to dst_path.

        Uses the optimal strategy based on backend capabilities:
        - **Same backend, path-addressed**: Backend-native server-side copy
          (S3 CopyObject / GCS rewrite). Zero client bandwidth.
        - **Same backend, CAS**: Metadata duplication — the content blob
          is already deduplicated, so no I/O is needed.
        - **Cross-backend**: Read from source, write to destination.
          Bounded by ``NEXUS_FS_MAX_INMEMORY_SIZE`` (1 GB).

        Args:
            src_path: Source virtual path.
            dst_path: Destination virtual path.
            context: Operation context for permission checks.

        Returns:
            Dict with path, size, etag of the new file.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist.
            FileExistsError: If destination path already exists.
            PermissionError: If source or destination is read-only.
            ValueError: If cross-backend copy exceeds size limit.
        """
        src_path = self._validate_path(src_path)
        dst_path = self._validate_path(dst_path)
        context = self._parse_context(context)

        zone_id, agent_id, is_admin = self._get_context_identity(context)

        # Route both paths
        src_route = self.router.route(src_path, is_admin=is_admin)
        dst_route = self.router.route(dst_path, is_admin=is_admin, check_write=True)

        if dst_route.readonly:
            raise PermissionError(f"Cannot copy to read-only path: {dst_path}")

        # Fast-fail (unlocked — re-checked under lock)
        if not self.metadata.exists(src_path) and not self.metadata.is_implicit_directory(src_path):
            raise NexusFileNotFoundError(src_path)

        src_meta = self.metadata.get(src_path)
        if src_meta is None:
            raise NexusFileNotFoundError(src_path)
        if src_meta.mime_type == "inode/directory":
            raise IsADirectoryError(f"Cannot copy a directory: {src_path}")

        # PRE-INTERCEPT (unlocked — hooks may be slow)
        from nexus.contracts.vfs_hooks import CopyHookContext

        _copy_ctx = CopyHookContext(
            src_path=src_path,
            dst_path=dst_path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            metadata=src_meta,
        )
        self._dispatch.intercept_pre_copy(_copy_ctx)

        # VFS I/O Lock: exclusive write on dst, shared read on src
        _first, _second = sorted([src_path, dst_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # Authoritative checks under lock
                src_meta = self.metadata.get(src_path)
                if src_meta is None:
                    raise NexusFileNotFoundError(src_path)

                if self.metadata.exists(dst_path):
                    raise FileExistsError(f"Destination path already exists: {dst_path}")

                same_backend = src_route.backend is dst_route.backend

                if same_backend and hasattr(src_route.backend, "copy_file"):
                    # Path-addressing backend — native server-side copy
                    src_route.backend.copy_file(
                        src_route.backend_path,
                        dst_route.backend_path,
                        context=context,
                    )
                    # Get the destination blob's actual size/version
                    # (NOT the source's — versioned backends assign new IDs).
                    dst_size = src_route.backend._transport.get_size(
                        src_route.backend._get_key_path(dst_route.backend_path.strip("/")),
                    )
                    dst_version: str | None = None
                    if (
                        hasattr(src_route.backend, "versioning_enabled")
                        and src_route.backend.versioning_enabled
                    ):
                        _get_ver = getattr(
                            src_route.backend._transport, "get_version_id", None
                        ) or getattr(src_route.backend._transport, "get_generation", None)
                        if _get_ver:
                            dst_version = _get_ver(
                                src_route.backend._get_key_path(dst_route.backend_path.strip("/"))
                            )

                    from dataclasses import replace as _replace

                    dst_meta = _replace(
                        src_meta,
                        path=dst_path,
                        physical_path=dst_route.backend_path,
                        etag=dst_version or src_meta.etag,
                        size=dst_size,
                    )
                    self.metadata.put(dst_meta)
                    result = {
                        "path": dst_path,
                        "size": dst_size,
                        "etag": dst_version or dst_meta.etag,
                        "version": dst_meta.version,
                    }

                elif same_backend and not hasattr(src_route.backend, "copy_file"):
                    # CAS backend — metadata-only copy (content is deduplicated)
                    from dataclasses import replace as _replace

                    dst_meta = _replace(src_meta, path=dst_path)
                    self.metadata.put(dst_meta)
                    result = {
                        "path": dst_path,
                        "size": dst_meta.size or 0,
                        "etag": dst_meta.etag,
                        "version": dst_meta.version,
                    }

                else:
                    # Cross-backend copy
                    src_can_stream = hasattr(src_route.backend, "stream_file")
                    dst_can_stream = hasattr(dst_route.backend, "write_file_chunked")

                    if src_can_stream and dst_can_stream:
                        # Streaming copy — no size limit, ~8 MB memory
                        chunks = src_route.backend.stream_file(src_route.backend_path)
                        dst_route.backend.write_file_chunked(
                            dst_route.backend_path,
                            chunks,
                            content_type=src_meta.mime_type or "",
                        )
                        # Use source size (streaming doesn't return size);
                        # get destination version if the backend is versioned.
                        dst_version_id: str | None = None
                        if (
                            hasattr(dst_route.backend, "versioning_enabled")
                            and dst_route.backend.versioning_enabled
                        ):
                            _get_ver = getattr(
                                dst_route.backend._transport, "get_version_id", None
                            ) or getattr(dst_route.backend._transport, "get_generation", None)
                            if _get_ver:
                                dst_version_id = _get_ver(
                                    dst_route.backend._get_key_path(
                                        dst_route.backend_path.strip("/")
                                    )
                                )

                        from dataclasses import replace as _replace

                        dst_meta = _replace(
                            src_meta,
                            path=dst_path,
                            physical_path=dst_route.backend_path,
                            etag=dst_version_id or src_meta.etag,
                        )
                        self.metadata.put(dst_meta)
                        result = {
                            "path": dst_path,
                            "size": dst_meta.size or 0,
                            "etag": dst_version_id or dst_meta.etag,
                            "version": dst_meta.version,
                        }
                    else:
                        # Fallback — read from backend directly (we already
                        # hold VFS locks, so must NOT call sys_read/write
                        # which would try to re-acquire locks → deadlock).
                        from nexus.contracts.constants import NEXUS_FS_MAX_INMEMORY_SIZE

                        src_size = src_meta.size or 0
                        if src_size > NEXUS_FS_MAX_INMEMORY_SIZE:
                            size_gb = src_size / (1024**3)
                            raise ValueError(
                                f"Cross-backend copy too large ({size_gb:.1f} GB > "
                                f"{NEXUS_FS_MAX_INMEMORY_SIZE / (1024**3):.0f} GB limit). "
                                f"Move the file to the same backend first."
                            )
                        # Build a context with backend_path for the source read
                        from dataclasses import replace as _ctx_replace

                        src_ctx = (
                            _ctx_replace(
                                context,
                                backend_path=src_route.backend_path,
                            )
                            if context
                            else context
                        )
                        content = src_route.backend.read_content(
                            src_meta.physical_path or src_route.backend_path,
                            context=src_ctx,
                        )
                        # Supply content_id=backend_path so path-addressed
                        # backends know where to write the blob.
                        write_result = dst_route.backend.write_content(
                            content,
                            content_id=dst_route.backend_path,
                            context=context,
                        )
                        from dataclasses import replace as _replace

                        dst_meta = _replace(
                            src_meta,
                            path=dst_path,
                            physical_path=write_result.content_id or dst_route.backend_path,
                        )
                        self.metadata.put(dst_meta)
                        result = {
                            "path": dst_path,
                            "size": dst_meta.size or 0,
                            "etag": write_result.content_id or dst_meta.etag,
                            "version": dst_meta.version,
                        }

            finally:
                if _h2:
                    self._vfs_lock_manager.release(_h2)
                    from nexus.lib.lock_order import L1_VFS, mark_released

                    mark_released(L1_VFS)
        finally:
            self._vfs_lock_manager.release(_h1)
            from nexus.lib.lock_order import L1_VFS, mark_released

            mark_released(L1_VFS)

        # Lock released — event dispatch + side effects
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_COPY,
                path=src_path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                new_path=dst_path,
                size=result.get("size"),
                etag=result.get("etag"),
            )
        )

        await self._dispatch.intercept_post_copy(_copy_ctx)

        return result

    @rpc_expose(description="Get file metadata without reading content")
    def stat(self, path: str, context: OperationContext | None = None) -> dict[str, Any]:
        """
        Get file metadata without reading the file content.

        This is useful for getting file size before streaming, or checking
        file properties without the overhead of reading large files.

        Args:
            path: Virtual path to stat
            context: Optional operation context for permission checks

        Returns:
            Dict with file metadata:
                - size: File size in bytes
                - etag: Content hash
                - version: Version number
                - modified_at: Last modification timestamp
                - is_directory: Whether path is a directory

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            AccessDeniedError: If access is denied
            PermissionError: If user doesn't have read permission

        Example:
            >>> info = nx.stat("/workspace/large_file.bin")
            >>> print(f"File size: {info['size']} bytes")
        """
        path = self._validate_path(path)

        # Check if it's an implicit directory first (for permission check optimization)
        is_implicit_dir = self.metadata.is_implicit_directory(path)

        # Issue #1815: permission check via KernelDispatch INTERCEPT hook.
        ctx = self._resolve_cred(context)
        if is_implicit_dir:
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._dispatch.intercept_pre_stat(
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE",
                        extra={"is_implicit_directory": True},
                    )
                )
            except PermissionDeniedError:
                raise PermissionError(
                    f"Access denied: User '{ctx.user_id}' does not have TRAVERSE "
                    f"permission for '{path}'"
                ) from None
        else:
            from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

            self._dispatch.intercept_pre_read(_RHC(path=path, context=context))

        # Return directory info for implicit directories
        if is_implicit_dir:
            return {
                "size": 0,
                "etag": None,
                "version": None,
                "modified_at": None,
                "is_directory": True,
            }

        # Get file metadata
        meta = self.metadata.get(path)
        if meta is None:
            raise NexusFileNotFoundError(path)

        # Get size from backend if not in metadata
        size = meta.size
        if size is None and meta.etag:
            # Try to get size from backend
            zone_id, agent_id, is_admin = self._get_context_identity(context)
            route = self.router.route(
                path,
                is_admin=is_admin,
                check_write=False,
            )
            try:
                # Add backend_path to context for path-based connectors
                size_context = context
                if context:
                    from dataclasses import replace

                    size_context = replace(context, backend_path=route.backend_path)
                size = route.backend.get_content_size(meta.etag, context=size_context)
            except Exception as exc:
                logger.debug("Failed to get content size for %s: %s", path, exc)
                size = None

        # Convert datetime to ISO string for wire compatibility with Rust FUSE client
        # The client expects a plain string, not the wrapped {"__type__": "datetime", ...} format
        modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None

        return {
            "size": size,
            "etag": meta.etag,
            "version": meta.version,
            "modified_at": modified_at_str,
            "is_directory": False,
        }

    @rpc_expose(description="Get metadata for multiple files in bulk")
    def stat_bulk(
        self,
        paths: list[str],
        context: OperationContext | None = None,
        skip_errors: bool = True,
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple files in a single RPC call.

        This is optimized for bulk operations where many file stats are needed.
        It batches permission checks and metadata lookups for better performance.

        Args:
            paths: List of virtual paths to stat
            context: Optional operation context for permission checks
            skip_errors: If True, skip files that can't be stat'd and return None.
                        If False, raise exception on first error.

        Returns:
            Dict mapping path -> stat dict (or None if skip_errors=True and stat failed)
            Each stat dict contains: size, etag, version, modified_at, is_directory

        Performance:
            - Single RPC call instead of N calls
            - Batch permission checks (one DB query instead of N)
            - Batch metadata lookups
            - Expected speedup: 10-50x for 100+ files
        """
        import time

        bulk_start = time.time()
        results: dict[str, dict[str, Any] | None] = {}

        # Validate all paths
        validated_paths = []
        for path in paths:
            try:
                validated_path = self._validate_path(path)
                validated_paths.append(validated_path)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_bulk for %s: %s", path, exc)
                if skip_errors:
                    results[path] = None
                    continue
                raise

        if not validated_paths:
            return results

        # Batch permission check via KernelDispatch INTERCEPT hook.
        perm_start = time.time()
        allowed_set: set[str]
        try:
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.types import OperationContext
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            ctx = self._resolve_cred(context)
            assert isinstance(ctx, OperationContext), "Context must be OperationContext"
            allowed: list[str] = []
            for p in validated_paths:
                try:
                    self._dispatch.intercept_pre_stat(_SHC(path=p, context=ctx, permission="READ"))
                    allowed.append(p)
                except PermissionDeniedError:
                    pass
            allowed_set = set(allowed)
        except Exception as e:
            logger.error("[STAT-BULK] Permission check failed: %s", e)
            if not skip_errors:
                raise
            allowed_set = set()

        perm_elapsed = time.time() - perm_start
        logger.info(
            f"[STAT-BULK] Permission check: {len(allowed_set)}/{len(validated_paths)} allowed in {perm_elapsed * 1000:.1f}ms"
        )

        # Mark denied files
        for path in validated_paths:
            if path not in allowed_set:
                results[path] = None

        # Batch metadata lookup - single SQL query for all paths
        meta_start = time.time()

        # Batch fetch metadata for all files in single query
        # Note: We assume paths are files (not implicit directories) since stat_bulk
        # is typically called on paths returned by list(). If a path isn't found,
        # we check if it's an implicit directory as a fallback.
        try:
            batch_meta = self.metadata.get_batch(list(allowed_set))
            for path, meta in batch_meta.items():
                if meta is None:
                    # Path not found in metadata - check if it's an implicit directory
                    if self.metadata.is_implicit_directory(path):
                        results[path] = {
                            "size": 0,
                            "etag": None,
                            "version": None,
                            "modified_at": None,
                            "is_directory": True,
                        }
                    elif skip_errors:
                        results[path] = None
                    else:
                        raise NexusFileNotFoundError(path)
                else:
                    modified_at_str = meta.modified_at.isoformat() if meta.modified_at else None
                    results[path] = {
                        "size": meta.size,
                        "etag": meta.etag,
                        "version": meta.version,
                        "modified_at": modified_at_str,
                        "is_directory": False,
                    }
        except NexusFileNotFoundError:
            raise
        except Exception as e:
            logger.warning("[STAT-BULK] Batch metadata failed: %s: %s", type(e).__name__, e)
            if not skip_errors:
                raise

        meta_elapsed = time.time() - meta_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[STAT-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, meta={meta_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Check if file exists")
    async def access(self, path: str, *, context: OperationContext | None = None) -> bool:
        """Tier 2: check if path explicitly exists and is accessible.

        Returns True if path has explicit metadata or is an implicit directory,
        False otherwise. Unlike sys_stat, does NOT synthesize directory entries.
        """
        try:
            path = self._validate_path(path)
            ctx = self._resolve_cred(context)

            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Permission check via stat hook (same as _check_is_directory)
            from nexus.contracts.exceptions import PermissionDeniedError
            from nexus.contracts.vfs_hooks import StatHookContext as _SHC

            try:
                self._dispatch.intercept_pre_stat(
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    )
                )
            except PermissionDeniedError:
                return False

            if self.metadata.exists(path):
                return True
            return is_implicit_dir
        except Exception:
            return False

    @rpc_expose(description="Check existence of multiple paths in single call")
    async def exists_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, bool]:
        """
        Check existence of multiple paths in a single call (Issue #859).

        This reduces network round trips when checking many paths at once.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its existence status (True/False)

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Each path is checked independently (errors don't affect others)

        Examples:
            >>> results = nx.exists_batch(["/file1.txt", "/file2.txt", "/missing.txt"])
            >>> print(results)
            {"/file1.txt": True, "/file2.txt": True, "/missing.txt": False}
        """
        results: dict[str, bool] = {}
        for path in paths:
            try:
                results[path] = await self.access(path, context=context)
            except Exception as exc:
                # Any error means file doesn't exist or isn't accessible
                logger.debug("Exists check failed for %s: %s", path, exc)
                results[path] = False
        return results

    @rpc_expose(description="Get metadata for multiple paths in single call")
    async def metadata_batch(
        self, paths: list[str], context: OperationContext | None = None
    ) -> dict[str, dict[str, Any] | None]:
        """
        Get metadata for multiple paths in a single call (Issue #859).

        This reduces network round trips when fetching metadata for many files.
        Processing 10 paths requires 1 round trip instead of 10.

        Args:
            paths: List of virtual paths to get metadata for
            context: Operation context for permission checks (uses default if None)

        Returns:
            Dictionary mapping each path to its metadata dict or None if not found.
            Metadata includes: path, size, etag, mime_type, created_at, modified_at,
            version, zone_id, is_directory.

        Performance:
            - Single RPC call instead of N calls
            - 10x fewer round trips for multi-path operations
            - Leverages batch metadata fetch from database

        Examples:
            >>> results = nx.metadata_batch(["/file1.txt", "/missing.txt"])
            >>> print(results["/file1.txt"]["size"])
            1024
            >>> print(results["/missing.txt"])
            None
        """
        results: dict[str, dict[str, Any] | None] = {}

        # Validate paths and collect valid ones
        valid_paths: list[str] = []
        for path in paths:
            try:
                validated = self._validate_path(path)
                valid_paths.append(validated)
            except Exception as exc:
                logger.debug("Path validation failed in metadata_batch for %s: %s", path, exc)
                results[path] = None

        # Batch fetch metadata from database
        if valid_paths and hasattr(self.metadata, "get_batch"):
            batch_metadata = self.metadata.get_batch(valid_paths)
        else:
            # Fallback to individual fetches if get_batch not available
            batch_metadata = {p: self.metadata.get(p) for p in valid_paths}

        # Process results with permission checks
        for path in valid_paths:
            try:
                meta = batch_metadata.get(path)

                if meta is None:
                    results[path] = None
                    continue

                # Permission check via KernelDispatch INTERCEPT.
                from nexus.contracts.exceptions import PermissionDeniedError
                from nexus.contracts.vfs_hooks import StatHookContext as _SHC

                ctx = self._resolve_cred(context)
                try:
                    self._dispatch.intercept_pre_stat(
                        _SHC(path=path, context=ctx, permission="READ")
                    )
                except PermissionDeniedError:
                    results[path] = None
                    continue

                # Check if it's a directory
                is_dir = await self.is_directory(path, context=context)  # type: ignore[attr-defined]  # allowed

                results[path] = {
                    "path": meta.path,
                    "backend_name": meta.backend_name,
                    "physical_path": meta.physical_path,
                    "size": meta.size,
                    "etag": meta.etag,
                    "mime_type": meta.mime_type,
                    "created_at": meta.created_at,
                    "modified_at": meta.modified_at,
                    "version": meta.version,
                    "zone_id": meta.zone_id,
                    "is_directory": is_dir,
                }
            except Exception as exc:
                logger.debug("Failed to build metadata result for %s: %s", path, exc)
                results[path] = None

        return results

    @rpc_expose(description="Delete multiple files/directories")
    async def delete_batch(
        self,
        paths: list[str],
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Delete multiple files or directories in a single operation.

        Each path is processed independently - failures on one path don't affect others.
        Directories require recursive=True to delete non-empty directories.

        Args:
            paths: List of virtual paths to delete
            recursive: If True, delete non-empty directories (like rm -rf)
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each path to its result:
                {"success": True} or {"success": False, "error": "error message"}

        Example:
            >>> results = nx.delete_batch(['/a.txt', '/b.txt', '/folder'])
            >>> for path, result in results.items():
            ...     if result['success']:
            ...         print(f"Deleted {path}")
            ...     else:
            ...         print(f"Failed {path}: {result['error']}")
        """
        results = {}
        for path in paths:
            try:
                path = self._validate_path(path)
                meta = self.metadata.get(path)

                # Check for implicit directory (exists because it has files beneath it)
                is_implicit_dir = meta is None and self.metadata.is_implicit_directory(path)

                if meta is None and not is_implicit_dir:
                    results[path] = {"success": False, "error": "File not found"}
                    continue

                # Check if this is a directory (explicit or implicit)
                is_dir = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                if is_dir:
                    # Use rmdir for directories
                    self._rmdir_internal(
                        path, recursive=recursive, context=context, is_implicit=is_implicit_dir
                    )
                else:
                    # Use delete for files
                    await self.sys_unlink(path, context=context)

                results[path] = {"success": True}
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        return results

    def _rmdir_internal(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
        is_implicit: bool | None = None,
    ) -> None:
        """Internal rmdir implementation without RPC decoration.

        Args:
            path: Directory path to remove
            recursive: If True, delete non-empty directories
            context: Operation context for permission checks
            is_implicit: If True, directory is implicit (no metadata, exists due to child files).
                        If None, will be auto-detected.
        """
        import errno

        path = self._validate_path(path)
        zone_id, agent_id, is_admin = self._get_context_identity(context)

        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=True,
        )

        if route.readonly:
            raise PermissionError(f"Cannot remove read-only directory: {path}")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._dispatch.intercept_pre_write(_WHC(path=path, content=b"", context=context))

        # Check if path exists (explicit or implicit)
        meta = self.metadata.get(path)
        if is_implicit is None:
            is_implicit = meta is None and self.metadata.is_implicit_directory(path)

        if meta is None and not is_implicit:
            raise NexusFileNotFoundError(path)

        # Check if it's a directory (skip for implicit dirs - they're always directories)
        if meta is not None and meta.mime_type != "inode/directory":
            raise OSError(errno.ENOTDIR, "Not a directory", path)

        # Get files in directory
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir and not recursive:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", path)

        if recursive and files_in_dir:
            # Issue #1320/#1772: Content cleanup deferred to CAS reachability
            # GC. Kernel only deletes metadata.

            # Batch delete from metadata store
            file_paths = [file_meta.path for file_meta in files_in_dir]
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Delete the directory metadata (only if explicit directory)
        if not is_implicit:
            self.metadata.delete(path)

    @rpc_expose(description="Rename/move multiple files")
    async def rename_batch(
        self,
        renames: list[tuple[str, str]],
        context: OperationContext | None = None,
    ) -> dict[str, dict]:
        """
        Rename/move multiple files in a single operation.

        Each rename is processed independently - failures on one don't affect others.
        This is a metadata-only operation (instant, regardless of file size).

        Args:
            renames: List of (old_path, new_path) tuples
            context: Optional operation context for permission checks

        Returns:
            Dictionary mapping each old_path to its result:
                {"success": True, "new_path": "..."} or {"success": False, "error": "..."}

        Example:
            >>> results = nx.rename_batch([
            ...     ('/old1.txt', '/new1.txt'),
            ...     ('/old2.txt', '/new2.txt'),
            ... ])
            >>> for old_path, result in results.items():
            ...     if result['success']:
            ...         print(f"Renamed {old_path} -> {result['new_path']}")
        """
        results = {}
        for old_path, new_path in renames:
            try:
                await self.sys_rename(old_path, new_path, context=context)
                results[old_path] = {"success": True, "new_path": new_path}
            except Exception as e:
                results[old_path] = {"success": False, "error": str(e)}

        return results

    def register_observe(self, observer: Any) -> None:
        """Register a mutation observer (OBSERVE phase, Issue #900)."""
        self._dispatch.register_observe(observer)

    # ------------------------------------------------------------------
    # Abstract method forwarders (ABCMeta requires real definitions)
    # These satisfy the NexusFilesystemABC while delegating to services.
    # ------------------------------------------------------------------

    # --- Workspace Versioning (→ _workspace_rpc_service) ---

    def workspace_snapshot(
        self,
        workspace_path: str | None = None,
        description: str | None = None,
        tags: builtins.list[str] | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_snapshot(
            workspace_path=workspace_path,
            description=description,
            tags=tags,
        )

    def workspace_restore(
        self,
        snapshot_number: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_restore(
            snapshot_number=snapshot_number,
            workspace_path=workspace_path,
        )

    def workspace_log(
        self,
        workspace_path: str | None = None,
        limit: int = 100,
    ) -> builtins.list[dict[str, Any]]:
        return self._workspace_rpc_service.workspace_log(
            workspace_path=workspace_path,
            limit=limit,
        )

    def workspace_diff(
        self,
        snapshot_1: int,
        snapshot_2: int,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.workspace_diff(
            snapshot_1=snapshot_1,
            snapshot_2=snapshot_2,
            workspace_path=workspace_path,
        )

    # --- Workspace Registry (→ _workspace_rpc_service) ---

    def register_workspace(
        self,
        path: str,
        name: str | None = None,
        description: str | None = None,
        created_by: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
        ttl: Any | None = None,
    ) -> dict[str, Any]:
        return self._workspace_rpc_service.register_workspace(
            path=path,
            name=name,
            description=description,
            created_by=created_by,
            tags=tags,
            metadata=metadata,
            session_id=session_id,
            ttl=ttl,
        )

    def unregister_workspace(self, path: str) -> bool:
        return self._workspace_rpc_service.unregister_workspace(path=path)

    def list_workspaces(self, context: Any | None = None) -> builtins.list[dict]:
        return self._workspace_rpc_service.list_workspaces(context=context)

    def get_workspace_info(self, path: str) -> dict | None:
        return self._workspace_rpc_service.get_workspace_info(path=path)

    # --- Sandbox Operations (→ _sandbox_rpc_service) ---

    def sandbox_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = "e2b",
        template_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_create(
            name=name,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            context=context,
        )

    def sandbox_get_or_create(
        self,
        name: str,
        ttl_minutes: int = 10,
        provider: str | None = None,
        template_id: str | None = None,
        verify_status: bool = True,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_get_or_create(
            name=name,
            ttl_minutes=ttl_minutes,
            provider=provider,
            template_id=template_id,
            verify_status=verify_status,
            context=context,
        )

    def sandbox_run(
        self,
        sandbox_id: str,
        language: str,
        code: str,
        timeout: int = 300,
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        context: dict | None = None,
        as_script: bool = False,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_run(
            sandbox_id=sandbox_id,
            language=language,
            code=code,
            timeout=timeout,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            context=context,
            as_script=as_script,
        )

    def sandbox_pause(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_pause(sandbox_id=sandbox_id, context=context)

    def sandbox_resume(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_resume(sandbox_id=sandbox_id, context=context)

    def sandbox_stop(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_stop(sandbox_id=sandbox_id, context=context)

    def sandbox_list(
        self,
        context: dict | None = None,
        verify_status: bool = False,
        user_id: str | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_list(
            context=context,
            verify_status=verify_status,
            user_id=user_id,
            zone_id=zone_id,
            agent_id=agent_id,
            status=status,
        )

    def sandbox_status(self, sandbox_id: str, context: dict | None = None) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_status(sandbox_id=sandbox_id, context=context)

    def sandbox_connect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        mount_path: str = "/mnt/nexus",
        nexus_url: str | None = None,
        nexus_api_key: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_connect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            mount_path=mount_path,
            nexus_url=nexus_url,
            nexus_api_key=nexus_api_key,
            agent_id=agent_id,
            context=context,
        )

    def sandbox_disconnect(
        self,
        sandbox_id: str,
        provider: str = "e2b",
        sandbox_api_key: str | None = None,
        context: dict | None = None,
    ) -> dict[Any, Any]:
        return self._sandbox_rpc_service.sandbox_disconnect(
            sandbox_id=sandbox_id,
            provider=provider,
            sandbox_api_key=sandbox_api_key,
            context=context,
        )

    # --- Mount Operations (→ mount_service sync accessors) ---

    def add_mount(
        self,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        readonly: bool = False,
        io_profile: str = "balanced",
        context: Any = None,
    ) -> str:
        return self.mount_service.add_mount_sync(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            readonly=readonly,
            io_profile=io_profile,
            context=context,
        )

    def remove_mount(self, mount_point: str, context: Any = None) -> dict[str, Any]:
        return self.mount_service.remove_mount_sync(mount_point=mount_point, context=context)

    def list_mounts(self, context: Any = None) -> builtins.list[dict[str, Any]]:
        return self.mount_service.list_mounts_sync(context=context)

    def get_mount(self, mount_point: str, context: Any = None) -> dict[str, Any] | None:
        return self.mount_service.get_mount_sync(mount_point=mount_point, context=context)

    def _matches_patterns(
        self,
        file_path: str,
        include_patterns: builtins.list[str] | None = None,
        exclude_patterns: builtins.list[str] | None = None,
    ) -> bool:
        """Check if file path matches include/exclude patterns."""
        import fnmatch as _fnmatch

        # Check include patterns
        if include_patterns and not any(_fnmatch.fnmatch(file_path, p) for p in include_patterns):
            return False

        # Check exclude patterns
        return not (
            exclude_patterns and any(_fnmatch.fnmatch(file_path, p) for p in exclude_patterns)
        )

    # --- Search (sys_readdir/glob/grep) ---

    def _entry_to_detail_dict(self, entry: FileMetadata, recursive: bool) -> dict[str, Any]:
        """Convert a FileMetadata entry to a detail dict for sys_readdir.

        Promotes entry_type=0 (DT_REG) to 1 (DT_DIR) for implicit directories
        in non-recursive listings, matching ls -l semantics.
        """
        return {
            "path": entry.path,
            "size": entry.size,
            "etag": entry.etag,
            "entry_type": 1
            if (
                not recursive
                and entry.entry_type == 0
                and self.metadata.is_implicit_directory(entry.path)
            )
            else entry.entry_type,
            "zone_id": entry.zone_id,
            "owner_id": entry.owner_id,
            "modified_at": entry.modified_at.isoformat() if entry.modified_at else None,
            "version": entry.version,
        }

    # Issue #3388: Internal metastore prefixes that must not appear in
    # user-facing directory listings (search checkpoints, ReBAC namespaces).
    # These are bare keys (no leading "/") — user paths always start with "/".
    _INTERNAL_PATH_PREFIXES = ("cfg:", "ns:")

    @staticmethod
    def _is_internal_path(path: str) -> bool:
        """Return True for system-internal metastore paths (bare keys)."""
        return path.startswith(NexusFS._INTERNAL_PATH_PREFIXES)

    @rpc_expose(description="List directory entries")
    async def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        *,
        context: OperationContext | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any:
        prefix = path if path != "/" else ""
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

        if limit is not None:
            from nexus.core.pagination import paginate_iter

            items_iter = (
                e
                for e in self.metadata.list_iter(prefix=prefix, recursive=recursive)
                if not self._is_internal_path(e.path)
            )
            result = paginate_iter(items_iter, limit=limit, cursor_path=cursor)
            if details:
                result.items = [self._entry_to_detail_dict(e, recursive) for e in result.items]
            else:
                result.items = [e.path for e in result.items]
            return result

        entries = self.metadata.list(prefix=prefix, recursive=recursive)
        entries = [e for e in entries if not self._is_internal_path(e.path)]
        if details:
            return [self._entry_to_detail_dict(e, recursive) for e in entries]
        return [e.path for e in entries]

    # _run_async: replaced by direct run_sync() calls (Issue #1381)

    @rpc_expose(description="Backfill sparse directory index for fast listings", admin_only=True)
    def backfill_directory_index(
        self,
        prefix: str = "/",
        zone_id: str | None = None,
        _context: Any = None,  # noqa: ARG002 - RPC interface requires context param
    ) -> dict[str, Any]:
        """Backfill sparse directory index from existing files.

        Use this to populate the index for directories that existed before
        the sparse index feature was added. This improves list() performance
        from O(n) LIKE queries to O(1) index lookups.

        Args:
            prefix: Path prefix to backfill (default: "/" for all)
            zone_id: Zone ID to backfill (None for all zones)
            _context: Operation context (admin required, enforced by @rpc_expose)

        Returns:
            Dict with entries_created count
        """
        created = self.metadata.backfill_directory_index(prefix=prefix, zone_id=zone_id)
        return {"entries_created": created, "prefix": prefix}

    @rpc_expose(description="Flush pending write observer events to DB", admin_only=True)
    def flush_write_observer(
        self,
        _context: Any = None,  # noqa: ARG002 - RPC interface requires context param
    ) -> dict[str, Any]:
        """Flush the async write observer so pending version/audit records are committed.

        The PipedRecordStoreWriteObserver enqueues events asynchronously via
        DT_PIPE.  This method drains the pipe and commits all pending events,
        guaranteeing that subsequent queries (e.g. list_versions) see the data.

        No-op when the synchronous RecordStoreWriteObserver is in use.

        Returns:
            Dict with ``flushed`` count.
        """
        # Issue #1801: use service registry to find write_observer — no closure needed.
        _wo = self.service("write_observer")
        if _wo is None or not hasattr(_wo, "flush"):
            return {"flushed": 0}
        from nexus.lib.sync_bridge import run_sync

        flushed: int = run_sync(_wo.flush())
        return {"flushed": flushed}

    # ------------------------------------------------------------------
    # DT_PIPE kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _pipe_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_PIPE — async blocking, waits until data is available.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            data = await self._pipe_manager.pipe_read(path, blocking=True)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
        if offset or count is not None:
            data = data[offset : offset + count] if count is not None else data[offset:]
        return data

    def _pipe_write(self, path: str, data: bytes) -> int:
        """Write to DT_PIPE — non-blocking, PipeFullError propagates.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeClosedError, PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            return self._pipe_manager.pipe_write_nowait(path, data)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        except PipeClosedError:
            raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None

    def _pipe_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_PIPE — close buffer + delete inode.

        Only handles local pipes. Remote pipes are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.pipe import PipeNotFoundError

        if self._pipe_manager is None:
            raise NexusFileNotFoundError(path, "PipeManager not available")

        try:
            self._pipe_manager.destroy(path)
        except PipeNotFoundError:
            raise NexusFileNotFoundError(path, f"Pipe not found: {path}") from None
        return {}

    # ------------------------------------------------------------------
    # DT_STREAM kernel primitives (§4.2)
    # ------------------------------------------------------------------

    async def _stream_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_STREAM — async blocking, waits until data at offset is available.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamClosedError, StreamNotFoundError

        try:
            if count is not None and count > 1:
                items, _ = await self._stream_manager.stream_read_batch_blocking(
                    path, offset, count, blocking=True
                )
                return b"".join(items)
            data, _ = await self._stream_manager.stream_read(path, offset, blocking=True)
            return data
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        except StreamClosedError:
            raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

    def _stream_write(self, path: str, data: bytes) -> int:
        """Write to DT_STREAM — non-blocking append, returns byte offset.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamClosedError, StreamNotFoundError

        try:
            return self._stream_manager.stream_write_nowait(path, data)
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        except StreamClosedError:
            raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

    def _stream_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_STREAM — close buffer + delete inode.

        Only handles local streams. Remote streams are intercepted by
        FederationIPCResolver in the PRE-DISPATCH phase.
        """
        from nexus.core.stream import StreamNotFoundError

        try:
            self._stream_manager.destroy(path)
        except StreamNotFoundError:
            raise NexusFileNotFoundError(path, f"Stream not found: {path}") from None
        return {}

    async def aclose(self) -> None:
        """Async shutdown: stop PersistentService + unregister hooks, then close.

        Preferred over close() when an event loop is available.
        Calls coordinator lifecycle methods first (async), then
        delegates to close() for sync resource cleanup.
        """
        # Issue #3391: drain deferred OBSERVE background tasks before tearing down.
        await self._dispatch.shutdown()

        coord = self.service_coordinator
        if coord is not None:
            await coord.stop_persistent_services()
            coord._unregister_all_hooks()
        self.close()

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Issue #1793/#1789/#1792: Service close via factory-registered callbacks.
        # Runs BEFORE pipe/IPC close so callbacks can drain pipe buffers
        # (Issue #3399: piped write observer needs to flush before buffers clear).
        for _close_cb in self._close_callbacks:
            try:
                _close_cb()
            except Exception as exc:
                logger.debug("close: callback failed (best-effort): %s", exc)

        # Close IPC primitives — kernel-internal (§4.2)
        if hasattr(self, "_pipe_manager"):
            self._pipe_manager.close_all()
        if hasattr(self, "_stream_manager"):
            self._stream_manager.close_all()
        # Close peer channel pool (persistent gRPC channels)
        if hasattr(self, "_channel_pool") and self._channel_pool is not None:
            self._channel_pool.close_all()

        # Auto-close all enlisted services that have a close() method
        # (rebac_manager, audit_store, etc.). Reverse registration order.
        self._service_registry.close_all_services()

        # Close metadata store
        self.metadata.close()

        # Close record store (Services layer SQL connections)
        if self._record_store is not None:
            self._record_store.close()

        # Close TokenManager to release database connection
        if hasattr(self, "_token_manager") and self._token_manager is not None:
            self._token_manager.close()

        # Close mounted backends that hold resources (e.g., OAuth connectors with SQLite)
        if hasattr(self, "router"):
            from nexus.core.protocols.connector import OAuthCapableProtocol

            for mp in self.router.get_mount_points():
                try:
                    route = self.router.route(mp, is_admin=True)
                    if isinstance(route.backend, OAuthCapableProtocol):
                        route.backend.token_manager.close()
                except Exception as e:
                    logger.debug("Failed to close backend token manager: %s", e)

        # Close process-local runtime resources owned by this NexusFS.
        while self._runtime_closeables:
            resource = self._runtime_closeables.pop()
            close_fn = getattr(resource, "close", None)
            if not callable(close_fn):
                continue
            try:
                close_fn()
            except Exception as e:
                logger.debug("Failed to close runtime resource %s: %s", type(resource).__name__, e)
