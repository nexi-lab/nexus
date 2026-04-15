"""Unified filesystem implementation for Nexus."""
# Kernel interface unification — see KERNEL-ARCHITECTURE.md §4.5

import asyncio
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
from nexus.contracts.metadata import DT_DIR, FileMetadata
from nexus.contracts.types import OperationContext
from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)
from nexus.core.hash_fast import hash_content
from nexus.core.metastore import MetastoreABC
from nexus.core.nexus_fs_dispatch import DispatchMixin
from nexus.core.nexus_fs_lock import LockMixin
from nexus.core.nexus_fs_watch import WatchMixin
from nexus.core.path_utils import validate_path
from nexus.core.router import PathRouter
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
    DispatchMixin,
    LockMixin,
    WatchMixin,
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
        Backends are mounted externally via ``DriverLifecycleCoordinator.mount()``
        (which writes to MountTable) — like Linux VFS, no global backend.

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

        # Kernel zone identity — analogous to Linux sb->s_dev.
        # Standalone: always ROOT_ZONE_ID. Federation: set at link time.
        self._zone_id: str = ROOT_ZONE_ID

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

        # Mount table (kernel mount_hashtable) + path router (read-only query)
        from nexus.core.mount_table import MountTable

        if router is not None:
            self.router = router
            # Extract mount_table from the router (already constructed by factory)
            self._mount_table: MountTable = router._mount_table
        else:
            self._mount_table = MountTable(metadata_store)
            self.router = PathRouter(self._mount_table)

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
            vfs_lock_manager=self._vfs_lock_manager,
        )

        self._init_dispatch()

        import os as _os_ipc

        _ipc_self_addr = _os_ipc.environ.get("NEXUS_ADVERTISE_ADDR")

        self._transport_pool = None
        if _ipc_self_addr:
            from nexus.remote.rpc_transport import RPCTransportPool as _RPCTransportPool

            self._transport_pool = _RPCTransportPool()

        from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator

        self._driver_coordinator: DriverLifecycleCoordinator = DriverLifecycleCoordinator(
            self._mount_table,
            self,
            self_address=_ipc_self_addr,
            transport_pool=self._transport_pool,
        )

        # Custom backends for SHM/remote pipes/streams (non-standard, keep in Python)
        self._custom_pipe_backends: dict[str, Any] = {}
        self._custom_stream_backends: dict[str, Any] = {}

        logger.info(
            "IPC primitives initialized: DriverCoordinator (self_address=%s)",
            _ipc_self_addr or "none/single-node",
        )

        from nexus.core.service_registry import ServiceRegistry

        self._service_registry: ServiceRegistry = ServiceRegistry(dispatch=self)

        # ── Kernel (Issue #1817 — single-FFI sys_read/sys_write) ──
        # Optional: requires nexus_kernel Rust extension. Falls back to pure
        # Python path (slower but functional) when unavailable.
        from nexus._rust_compat import RUST_AVAILABLE

        self._kernel = None
        if RUST_AVAILABLE:
            try:
                from nexus.core.metastore import RustMetastoreProxy

                # Reuse kernel from RustMetastoreProxy (already has metastore wired)
                if isinstance(metadata_store, RustMetastoreProxy):
                    self._kernel = metadata_store._rust_kernel
                    metadata_store._kernel = self._kernel
                    self._mount_table.bind_kernel(self._kernel)
                    _vfs_rust = getattr(self._vfs_lock_manager, "_rust", None)
                    if _vfs_rust is not None:
                        self._kernel.set_vfs_lock(_vfs_rust)
                    # No set_metastore needed — RustMetastoreProxy already wired via set_metastore_path
                else:
                    from nexus_kernel import Kernel as _Kernel

                    self._kernel = _Kernel()
                    metadata_store._kernel = self._kernel
                    # PyMetastoreAdapter removed (Phase 9) — wire redb if available
                    # Note: set_metastore_path MUST happen BEFORE bind_kernel so that
                    # backfilled mounts inherit the metastore.
                    _redb_path = getattr(metadata_store, "_redb_path", None)
                    if _redb_path is not None:
                        self._kernel.set_metastore_path(str(_redb_path))
                    self._mount_table.bind_kernel(self._kernel)
                    _vfs_rust = getattr(self._vfs_lock_manager, "_rust", None)
                    if _vfs_rust is not None:
                        self._kernel.set_vfs_lock(_vfs_rust)
            except Exception as exc:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "Kernel init failed — falling back to Python path: %s", exc
                )
                self._kernel = None

        # ── Kernel-knows (sentinel None, injected by factory) ───────────
        # See KERNEL-ARCHITECTURE.md §1 DI patterns table.
        # None = graceful degrade (like Linux LSM: no module loaded = no check).

        self._event_bus: Any = None
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

    def link(
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

    def initialize(self) -> None:
        """Phase 2: One-time side effects — flag-only no-op.

        Actual initialization is done by factory._lifecycle._initialize_services(),
        called directly from create_nexus_fs(). This method exists for
        backward compatibility (tests, manual construction).
        """
        if self._initialized:
            return
        if not self._linked:
            self.link()
        self._initialized = True

    def bootstrap(self) -> None:
        """Phase 3: Start persistent services.  Server/Worker only.

        Auto-starts all PersistentService instances (ZoneLifecycleService,
        EventDeliveryWorker, DeferredPermissionBuffer, etc.) via
        ServiceRegistry.start_persistent_services().

        Idempotent — guarded by ``_bootstrapped`` flag.
        """
        if self._bootstrapped:
            return
        if not self._initialized:
            self.initialize()
        # Auto-lifecycle: start PersistentService instances (Issue #1580)
        coord = self.service_coordinator
        if coord is not None:
            coord.start_persistent_services()
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

    def swap_service(self, name: str, new_instance: Any, **kwargs: Any) -> None:
        """Hot-swap a service — all quadrants supported (#1452)."""
        self._service_registry.swap_service(name, new_instance, **kwargs)

    def _upgrade_lock_manager(self, lock_manager: Any) -> None:
        """Hot-swap LocalLockManager → RaftLockManager at link time.

        Kernel owns the hook point,
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

    def _build_rust_ctx(self, context: "OperationContext | None", is_admin: bool) -> object:
        """Build Rust OperationContext from Python context with all fields."""
        from nexus_kernel import OperationContext as _RustCtx

        return _RustCtx(
            user_id=context.user_id if context else "anonymous",
            zone_id=self._zone_id,  # routing zone (always set)
            is_admin=is_admin,
            agent_id=getattr(context, "agent_id", None) if context else None,
            is_system=getattr(context, "is_system", False) if context else False,
            groups=context.groups if context else [],
            admin_capabilities=list(context.admin_capabilities) if context else [],
            subject_type=getattr(context, "subject_type", "user") if context else "user",
            subject_id=getattr(context, "subject_id", None) if context else None,
            request_id=getattr(context, "request_id", "") if context else "",
            context_zone_id=context.zone_id if context else None,  # caller's zone
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

    # =========================================================================
    # Virtual .readme/ overlay helper (Issue #3728)
    # =========================================================================

    def _try_virtual_readme_stat(
        self,
        path: str,
        ctx: OperationContext,
    ) -> dict[str, Any] | None:
        """Return a stat dict for a virtual ``.readme/`` entry, or ``None``.

        Called from ``sys_stat`` when the metastore has no entry for the
        path — if the path routes to a skill backend and falls under
        ``.readme/``, synthesize a stat dict from the virtual tree.

        Returns ``None`` for any of: path doesn't route anywhere, backend
        has no skill docs, path isn't under ``.readme/``, or normalization
        fails.  The caller treats ``None`` as "really not found" and
        continues with the normal sys_stat miss path.
        """
        try:
            _, _, is_admin = self._get_context_identity(ctx)
            route = self.router.route(
                path, is_admin=is_admin, check_write=False, zone_id=self._zone_id
            )
        except Exception:
            return None

        backend = getattr(route, "backend", None)
        if backend is None:
            return None

        mount_point = getattr(route, "mount_point", "") or ""
        backend_path = getattr(route, "backend_path", "") or ""

        from nexus.backends.connectors.schema_generator import (
            _parse_readme_path_parts,
            _readme_dir_for,
            get_virtual_readme_tree_for_backend,
            overlay_owns_path,
        )

        # Round 5 finding #13: defer to real backend when it owns this
        # path, so ``sys_stat`` can't report virtual metadata for a file
        # that ``sys_read`` serves from real storage.  Malformed paths
        # propagate as None so sys_stat returns its normal miss result.
        try:
            _owns = overlay_owns_path(backend, mount_point, backend_path, context=ctx)
        except ValueError:
            return None
        if not _owns:
            return None

        try:
            parts = _parse_readme_path_parts(backend_path, readme_dir=_readme_dir_for(backend))
        except ValueError:
            return None  # malformed path — let sys_stat return None

        if parts is None:
            return None  # not under .readme/

        try:
            tree = get_virtual_readme_tree_for_backend(backend, mount_point)
        except Exception:
            return None

        entry = tree.find(parts)
        if entry is None:
            return None  # under .readme/ but not a known file

        backend_name = getattr(backend, "name", "") or ""
        return {
            "path": path,
            "backend_name": backend_name,
            "physical_path": "",
            "size": entry.size(),
            "etag": None,
            "mime_type": "inode/directory" if entry.is_dir else "text/markdown",
            "created_at": None,
            "modified_at": None,
            "is_directory": entry.is_dir,
            "entry_type": 1 if entry.is_dir else 0,
            "owner": ctx.user_id,
            "group": ctx.user_id,
            # Read-only — virtual files cannot be modified.
            "mode": 0o555 if entry.is_dir else 0o444,
            "version": 1,
            "zone_id": self._zone_id,
        }

    def _reject_if_virtual_readme(
        self,
        path: str,
        context: OperationContext | None,
        op: str = "write",
    ) -> None:
        """Raise ``PermissionError`` if ``path`` is under a virtual ``.readme/``.

        Issue #3728: the overlay advertises virtual files as mode 0o444
        (read-only), but the write/delete/rename/mkdir paths route on
        backend_path directly — without this guard, a caller writing to
        ``/<mount>/.readme/README.md`` would create a real file in the
        backend (e.g. a stray ``.readme/README.md`` in the user's Google
        Drive).  This helper blocks every mutating entry point at the
        kernel layer so the virtual tree cannot be mutated.

        Called from: ``_write_content``, ``sys_write``, ``write``,
        ``mkdir``, ``rmdir``, ``sys_unlink``, ``sys_rename``,
        ``write_batch``, ``delete_batch``, ``rename_batch``.
        """
        try:
            _, _, is_admin = self._get_context_identity(context)
            route = self.router.route(
                path, is_admin=is_admin, check_write=False, zone_id=self._zone_id
            )
        except Exception:
            return  # non-routable path — let the real call surface the error

        backend = getattr(route, "backend", None)
        if backend is None:
            return

        backend_path = getattr(route, "backend_path", "") or ""
        mount_point = getattr(route, "mount_point", "") or ""

        from nexus.backends.connectors.schema_generator import overlay_owns_path

        # Shared ownership check — only reject when the virtual overlay
        # is authoritative for this path.  On deferring backends
        # (native gdrive) with real data here, the mutation is a
        # legitimate operation against user data and passes through.
        # Malformed paths (traversal etc.) fall through so the real
        # call surfaces the underlying error with its own message.
        try:
            owns = overlay_owns_path(backend, mount_point, backend_path, context=context)
        except ValueError:
            return
        if not owns:
            return

        raise PermissionError(
            f"Cannot {op} virtual .readme/ path: {path} "
            f"(skill docs are read-only and generated from class metadata)"
        )

    def _try_virtual_readme_bytes(
        self,
        path: str,
        context: OperationContext | None,
    ) -> bytes | None:
        """Return the bytes for a virtual ``.readme/`` path, or ``None``.

        Synchronous sibling of ``_try_virtual_readme_stat`` used by
        ``read_bulk`` (which is sync and cannot ``await sys_read``) to
        serve virtual skill docs when the Rust fast path misses.
        """
        try:
            _, _, is_admin = self._get_context_identity(context)
            route = self.router.route(
                path, is_admin=is_admin, check_write=False, zone_id=self._zone_id
            )
        except Exception:
            return None

        backend = getattr(route, "backend", None)
        if backend is None:
            return None

        mount_point = getattr(route, "mount_point", "") or ""
        backend_path = getattr(route, "backend_path", "") or ""

        from nexus.backends.connectors.schema_generator import dispatch_virtual_readme_read

        try:
            return dispatch_virtual_readme_read(backend, mount_point, backend_path, context=context)
        except Exception:
            return None

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
        already exists (e.g. a DT_MOUNT entry written by ``MountTable.add()``).
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

    def _check_is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
        _meta: Any = _SENTINEL,
    ) -> bool:
        """Internal: check if path is a directory (explicit or implicit).

        §11 Phase 6: converted from async def → def. Body was already
        fully synchronous (no awaits). Used by sys_stat.

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
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    ),
                )
            except PermissionDeniedError:
                return False

            # Use pre-fetched meta if provided, otherwise fetch
            meta = self.metadata.get(path) if _meta is _SENTINEL else _meta
            if meta is not None and (meta.is_dir or meta.is_mount or meta.is_external_storage):
                return True

            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                is_admin=ctx.is_admin,
                check_write=False,
                zone_id=self._zone_id,
            )
            if route.backend.is_directory(route.backend_path):
                return True
            return is_implicit_dir
        except (InvalidPathError, NexusFileNotFoundError):
            return False

    @rpc_expose(description="Check if path is a directory")
    def is_directory(
        self,
        path: str,
        *,
        context: OperationContext | None = None,
    ) -> bool:
        """Tier 2: convenience wrapper — derives from sys_stat.

        Equivalent to ``(await sys_stat(path)).get("is_directory", False)``.
        """
        try:
            stat = self.sys_stat(path, context=context)
            return stat is not None and stat.get("is_directory", False)
        except (InvalidPathError, NexusFileNotFoundError):
            return False

    # Lock methods in nexus_fs_lock.py (LockMixin)
    # sys_watch is in nexus_fs_watch.py (WatchMixin)

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
            if not (meta.is_mount or meta.is_external_storage):
                continue
            top = meta.path.lstrip("/").split("/")[0]
            if not top:
                continue
            if meta.path in admin_only and not ctx.is_admin:
                continue
            names.add(top)
        return sorted(names)

    @rpc_expose(description="Get file metadata for FUSE operations")
    def sys_stat(
        self,
        path: str,
        *,
        include_lock: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr).

        When include_lock=True, appends a "lock" field with advisory lock
        state from _lock_manager (zero cost when False — default).
        """
        # ── Rust fast path (Phase H): dcache hit → dict from Rust ──────
        # Skipped when include_lock=True (needs Python _lock_manager).
        if not include_lock and self._kernel is not None:
            _is_admin = (
                getattr(context, "is_admin", False)
                if context is not None and not isinstance(context, dict)
                else (context.get("is_admin", False) if isinstance(context, dict) else False)
            )
            _stat = self._kernel.sys_stat(path, self._zone_id, _is_admin)
            if _stat is not None:
                # Rust returns dict without owner/group (context-dependent)
                ctx = self._resolve_cred(context)
                _stat["owner"] = ctx.user_id
                _stat["group"] = ctx.user_id
                return _stat

        ctx = self._resolve_cred(context)
        normalized = self._validate_path(path, allow_root=True)

        # Fetch metadata once, share with _check_is_directory to avoid duplicate lookup
        file_meta = self.metadata.get(normalized)

        # Check if it's a directory (pass pre-fetched meta to avoid second metadata.get)
        is_dir = self._check_is_directory(normalized, context=ctx, _meta=file_meta)

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
            # Virtual .readme/ overlay check (Issue #3728) — before giving
            # up, see if this is a synthetic file under a skill backend's
            # .readme/ directory.
            _vstat = self._try_virtual_readme_stat(normalized, ctx)
            if _vstat is not None:
                return _vstat
            return None

        result: dict[str, Any] = {
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

        # Optional lock enrichment (zero cost when include_lock=False)
        if include_lock:
            lock_info = self._lock_manager.get_lock_info(normalized)
            result["lock"] = self._format_lock_info(lock_info) if lock_info else None

        return result

    @staticmethod
    def _format_lock_info(info: Any) -> dict[str, Any]:
        """Format LockInfo for sys_stat(include_lock=True) response."""
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

    @rpc_expose(description="Upsert file metadata attributes")
    def sys_setattr(
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
            self._service_registry.enlist(
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
                # Idempotent open: re-create Rust buffer if lost after restart
                if not self._kernel.has_pipe(path):
                    self._kernel.create_pipe(path, attrs.get("capacity", 65_536))
                return {"path": path, "created": False, "entry_type": requested_type}
            if meta.entry_type == requested_type and requested_type == DT_STREAM:
                # Idempotent open: re-create Rust buffer if lost after restart
                if not self._kernel.has_stream(path):
                    self._kernel.create_stream(path, attrs.get("capacity", 65_536))
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

            io_profile = attrs.get("io_profile", "memory")
            try:
                if io_profile == "shared_memory":
                    from nexus.core.shm_pipe import SharedMemoryPipeBackend

                    pipe_backend, _shm_path, _data_rd_fd, _space_rd_fd = (
                        SharedMemoryPipeBackend.create(capacity)
                    )
                    self._custom_pipe_backends[path] = pipe_backend
                    # Rust kernel tracks the inode (metastore + dcache)
                    self._kernel.create_pipe(path, capacity)
                else:
                    # Standard memory pipe → Rust kernel IPC registry
                    self._kernel.create_pipe(path, capacity)
            except (PipeError, Exception) as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

        if entry_type == DT_STREAM:
            from nexus.core.stream import StreamError

            io_profile = attrs.get("io_profile", "memory")

            # Check if mount provides a custom stream backend factory
            # (e.g. CAS-backed or WAL-backed streams). Default: Rust kernel IPC stream.
            _mount_entry = self.router.get_mount_entry_for_path(path)
            _factory = _mount_entry.stream_backend_factory if _mount_entry else None

            try:
                if _factory is not None:
                    backend = _factory(path, capacity)
                    self._custom_stream_backends[path] = backend
                    self._kernel.create_stream(path, capacity)
                elif io_profile == "shared_memory":
                    from nexus.core.shm_stream import SharedMemoryStreamBackend

                    stream_backend, _shm_path, _data_rd_fd = SharedMemoryStreamBackend.create(
                        capacity
                    )
                    self._custom_stream_backends[path] = stream_backend
                    self._kernel.create_stream(path, capacity)
                else:
                    # Standard memory stream → Rust kernel IPC registry
                    self._kernel.create_stream(path, capacity)
            except (StreamError, Exception) as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

        if entry_type == DT_DIR:
            now = datetime.now(UTC)
            empty_hash = hash_content(b"")
            route = self.router.route(path, is_admin=True, zone_id=self._zone_id)
            metadata = FileMetadata(
                path=path,
                backend_name=self._driver_coordinator.backend_key(route.backend, route.mount_point),
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
            route.metastore.put(metadata)
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
                    root_route = self.router.route(
                        "/", is_admin=is_admin, check_write=False, zone_id=self._zone_id
                    )
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
                    zone_id=self._zone_id,
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

    # _backend_read deleted — Rust PyObjectStoreAdapter handles all backends
    # via OperationContext (Rust-constructed with backend_path from route).

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

    # _acquire_lock_sync / _release_lock_sync in nexus_fs_lock.py (LockMixin)

    # _acquire_lock_sync / _release_lock_sync in nexus_fs_lock.py (LockMixin)

    @rpc_expose(description="Read file content")
    def sys_read(
        self,
        path: str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read file content as bytes (POSIX pread(2)).

        Thin async wrapper around Rust Kernel.sys_read (pure Rust, zero GIL).
        DT_PIPE/DT_STREAM, resolve, and hooks are [TRANSITIONAL] — migrates
        to Rust dispatch middleware in PR 7.
        """
        # DT_PIPE: Rust IPC registry handles hot path.  Check custom backends
        # (SHM/remote) first; standard memory pipes are in Rust kernel.
        _custom_pbuf = self._custom_pipe_backends.get(path)
        if _custom_pbuf is not None:
            from nexus.core.pipe import PipeClosedError, PipeEmptyError

            try:
                data = _custom_pbuf.read_nowait()
            except PipeEmptyError:
                return self._pipe_read(path, count=count, offset=offset)
            except PipeClosedError:
                raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
            if offset or count is not None:
                data = data[offset : offset + count] if count is not None else data[offset:]
            return data

        # DT_STREAM: custom backends (SHM/factory-provided)
        _custom_sbuf = self._custom_stream_backends.get(path)
        if _custom_sbuf is not None:
            from nexus.core.stream import StreamClosedError, StreamEmptyError

            try:
                if count is not None and count > 1:
                    # [TRANSITIONAL] Sync: use sync read_batch for custom backends.
                    items, _ = _custom_sbuf.read_batch(offset, count)
                    return b"".join(items)
                data, _ = _custom_sbuf.read_at(offset)
                return data
            except StreamEmptyError:
                raise NexusFileNotFoundError(path, f"Stream empty at offset {offset}") from None
            except StreamClosedError:
                raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

        path = self._validate_path(path)
        context = self._parse_context(context)
        _handled, _resolve_hint = self.resolve_read(path, context=context)
        if _handled:
            content = _resolve_hint or b""
            if offset or count is not None:
                content = (
                    content[offset : offset + count] if count is not None else content[offset:]
                )
            return content

        # External connector mount read — only for ExternalRouteResult.
        _is_admin = (
            getattr(context, "is_admin", False)
            if context is not None and not isinstance(context, dict)
            else (context.get("is_admin", False) if isinstance(context, dict) else False)
        )
        from nexus.core.router import ExternalRouteResult

        _route = self.router.route(
            path, is_admin=_is_admin, check_write=False, zone_id=self._zone_id
        )
        _route_backend = getattr(_route, "backend", None)
        _route_backend_path = getattr(_route, "backend_path", "") or ""
        _route_mount_point = getattr(_route, "mount_point", "") or ""
        if isinstance(_route, ExternalRouteResult) and _route_backend is not None:
            _ctx = (
                _dc_replace(
                    context,
                    backend_path=_route_backend_path,
                    virtual_path=path,
                    mount_path=_route_mount_point,
                )
                if context
                else OperationContext(
                    user_id="anonymous",
                    groups=[],
                    backend_path=_route_backend_path,
                    virtual_path=path,
                    mount_path=_route_mount_point,
                )
            )
            try:
                # Virtual .readme/ overlay check (Issue #3728).  If the path
                # is under a skill backend's .readme/ directory, serve from
                # the generated tree instead of calling the real backend.
                from nexus.backends.connectors.schema_generator import (
                    dispatch_virtual_readme_read,
                )

                _virtual_data = dispatch_virtual_readme_read(
                    _route_backend,
                    _route_mount_point,
                    _route_backend_path,
                    context=_ctx,
                )
                if _virtual_data is not None:
                    data = _virtual_data
                else:
                    data = _route_backend.read_content(_route_backend_path, context=_ctx)
                if offset or count is not None:
                    data = data[offset : offset + count] if count is not None else data[offset:]
                return data
            except Exception:
                # If connector read fails, fall through to kernel
                if isinstance(_route, ExternalRouteResult):
                    raise  # External mounts should not fall through

        # PRE-INTERCEPT hooks dispatched by Rust sys_read (dispatch_pre_hooks)

        # ── KERNEL (Rust — pre-hooks + route + backend read) ──
        _rust_ctx = self._build_rust_ctx(context, _is_admin)
        result = self._kernel.sys_read(path, _rust_ctx)

        # DT_PIPE: Rust returns hit=true if data popped, hit=false if empty
        if result.entry_type == 3:  # DT_PIPE
            if result.hit:
                data = result.data or b""
                if offset or count is not None:
                    data = data[offset : offset + count] if count is not None else data[offset:]
                return data
            # Empty pipe — try nowait (hot path), then block in Rust (GIL-free)
            _data = self._kernel.pipe_read_nowait(path)
            if _data is not None:
                if offset or count is not None:
                    _data = _data[offset : offset + count] if count is not None else _data[offset:]
                return bytes(_data)
            _data = self._kernel.pipe_read_blocking(path, 5000)
            if offset or count is not None:
                _data = _data[offset : offset + count] if count is not None else _data[offset:]
            return bytes(_data)

        # DT_STREAM: blocking reads with offset tracking
        if result.entry_type == 4:  # DT_STREAM
            _result = self._kernel.stream_read_at(path, offset)
            if _result is not None:
                return bytes(_result[0])
            # Slow path — block in Rust (GIL-free)
            _data, _next = self._kernel.stream_read_at_blocking(path, offset, 30000)
            return bytes(_data)

        if not result.hit:
            raise NexusFileNotFoundError(path)
        data = result.data or b""

        if offset or count is not None:
            data = data[offset : offset + count] if count is not None else data[offset:]

        # POST-INTERCEPT: hooks dispatched via Rust dispatch_post_hooks
        if result.post_hook_needed:
            zone_id, agent_id, _ = self._get_context_identity(context)
            from nexus.contracts.vfs_hooks import ReadHookContext

            _read_ctx = ReadHookContext(
                path=path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                content=data,
                content_hash=result.content_hash,
            )
            self._kernel.dispatch_post_hooks("read", _read_ctx)
            data = _read_ctx.content or data

        return data

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

        bulk_start = time.time()
        results: dict[str, bytes | dict[str, Any] | None] = {}

        # Small-batch fast path: <=4 paths → sequential sys_read (no batch overhead).
        # Avoids permission-check batching, metadata batching, and logging for tiny requests.
        if len(paths) <= 4:
            zone_id, agent_id, is_admin = self._get_context_identity(context)
            _rust_ctx = self._build_rust_ctx(context, is_admin)
            for path in paths:
                try:
                    vpath = self._validate_path(path)
                    result = self._kernel.sys_read(vpath, _rust_ctx)
                    if not result.hit:
                        if skip_errors:
                            results[path] = None
                            continue
                        raise NexusFileNotFoundError(path)
                    content = result.data or b""
                    if return_metadata:
                        meta = self.metadata.get(vpath)
                        results[path] = {
                            "content": content,
                            "etag": meta.etag if meta else None,
                            "version": meta.version if meta else 0,
                            "modified_at": meta.modified_at if meta else None,
                            "size": len(content),
                        }
                    else:
                        results[path] = content
                except NexusFileNotFoundError:
                    if skip_errors:
                        results[path] = None
                    else:
                        raise
                except Exception as e:
                    logger.warning(
                        "[READ-BULK] Failed to read %s: %s: %s", path, type(e).__name__, e
                    )
                    if skip_errors:
                        results[path] = None
                    else:
                        raise
            return results

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
                    self._kernel.dispatch_pre_hooks(
                        "stat", _SHC(path=p, context=ctx, permission="READ")
                    )
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

        # Read allowed files via Rust kernel sys_read (single path per call).
        # Rust kernel handles: validate → route → dcache → metastore → backend read.
        read_start = time.time()
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        _rust_ctx = self._build_rust_ctx(context, is_admin)

        # Batch metadata lookup (needed for return_metadata=True)
        batch_meta: dict[str, FileMetadata | None] | None = None
        if return_metadata:
            meta_start = time.time()
            batch_meta = self.metadata.get_batch(list(allowed_set))
            meta_elapsed = (time.time() - meta_start) * 1000
            logger.info(
                f"[READ-BULK] Batch metadata lookup: {len(batch_meta)} paths in {meta_elapsed:.1f}ms"
            )

        for path in allowed_set:
            try:
                result = self._kernel.sys_read(path, _rust_ctx)
                content = None
                if result.hit:
                    content = result.data or b""
                else:
                    # Rust fast path missed.  Virtual ``.readme/`` paths
                    # (Issue #3728) are not in the metastore, so we
                    # route through the same dispatch helper that the
                    # async ``sys_read`` uses before declaring "not found".
                    content = self._try_virtual_readme_bytes(path, context)
                if content is None:
                    if skip_errors:
                        results[path] = None
                        continue
                    raise NexusFileNotFoundError(path)
                if return_metadata:
                    assert batch_meta is not None
                    meta = batch_meta.get(path)
                    results[path] = {
                        "content": content,
                        "etag": meta.etag if meta else None,
                        "version": meta.version if meta else 0,
                        "modified_at": meta.modified_at if meta else None,
                        "size": len(content),
                    }
                else:
                    results[path] = content
            except NexusFileNotFoundError:
                if skip_errors:
                    results[path] = None
                else:
                    raise
            except Exception as e:
                logger.warning("[READ-BULK] Failed to read %s: %s: %s", path, type(e).__name__, e)
                if skip_errors:
                    results[path] = None
                else:
                    raise

        read_elapsed = time.time() - read_start
        bulk_elapsed = time.time() - bulk_start

        logger.info(
            f"[READ-BULK] Completed: {len(results)} files in {bulk_elapsed * 1000:.1f}ms "
            f"(perm={perm_elapsed * 1000:.0f}ms, read={read_elapsed * 1000:.0f}ms)"
        )

        return results

    @rpc_expose(description="Read a byte range from a file")
    def read_range(
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
        _handled, _resolve_hint = self.resolve_read(path, context=context)
        if _handled:
            return (_resolve_hint or b"")[start:end]

        # Issue #3728 virtual ``.readme/`` overlay early-exit.
        # Virtual skill docs have no metastore rows by design, so the
        # meta-backed range path below would unconditionally raise
        # ``NexusFileNotFoundError`` for them.  Serve from the overlay
        # first and slice the returned bytes; fall through to the
        # normal range path for real files.
        #
        # Defensive: unit tests use stub filesystems that may not
        # subclass NexusFS and therefore lack ``_try_virtual_readme_bytes``.
        # Fall through silently in that case — the stub isn't serving
        # a skill backend anyway.
        _virtual_probe = getattr(self, "_try_virtual_readme_bytes", None)
        if callable(_virtual_probe):
            _virtual_bytes = _virtual_probe(path, context)
            if _virtual_bytes is not None:
                return _virtual_bytes[start:end]

        # OPTIMISED PATH: no post-read hooks + backend has read_content_range
        from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

        has_post_hooks = self.read_hook_count > 0

        if not has_post_hooks:
            self._kernel.dispatch_pre_hooks("read", _RHC(path=path, context=context))

            zone_id, agent_id, is_admin = self._get_context_identity(context)
            route = self.router.route(
                path, is_admin=is_admin, check_write=False, zone_id=self._zone_id
            )

            meta = route.metastore.get(path)

            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(path)

            _rb = self._driver_coordinator.resolve_backend(meta.backend_name)
            if hasattr(_rb, "read_content_range"):
                from dataclasses import replace as _replace

                read_context = (
                    _replace(
                        context,
                        backend_path=route.backend_path,
                        mount_path=route.mount_point,
                    )
                    if context
                    else None
                )
                return _rb.read_content_range(meta.etag, start, end, context=read_context)

        # FALLBACK: full read via sys_read + slice
        content = self.sys_read(path, count=end, offset=0, context=context)
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

        self._kernel.dispatch_pre_hooks("read", _RHC(path=path, context=context))

        # Route to backend with access control
        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
            zone_id=self._zone_id,
        )

        # Check if file exists in metadata
        meta = route.metastore.get(path)
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

        self._kernel.dispatch_pre_hooks("read", _RHC(path=path, context=context))

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        route = self.router.route(
            path,
            is_admin=is_admin,
            check_write=False,
            zone_id=self._zone_id,
        )

        meta = route.metastore.get(path)
        if meta is None or meta.etag is None:
            raise NexusFileNotFoundError(path)

        yield from route.backend.stream_range(
            meta.etag, start, end, chunk_size=chunk_size, context=context
        )

    @rpc_expose(description="Write file content from stream")
    def write_stream(
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
            zone_id=self._zone_id,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Virtual .readme/ paths are read-only (Issue #3728).
        self._reject_if_virtual_readme(path, context, op="write_stream")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._kernel.dispatch_pre_hooks("write", _WHC(path=path, content=b"", context=context))

        # Get existing metadata for version tracking
        now = datetime.now(UTC)
        meta = route.metastore.get(path)

        # Add backend_path to context for path-based connectors
        if context:
            context = _dc_replace(
                context,
                backend_path=route.backend_path,
                virtual_path=path,
                mount_path=route.mount_point,
            )
        else:
            context = OperationContext(
                user_id="anonymous",
                groups=[],
                backend_path=route.backend_path,
                virtual_path=path,
                mount_path=route.mount_point,
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
            backend_name=self._driver_coordinator.backend_key(route.backend, route.mount_point),
            physical_path=content_hash,  # CAS: hash is the "physical" location
            etag=content_hash,
            size=size,
            version=new_version,
            created_at=meta.created_at if meta else now,
            modified_at=now,
            zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
        )

        route.metastore.put(new_meta)

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
        self._kernel.dispatch_post_hooks("write", _ws_ctx)

        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now.isoformat(),
            "size": size,
        }

    @rpc_expose(description="Write file content")
    def sys_write(
        self,
        path: str,
        buf: bytes | str,
        *,
        count: int | None = None,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        """Write content to a file (POSIX write(2)).

        Thin async wrapper around Rust Kernel.sys_write (CAS I/O is pure Rust,
        zero GIL). Metastore.put stays in Python [TRANSITIONAL] — migrates to
        Rust metastore in PR 7.
        """
        # DT_PIPE: custom backends (SHM/remote) checked first
        _custom_pbuf = self._custom_pipe_backends.get(path)
        if _custom_pbuf is not None:
            n = _custom_pbuf.write_nowait(buf if isinstance(buf, bytes) else buf.encode("utf-8"))
            return {"path": path, "bytes_written": n}

        # DT_STREAM: custom backends (SHM/factory-provided)
        _custom_sbuf = self._custom_stream_backends.get(path)
        if _custom_sbuf is not None:
            if isinstance(buf, str):
                buf = buf.encode("utf-8")
            _off = _custom_sbuf.write_nowait(buf)
            return {"path": path, "bytes_written": len(buf), "offset": _off}

        # Normalize input
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        # [TRANSITIONAL] PRE-DISPATCH: resolve — migrates to Rust dispatch middleware in PR 7
        context = self._parse_context(context)

        # Virtual .readme/ paths are read-only (Issue #3728).
        self._reject_if_virtual_readme(path, context, op="sys_write")

        _handled, _result = self.resolve_write(path, buf)
        if _handled:
            base: dict[str, Any] = {"path": path, "bytes_written": len(buf)}
            if isinstance(_result, dict):
                base.update(_result)
            return base

        # IPC write: Rust kernel handles DT_PIPE/DT_STREAM inline.
        # Rust condvar wakes blocked readers automatically after write.
        _meta = self.metadata.get(path)
        if _meta is not None and _meta.is_pipe:
            n = self._kernel.pipe_write_nowait(path, buf)
            return {"path": path, "bytes_written": n}
        if _meta is not None and _meta.is_stream:
            _off = self._kernel.stream_write_nowait(path, buf)
            return {"path": path, "bytes_written": len(buf), "offset": _off}
        if _meta is None:
            raise NexusFileNotFoundError(
                path, "sys_write requires existing file — use write() for create-on-write"
            )

        # ── KERNEL (pure Rust CAS write, zero GIL) ──
        _is_admin = (
            getattr(context, "is_admin", False)
            if context is not None and not isinstance(context, dict)
            else (context.get("is_admin", False) if isinstance(context, dict) else False)
        )
        _rust_ctx = self._build_rust_ctx(context, _is_admin)
        result = self._kernel.sys_write(path, _rust_ctx, buf)

        if result.hit:
            # Rust wrote to backend (CAS or PAS) + built metadata + updated dcache
            zone_id, agent_id, _ = self._get_context_identity(context)
            self._dispatch_write_events(
                path,
                _WriteContentResult(
                    content_hash=result.content_id or "",
                    size=result.size,
                    metadata=FileMetadata(
                        path=path,
                        backend_name="",
                        physical_path=result.content_id or "",
                        size=result.size,
                        etag=result.content_id,
                        version=result.version,
                        zone_id=zone_id,
                    ),
                    new_version=result.version,
                    is_new=(_meta is None),
                    old_etag=_meta.etag if _meta else None,
                    old_metadata=_meta,
                    context=context or OperationContext(user_id="anonymous", groups=[]),
                    zone_id=zone_id,
                    agent_id=agent_id,
                    is_remote=False,
                    is_external=False,
                ),
                buf,
            )
        else:
            # Fallback: DT_PIPE/DT_STREAM, route fail, or no-backend mount.
            # Normal CAS/PAS backends always hit=true (PR 12a fixed ObjectStore trait).
            self._write_internal(
                path=path, content=buf, offset=offset, context=context, _meta=_meta
            )

        return {"path": path, "bytes_written": len(buf)}

    # ── Tier 2 overrides (NexusFS-specific) ───────────────────────

    @rpc_expose(description="Create directory")
    def mkdir(
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

        # Route to backend with write access check
        route = self.router.route(
            path, is_admin=ctx.is_admin, check_write=True, zone_id=self._zone_id
        )

        if route.readonly:
            raise PermissionError(f"Cannot create directory in read-only path: {path}")

        # Virtual .readme/ paths are read-only (Issue #3728).
        self._reject_if_virtual_readme(path, context, op="mkdir")

        # Check if directory already exists
        existing = route.metastore.get(path)
        is_implicit_dir = existing is None and self.metadata.is_implicit_directory(path)

        if existing is not None or is_implicit_dir:
            if not exist_ok and not parents:
                raise FileExistsError(f"Directory already exists: {path}")
            # DT_MOUNT entries are created by MountTable.add() *before*
            # mkdir is called, so parent dirs may still need metadata.
            if existing is not None:
                if parents:
                    self._ensure_parent_directories(path, ctx)
                return

        # PRE-INTERCEPT hooks via Rust kernel
        _rust_ctx = self._build_rust_ctx(ctx, ctx.is_admin)
        _mkdir_result = self._kernel.sys_mkdir(path, _rust_ctx, parents, exist_ok)

        # Python always does metastore + backend (authoritative metadata with timestamps/backend_key)
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True, context=ctx)

        if parents:
            self._ensure_parent_directories(path, ctx)

        self._setattr_create(
            path,
            DT_DIR,
            {
                "zone_id": ctx.zone_id or ROOT_ZONE_ID,
            },
        )

        # OBSERVE: Rust kernel fires DirCreate when hit=true (§11 Phase 5).
        # Only Python fires for the fallback path.
        if _mkdir_result.post_hook_needed:
            from nexus.contracts.vfs_hooks import MkdirHookContext

            self._kernel.dispatch_post_hooks(
                "mkdir",
                MkdirHookContext(
                    path=path,
                    context=ctx,
                    zone_id=ctx.zone_id,
                    agent_id=ctx.agent_id,
                ),
            )

    @rpc_expose(description="Remove directory")
    def rmdir(
        self,
        path: str,
        recursive: bool = True,
        context: OperationContext | None = None,
    ) -> None:
        """Remove a directory with lenient defaults (Tier 2 convenience).

        Defaults to recursive=True (rm -rf semantics).
        Delegates directly to sys_unlink.
        """
        self.sys_unlink(path, recursive=recursive, context=context)

    @rpc_expose(description="Read file with optional metadata")
    def read(
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
        content = self.sys_read(path, count=count, offset=offset, context=context)

        if not return_metadata:
            return content

        # Compose with sys_stat for metadata
        meta_dict = self.sys_stat(path, context=context)
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
    def write(
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
        ``with locked(path)`` to compose locking at the caller level.
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
        _handled, _result = self.resolve_write(path, buf)
        if _handled:
            return _result

        # Thread TTL into context (Issue #3405)
        if ttl is not None and ttl > 0:
            context = self._ensure_context_ttl(context, ttl)

        return self._write_internal(
            path=path, content=buf, offset=offset, context=context, consistency=_consistency
        )

    def _write_internal(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        consistency: str = "sc",
        offset: int = 0,
        _meta: Any = None,
    ) -> dict[str, Any]:
        """Kernel write implementation — OCC-free.

        Thin composition of _write_content (locked I/O) + _dispatch_write_events
        (sync event dispatch).

        OCC checks (if_match, if_none_match) are done by callers
        (write() convenience method or RPC handlers) BEFORE calling this.

        Used by both sys_write (returns int) and write() (returns dict).

        Issue #1323: OCC params removed from kernel write path.
        Issue #1829: Split into _write_content + _dispatch_write_events (SRP).
        """
        wr = self._write_content(
            path, content, context, offset=offset, consistency=consistency, _meta=_meta
        )
        return self._dispatch_write_events(path, wr, content)

    def _write_content(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
        offset: int = 0,
        consistency: str = "sc",
        _meta: Any = None,
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
            zone_id=self._zone_id,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Path is read-only: {path}")

        # Virtual .readme/ paths are read-only (Issue #3728).
        self._reject_if_virtual_readme(path, context, op="write")

        # Get existing metadata for permission check and update detection (single query)
        now = datetime.now(UTC)
        meta = _meta if _meta is not None else route.metastore.get(path)

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        # Hook handles existing-file (owner fast-path) vs new-file (parent check)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._kernel.dispatch_pre_hooks(
            "write",
            _WHC(
                path=path,
                content=content,
                context=context,
                old_metadata=meta,
            ),
        )

        # Add backend_path to context for path-based connectors
        from dataclasses import replace

        if context:
            context = replace(
                context,
                backend_path=route.backend_path,
                virtual_path=path,
                mount_path=route.mount_point,
            )
        else:
            from nexus.contracts.types import OperationContext

            context = OperationContext(
                user_id="anonymous",
                groups=[],
                backend_path=route.backend_path,
                virtual_path=path,
                mount_path=route.mount_point,
            )

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
                    backend_name=self._driver_coordinator.backend_key(
                        route.backend, route.mount_point
                    ),
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
                    route.metastore.put(metadata, consistency=consistency)
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

                # Kernel-managed metadata (POSIX generic_write_end pattern):
                # kernel updates mtime, size, version, etag in VFS lock
                # after backend.write_content(). Drivers only manage content.
                metadata = self._build_write_metadata(
                    path=path,
                    backend_name=self._driver_coordinator.backend_key(
                        route.backend, route.mount_point
                    ),
                    content_hash=content_hash,
                    # _wr.size is the total file size after splice (not bytes written)
                    size=_wr.size if offset > 0 else len(content),
                    existing_meta=meta,
                    now=now,
                    zone_id=zone_id,
                    context=context,
                )
                new_version = metadata.version
                route.metastore.put(metadata, consistency=consistency)

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

    def _dispatch_write_events(
        self,
        path: str,
        result: _WriteContentResult,
        content: bytes,
    ) -> dict[str, Any]:
        """Post-write event dispatch (sync, outside lock).

        Fires FileEvent notify (OBSERVE) + dispatch_post_hooks (INTERCEPT).
        Uses the augmented context from _write_content (stored in result).

        Returns:
            Dict with metadata {etag, version, modified_at, size}.
        """
        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # OBSERVE dispatch is handled by the caller:
        #   - sys_write hit=true → Rust kernel fires OBSERVE via ThreadPool (§11 Phase 5)
        #   - _write_internal (hit=false fallback) → Python fires notify() before calling this

        # INTERCEPT POST hooks
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
        self._kernel.dispatch_post_hooks("write", _write_ctx)

        # Return metadata for optimistic concurrency control
        return {
            "etag": result.content_hash,
            "version": result.new_version,
            "modified_at": result.metadata.modified_at,
            "size": result.size,
        }

    def atomic_update(
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

        For multiple operations within one lock, use ``with locked()`` instead.

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
            >>> nx.atomic_update(
            ...     "/counters/visits.json",
            ...     lambda c: json.dumps({"count": json.loads(c)["count"] + 1}).encode()
            ... )

            >>> # Append to a log file atomically
            >>> nx.atomic_update(
            ...     "/logs/access.log",
            ...     lambda c: c + b"New log entry\\n"
            ... )

            >>> # Update config safely across multiple agents
            >>> nx.atomic_update(
            ...     "/shared/config.json",
            ...     lambda c: json.dumps({**json.loads(c), "version": 2}).encode()
            ... )
        """
        with self.locked(path, timeout=timeout, ttl=ttl, context=context) as lock_id:  # noqa: F841
            content = self.sys_read(path, context=context)
            new_content = update_fn(content)
            return self.write(path, new_content, context=context)

    @rpc_expose(description="Append content to an existing file or create if it doesn't exist")
    def append(
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
            result = self.read(path, context=context, return_metadata=True)
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
        return self.write(
            path,
            final_content,
            context=context,
        )

    @rpc_expose(description="Apply surgical search/replace edits to a file")
    def edit(
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
        result = self.read(path, context=context, return_metadata=True)
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
        write_result = self.write(
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
    def write_batch(
        self, files: list[tuple[str, bytes]], context: OperationContext | None = None
    ) -> list[dict[str, Any]]:
        """
        Write multiple files in a single round-trip for improved performance.

        This is 13x faster than calling write() multiple times for small files
        because it uses a single database transaction instead of N transactions.

        **Atomicity**: best-effort. For CAS backends (the common case) each file
        is written independently via content-addressed storage, so a mid-batch
        failure leaves already-written files on disk. No rollback or compensation
        is performed. Callers that need true all-or-nothing semantics should use
        separate write() calls inside an explicit transaction (if supported) or
        implement idempotent retries using the returned etags.

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

            >>> # Best-effort batch write (not all-or-nothing; see docstring)
            >>> files = [
            ...     ("/config/setting1.json", b'{"enabled": true}'),
            ...     ("/config/setting2.json", b'{"timeout": 30}'),
            ... ]
            >>> nx.write_batch(files)
        """
        if not files:
            return []

        # Validate paths
        validated_files: list[tuple[str, bytes]] = []
        for path, content in files:
            validated_path = self._validate_path(path)
            # Virtual .readme/ paths are read-only (Issue #3728).
            self._reject_if_virtual_readme(validated_path, context, op="write_batch")
            validated_files.append((validated_path, content))

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        paths = [p for p, _ in validated_files]

        # Get existing metadata for pre-hooks and is_new detection
        existing_metadata = self.metadata.get_batch(paths)

        # PRE-INTERCEPT: pre-write hooks per file in batch
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        for path in paths:
            meta = existing_metadata.get(path)
            self._kernel.dispatch_pre_hooks(
                "write",
                _WHC(
                    path=path,
                    content=b"",
                    context=context,
                    old_metadata=meta,
                ),
            )

        # ── KERNEL: Rust batch write (validate + route + lock + write + metastore + dcache) ──
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        rust_results = self._kernel._write_batch(validated_files, _rust_ctx)

        now = datetime.now(UTC)
        metadata_list: list[FileMetadata] = []
        results: list[dict[str, Any]] = []

        for i, (path, content) in enumerate(validated_files):
            r = rust_results[i]
            if r.hit:
                results.append(
                    {
                        "etag": r.content_id,
                        "version": r.version,
                        "modified_at": now,
                        "size": r.size,
                    }
                )
                metadata_list.append(
                    FileMetadata(
                        path=path,
                        backend_name="",
                        physical_path=r.content_id or "",
                        size=r.size,
                        etag=r.content_id,
                        version=r.version,
                        zone_id=zone_id or "root",
                    )
                )
            else:
                # Fallback: remote backend or route failure — use Python path
                route = self.router.route(
                    path, is_admin=is_admin, check_write=True, zone_id=self._zone_id
                )
                _write_ctx = (
                    _dc_replace(
                        context,
                        backend_path=route.backend_path,
                        virtual_path=path,
                        mount_path=route.mount_point,
                    )
                    if context
                    else OperationContext(
                        user_id="anonymous",
                        groups=[],
                        backend_path=route.backend_path,
                        virtual_path=path,
                        mount_path=route.mount_point,
                    )
                )
                content_hash = route.backend.write_content(content, context=_write_ctx).content_id
                meta = existing_metadata.get(path)
                new_version = (meta.version + 1) if meta else 1
                results.append(
                    {
                        "etag": content_hash,
                        "version": new_version,
                        "modified_at": now,
                        "size": len(content),
                    }
                )
                metadata_list.append(
                    FileMetadata(
                        path=path,
                        backend_name=self._driver_coordinator.backend_key(
                            route.backend, route.mount_point
                        ),
                        physical_path=content_hash,
                        size=len(content),
                        etag=content_hash,
                        created_at=meta.created_at if meta else now,
                        modified_at=now,
                        version=new_version,
                        zone_id=zone_id or "root",
                    )
                )

        # Persist metadata for all items via Python metastore
        # (Rust _write_batch updates Rust DCache but Python metastore needs explicit put)
        self.metadata.put_batch(metadata_list)

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        items = [
            (metadata, existing_metadata.get(metadata.path) is None) for metadata in metadata_list
        ]
        from nexus.contracts.vfs_hooks import WriteBatchHookContext

        self._dispatch_batch_post_hook(
            "write_batch",
            WriteBatchHookContext(items=items, context=context, zone_id=zone_id, agent_id=agent_id),
        )

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        for metadata in metadata_list:
            old_meta = existing_metadata.get(metadata.path)
            _ = old_meta is None  # is_new removed with notify

        # Issue #1682: Hierarchy tuples + owner grants moved to post_write_batch hooks.

        return results

    def _dispatch_batch_post_hook(self, event_name: str, ctx: Any) -> None:
        """Dispatch a post-batch hook if any listeners are registered.

        Shared by write_batch and read_batch to avoid duplicating the
        hook_count guard + dispatch_post_hooks call.
        """
        if self._kernel.hook_count(event_name) > 0:
            self._kernel.dispatch_post_hooks(event_name, ctx)

    @rpc_expose(description="Read multiple files atomically in a single round-trip")
    def read_batch(
        self,
        paths: list[str],
        *,
        partial: bool = False,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        """
        Read multiple files in a single round-trip for improved performance.

        Uses the Rust kernel's parallel _read_batch (rayon par_iter) for all
        paths, then a single metadata.get_batch() call — no N+1 queries.

        Args:
            paths:   List of virtual paths to read.
            partial: If False (default), raises NexusFileNotFoundError on
                     the first path that is missing or inaccessible.
                     If True, returns a per-item result for every path
                     (successful reads and errors alike).
            context: Optional operation context for permission checks.

        Returns:
            List of dicts in the same order as *paths*.

            Successful item::

                {
                    "path":        str,
                    "content":     bytes,
                    "etag":        str | None,   # from actual read bytes (r.content_hash)
                    "version":     int,           # from pre-read metadata snapshot
                    "modified_at": datetime | None,  # from pre-read metadata snapshot
                    "size":        int,
                }

            **Note on consistency**: ``etag`` reflects the actual bytes returned
            (authoritative). ``version`` and ``modified_at`` come from a metadata
            snapshot taken *before* the reads, so under concurrent writes they
            may not match the returned content. Use ``etag`` for cache validation
            or optimistic concurrency; do not rely on ``version``/``modified_at``
            being coherent with the content under concurrent updates.

            Failed item (only possible when partial=True)::

                {
                    "path":  str,
                    "error": "not_found",
                }

        Raises:
            InvalidPathError:       If any path is invalid (always, even in partial mode).
            NexusFileNotFoundError: If any path is missing and partial=False.
            NexusPermissionError:   If access is denied and partial=False.
        """
        if not paths:
            return []

        # Validate all paths up-front — invalid paths always raise, even in partial mode.
        validated_paths: list[str] = [self._validate_path(p) for p in paths]

        zone_id, agent_id, is_admin = self._get_context_identity(context)
        _rust_ctx = self._build_rust_ctx(context, is_admin)

        # PRE-INTERCEPT: per-path stat/read permission hooks (same pattern as read_bulk).
        from nexus.contracts.exceptions import PermissionDeniedError
        from nexus.contracts.vfs_hooks import StatHookContext as _SHC

        _ctx = self._resolve_cred(context)
        allowed_paths: list[str] = []
        denied_paths: set[str] = set()
        for path in validated_paths:
            try:
                self._kernel.dispatch_pre_hooks(
                    "stat", _SHC(path=path, context=_ctx, permission="READ")
                )
                allowed_paths.append(path)
            except PermissionDeniedError as exc:
                if not partial:
                    from nexus.contracts.exceptions import NexusPermissionError

                    raise NexusPermissionError(f"Permission denied: {path}") from exc
                denied_paths.add(path)

        # Batch metadata fetch — one query for all allowed paths.
        batch_meta = self.metadata.get_batch(allowed_paths) if allowed_paths else {}

        # Finding #3 — DoS guard: reject batches whose declared metadata size exceeds
        # the per-request ceiling.  Uses metadata sizes already fetched, so no extra
        # round-trip is needed.  External-mount / virtual paths that lack metadata
        # entries contribute 0 to the total; their own backends enforce their limits.
        #
        # IMPORTANT: iterate over allowed_paths (with duplicates), NOT over
        # batch_meta.values() (unique keys).  A request repeating the same large file
        # N times would otherwise bypass the cap since the dict only stores one entry
        # per unique path.
        _MAX_BATCH_READ_BYTES = 100 * 1024 * 1024  # 100 MB
        if allowed_paths and batch_meta:
            _total_declared = sum(
                batch_meta[p].size
                for p in allowed_paths
                if batch_meta.get(p) is not None  # value may be None for missing files
            )
            if _total_declared > _MAX_BATCH_READ_BYTES:
                raise ValueError(
                    f"Batch read aggregate declared size {_total_declared} bytes exceeds "
                    f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                )

        # KERNEL: parallel Rust read for all allowed paths.
        rust_results = self._kernel._read_batch(allowed_paths, _rust_ctx) if allowed_paths else []

        results: list[dict[str, Any]] = []
        hit_items: list[tuple[str, "FileMetadata | None"]] = []  # for post-hooks

        # Check once whether any per-file "read" post-hooks are registered.
        # These hooks (e.g. DynamicViewerReadHook) may transform or redact content.
        # Finding #1 — we must fire them per-item so batch semantics match single read().
        _has_read_hooks = self._kernel.hook_count("read") > 0

        # Map allowed_paths → rust_results (same order, guaranteed by _read_batch).
        allowed_iter = iter(rust_results)

        # Cumulative byte counter — tracks actual bytes loaded across both the
        # CAS fast path and the fallback read() path.  External/virtual paths have
        # no metadata entry so they contribute 0 to the upfront declared-size check;
        # their actual content is captured here to close that gap.
        _loaded_bytes = 0

        for path in validated_paths:
            if path in denied_paths:
                results.append({"path": path, "error": "permission_denied"})
                continue

            r = next(allowed_iter)
            meta = batch_meta.get(path)

            if not r.hit:
                # Finding #2 — _read_batch returns hit=False not only for missing CAS
                # files but also for: DT_PIPE / DT_STREAM entries, backend read errors,
                # lock timeouts, route misses, and external connector paths.  A bare
                # hit=False must not be treated as "file not found" for all of these.
                #
                # Delegate to the full single-file read() path, which correctly handles:
                #   • virtual resolver paths (resolve_read)
                #   • external connector mounts (ExternalRouteResult)
                #   • DT_PIPE / DT_STREAM entry types
                #   • standard per-file read hooks (DynamicViewerReadHook, etc.)
                #
                # Only NexusFileNotFoundError from read() is classified as "not found";
                # any other exception is a real failure and either propagates (strict
                # mode) or surfaces as a per-item "read_error" (partial mode).
                #
                # Resolver permission errors and parser failures are NOT caught here —
                # they propagate through read() just as they would via the single-file
                # endpoint.
                try:
                    content = self.read(path, context=context)
                    _loaded_bytes += len(content)
                    if _loaded_bytes > _MAX_BATCH_READ_BYTES:
                        raise ValueError(
                            f"Batch read aggregate size exceeded "
                            f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                        )
                    results.append(
                        {
                            "path": path,
                            "content": content,
                            "etag": meta.etag if meta else None,
                            "version": meta.version if meta else 0,
                            "modified_at": meta.modified_at if meta else None,
                            "size": len(content),
                        }
                    )
                    hit_items.append((path, meta))
                    continue
                except NexusFileNotFoundError:
                    pass  # Confirmed missing — fall through to not_found handling.
                except Exception:
                    # Real failure (backend error, permission denied, lock timeout…).
                    # In partial mode return a per-item error so the rest of the batch
                    # is not aborted.  In strict mode re-raise so the caller sees the
                    # actual failure.
                    if not partial:
                        raise
                    results.append({"path": path, "error": "read_error"})
                    continue

                if not partial:
                    raise NexusFileNotFoundError(path)
                results.append({"path": path, "error": "not_found"})
                continue

            content = bytes(r.data) if r.data else b""
            _loaded_bytes += len(content)
            if _loaded_bytes > _MAX_BATCH_READ_BYTES:
                raise ValueError(
                    f"Batch read aggregate size exceeded "
                    f"{_MAX_BATCH_READ_BYTES // (1024 * 1024)} MB limit"
                )

            # Finding #1 — per-item "read" post-hook (mirrors read() at line ~1285).
            # Ensures content-transforming hooks such as DynamicViewerReadHook fire
            # for every successfully read item, preventing authorization bypass via
            # the batch endpoint.
            if _has_read_hooks:
                from nexus.contracts.vfs_hooks import ReadHookContext

                _read_ctx = ReadHookContext(
                    path=path,
                    context=context,
                    zone_id=zone_id,
                    agent_id=agent_id,
                    content=content,
                    content_hash=r.content_hash,
                )
                self._kernel.dispatch_post_hooks("read", _read_ctx)
                content = _read_ctx.content or content

            # Use r.content_hash as the primary etag — it reflects the actual bytes
            # returned by this read, not the pre-read metadata snapshot (which can be
            # stale under concurrent writes).  Fall back to meta.etag only when the
            # Rust result has no content_hash (older backends / degenerate path).
            _etag = r.content_hash or (meta.etag if meta else None)
            results.append(
                {
                    "path": path,
                    "content": content,
                    "etag": _etag,
                    "version": meta.version if meta else 0,
                    "modified_at": meta.modified_at if meta else None,
                    "size": len(content),
                }
            )
            hit_items.append((path, meta))

        # POST-INTERCEPT: batch post-hook (only if listeners registered).
        from nexus.contracts.vfs_hooks import ReadBatchHookContext

        self._dispatch_batch_post_hook(
            "read_batch",
            ReadBatchHookContext(
                items=hit_items, context=context, zone_id=zone_id, agent_id=agent_id
            ),
        )

        return results

    @rpc_expose(description="Delete file")
    def sys_unlink(
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
            Dict on success.

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
            self._service_registry.unregister_service_full(name)
            return {"path": path, "unregistered": True, "service": name}

        if path.startswith("/__sys__/hooks/"):
            raise NotImplementedError(
                "Standalone hook removal via /__sys__/hooks/ not yet supported."
            )

        # DT_PIPE fast-path: check Rust IPC registry + custom backends
        if self._kernel.has_pipe(path) or path in self._custom_pipe_backends:
            return self._pipe_destroy(path)
        # DT_STREAM fast-path: check Rust IPC registry + custom backends
        if self._kernel.has_stream(path) or path in self._custom_stream_backends:
            return self._stream_destroy(path)

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self.resolve_delete(path, context=context)
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
            zone_id=self._zone_id,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot delete from read-only path: {path}")

        # Virtual .readme/ paths are read-only (Issue #3728).
        self._reject_if_virtual_readme(path, context, op="delete")

        # Check if file exists in metadata.
        # Use prefetched hint from resolve_delete() if available (#1311)
        meta = _result if _result is not None else route.metastore.get(path)

        if meta is None:
            raise NexusFileNotFoundError(path)

        # ── Directory branch: rmdir logic ────────────────────────────
        if meta.is_dir or meta.is_mount or meta.is_external_storage:
            return self._unlink_directory(
                path, meta=meta, route=route, recursive=recursive, context=context
            )

        # ── File branch: regular unlink ──────────────────────────────

        # PRE-INTERCEPT hooks dispatched by Rust kernel
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        _unlink_result = self._kernel.sys_unlink(path, _rust_ctx)

        # POST-INTERCEPT hooks
        from nexus.contracts.vfs_hooks import DeleteHookContext

        _delete_ctx = DeleteHookContext(
            path=path,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            metadata=meta,
        )
        if _unlink_result.post_hook_needed:
            self._kernel.dispatch_post_hooks("delete", _delete_ctx)

        # Python always does metastore + backend delete under VFS lock
        # (Rust kernel has the capability for FUSE/gRPC bypass)
        with self._vfs_locked(path, "write"):
            route.metastore.delete(path)

            # PAS backend propagation
            if hasattr(route.backend, "delete"):
                try:
                    route.backend.delete(route.backend_path, context=context)
                except Exception as _be:
                    logger.warning(
                        "Backend file delete %s failed (metadata already deleted): %s",
                        route.backend_path,
                        _be,
                    )

        # OBSERVE: Rust kernel fires FileDelete when hit=true (§11 Phase 5).
        # Only Python fires for the fallback path.

        return {}

    def _unlink_directory(
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

        self._kernel.dispatch_pre_hooks("rmdir", RmdirHookContext(path=path, context=ctx))

        # DT_MOUNT / DT_EXTERNAL_STORAGE: unmount via DriverLifecycleCoordinator + delete metadata
        if meta.is_mount or meta.is_external_storage:
            removed = self._driver_coordinator.unmount(path)
            if removed:
                route.metastore.delete(path)
                logger.info("sys_unlink: unmounted %s", path)
            return {}

        # Python always does full rmdir (Rust kernel has the capability for FUSE/gRPC bypass)
        dir_path = path if path.endswith("/") else path + "/"
        # Use recursive listing when deleting recursively so all descendants are
        # cleaned from the metastore in one batch (not just immediate children).
        files_in_dir = (
            route.metastore.list(dir_path, recursive=True)
            if recursive
            else route.metastore.list(dir_path)
        )

        if files_in_dir:
            if not recursive:
                raise OSError(errno.ENOTEMPTY, f"Directory not empty: {path}")
            # Recursive: batch delete all children
            file_paths = [file_meta.path for file_meta in files_in_dir]
            route.metastore.delete_batch(file_paths)

        # Remove directory in backend (suppress errors — CAS may not have physical dir,
        # or it may already be gone if metastore and backend are out of sync)
        with contextlib.suppress(NexusFileNotFoundError, BackendError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Delete directory's own metadata entry
        try:
            route.metastore.delete(path)
        except Exception as e:
            logger.debug("Failed to delete directory metadata for %s: %s", path, e)

        # Clean up sparse directory index entries
        if hasattr(route.metastore, "delete_directory_entries_recursive"):
            try:
                route.metastore.delete_directory_entries_recursive(path)
            except Exception as e:
                logger.debug("Failed to clean up directory index for %s: %s", path, e)

        self._kernel.dispatch_post_hooks(
            "rmdir",
            RmdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                recursive=recursive,
            ),
        )

        return {}

    @rpc_expose(description="Rename/move file")
    def sys_rename(
        self,
        old_path: str,
        new_path: str,
        *,
        force: bool = False,
        context: OperationContext | None = None,
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
            force: If True, delete the destination before renaming (overwrite).
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If source file doesn't exist
            FileExistsError: If destination path already exists (and force=False)
            InvalidPathError: If either path is invalid
            PermissionError: If either path is read-only
            AccessDeniedError: If access is denied (zone isolation)

        Example:
            >>> nx.sys_rename('/workspace/old.txt', '/workspace/new.txt')
            >>> nx.sys_rename('/folder-a/file.txt', '/shared/folder-a/file.txt')
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
            zone_id=self._zone_id,
        )
        new_route = self.router.route(
            new_path,
            is_admin=is_admin,
            check_write=True,  # Need write access to destination
            zone_id=self._zone_id,
        )

        # Check if paths are read-only
        if old_route.readonly:
            raise PermissionError(f"Cannot rename from read-only path: {old_path}")
        if new_route.readonly:
            raise PermissionError(f"Cannot rename to read-only path: {new_path}")

        # Virtual .readme/ paths are read-only on both ends (Issue #3728).
        self._reject_if_virtual_readme(old_path, context, op="rename")
        self._reject_if_virtual_readme(new_path, context, op="rename")

        # ── Fast-fail (unlocked, optimization only) ──
        # Avoids lock acquisition for the common "file not found" error case.
        # Not authoritative — re-checked under lock below.
        if not old_route.metastore.exists(old_path) and not self.metadata.is_implicit_directory(
            old_path
        ):
            raise NexusFileNotFoundError(old_path)

        meta = old_route.metastore.get(old_path)
        is_directory = (
            meta and meta.mime_type == "inode/directory"
        ) or self.metadata.is_implicit_directory(old_path)

        # PRE-INTERCEPT hooks dispatched by Rust kernel
        _rust_ctx = self._build_rust_ctx(context, is_admin)
        _rename_result = self._kernel.sys_rename(old_path, new_path, _rust_ctx)

        # Python always does full metastore rename under VFS lock
        # (Rust kernel has the capability for FUSE/gRPC bypass, but Python
        # wrapper continues to use route.metastore for authoritative metadata)
        _first, _second = sorted([old_path, new_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # Authoritative checks (under lock, TOCTOU-safe)
                is_implicit_dir = not old_route.metastore.exists(
                    old_path
                ) and self.metadata.is_implicit_directory(old_path)
                if not old_route.metastore.exists(old_path) and not is_implicit_dir:
                    raise NexusFileNotFoundError(old_path)

                meta = old_route.metastore.get(old_path)
                is_directory = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                # Check destination — use backend.file_exists() for PAS backends
                if new_route.metastore.exists(new_path):
                    if force:
                        # force=True: delete destination so rename can proceed
                        self.sys_unlink(new_path, recursive=True, context=context)
                    elif hasattr(new_route.backend, "file_exists"):
                        if new_route.backend.file_exists(new_route.backend_path):
                            raise FileExistsError(f"Destination path already exists: {new_path}")
                        logger.warning(
                            "Cleaning up stale metadata for %s (file not in backend storage)",
                            new_path,
                        )
                        new_route.metastore.delete(new_path)
                    else:
                        raise FileExistsError(f"Destination path already exists: {new_path}")

                # Metadata rename (put-first for crash safety)
                from dataclasses import replace as _replace

                _old_meta = old_route.metastore.get(old_path)
                if _old_meta is not None:
                    _new_meta = _replace(_old_meta, path=new_path)
                    new_route.metastore.put(_new_meta)
                    old_route.metastore.delete(old_path)
                elif not is_directory:
                    raise NexusFileNotFoundError(old_path)

                # Rename children (directories)
                if is_directory:
                    _prefix = old_path.rstrip("/") + "/"
                    for child in old_route.metastore.list(_prefix, recursive=True):
                        _child_new = new_path + child.path[len(old_path) :]
                        _child_new_meta = _replace(child, path=_child_new)
                        new_route.metastore.put(_child_new_meta)
                        old_route.metastore.delete(child.path)

                # PAS backend propagation
                if hasattr(old_route.backend, "rename"):
                    try:
                        old_route.backend.rename(
                            old_route.backend_path,
                            new_route.backend_path,
                            context=context,
                        )
                    except Exception as _be:
                        logger.warning(
                            "Backend rename %s → %s failed (metadata already updated): %s",
                            old_route.backend_path,
                            new_route.backend_path,
                            _be,
                        )
            finally:
                if _h2:
                    self._vfs_lock_manager.release(_h2)
                    from nexus.lib.lock_order import L1_VFS, mark_released

                    mark_released(L1_VFS)
        finally:
            self._vfs_lock_manager.release(_h1)
            from nexus.lib.lock_order import L1_VFS, mark_released

            mark_released(L1_VFS)

        # OBSERVE: Rust kernel fires FileRename when hit=true (§11 Phase 5).
        # Only Python fires for the fallback path.

        # POST-INTERCEPT hooks
        if _rename_result.post_hook_needed:
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
            self._kernel.dispatch_post_hooks("rename", _rename_ctx)

        return {}

    # ------------------------------------------------------------------
    # sys_copy — Issue #3329 (Workstream 3: native copy/move)
    # ------------------------------------------------------------------

    @rpc_expose(description="Copy file with native backend support")
    def sys_copy(
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
        src_route = self.router.route(src_path, is_admin=is_admin, zone_id=self._zone_id)
        dst_route = self.router.route(
            dst_path, is_admin=is_admin, check_write=True, zone_id=self._zone_id
        )

        if dst_route.readonly:
            raise PermissionError(f"Cannot copy to read-only path: {dst_path}")

        # Virtual .readme/ destination is read-only (Issue #3728).
        self._reject_if_virtual_readme(dst_path, context, op="copy")

        # Virtual .readme/ source: bypass metastore and copy the virtual
        # bytes to the destination.  Virtual docs have no metastore row
        # so the normal ``src_meta.get`` path below would fail before
        # hooks run — do the safety checks the normal branch does
        # (source READ permission via the copy hook, destination
        # existence on BOTH metastore and backend, locked re-check to
        # close the concurrent-write race in round 8 finding #19) here
        # first.  Round 6 findings #14+#15, round 8 finding #19.
        _virtual_src_bytes = self._try_virtual_readme_bytes(src_path, context)
        if _virtual_src_bytes is not None:
            # Enforce source READ permission + destination WRITE
            # permission via the same copy-hook pipeline the normal
            # copy path runs.  Permission hooks receive a synthesized
            # metadata dict since virtual docs have no row.
            from nexus.contracts.vfs_hooks import CopyHookContext as _CHC

            _virtual_copy_ctx = _CHC(
                src_path=src_path,
                dst_path=dst_path,
                context=context,
                zone_id=zone_id,
                agent_id=agent_id,
                metadata=None,  # virtual docs have no FileMetadata row
            )
            self._kernel.dispatch_pre_hooks("copy", _virtual_copy_ctx)

            def _check_dst_exists() -> None:
                """Raise FileExistsError if dst_path is occupied.

                Probes both the metastore and the backend so a real
                backend file that hasn't been synced to the metastore
                still blocks the copy.
                """
                if dst_route.metastore.exists(dst_path):
                    raise FileExistsError(f"Destination path already exists: {dst_path}")
                _dst_bp = getattr(dst_route, "backend_path", "") or ""
                _dst_be = getattr(dst_route, "backend", None)
                _fn = getattr(_dst_be, "content_exists", None)
                if _dst_be is not None and callable(_fn):
                    from dataclasses import replace as _replace

                    try:
                        _pctx = (
                            _replace(context, backend_path=_dst_bp) if context is not None else None
                        )
                    except Exception:
                        _pctx = None
                    try:
                        if _fn(_dst_bp, context=_pctx):
                            raise FileExistsError(
                                f"Destination path already exists on backend: {dst_path}"
                            )
                    except FileExistsError:
                        raise
                    except Exception as _probe_err:
                        # Probe failed — downgrade to debug-log and
                        # fall through.  The follow-up ``write()`` call
                        # will surface any permanent create-semantics
                        # error with its own richer context, so
                        # raising a best-effort probe error here would
                        # just add noise.  Logged (not swallowed) so
                        # ``test_no_silent_swallowers_in_nexus_fs``
                        # stays green.
                        logger.debug(
                            "[VIRTUAL-COPY] backend.content_exists probe failed for %s: %s",
                            _dst_bp,
                            _probe_err,
                        )

            # Destination-exists fast-fail (round 6 finding #15 — the
            # probe covers both metastore and backend).
            #
            # Round 9 finding #21: we do NOT wrap this in
            # ``_vfs_locked(dst_path)`` because the follow-up
            # ``self.write(dst_path, ...)`` takes the same path-level
            # write lock internally, and the lock manager is not
            # re-entrant — nesting the acquisition would deadlock
            # until the 5-second timeout.  The residual TOCTOU
            # window between this check and ``write()``'s own
            # acquisition is bounded by ``write()``'s last-writer-
            # wins overwrite semantics — the same behavior every
            # other caller of ``write()`` already tolerates.
            _check_dst_exists()
            write_result = self.write(dst_path, _virtual_src_bytes, context=context)
            self._kernel.dispatch_post_hooks("copy", _virtual_copy_ctx)
            return {
                "src_path": src_path,
                "dst_path": dst_path,
                "size": len(_virtual_src_bytes),
                "etag": write_result.get("etag"),
                "version": write_result.get("version"),
                "modified_at": write_result.get("modified_at"),
            }

        # Fast-fail (unlocked — re-checked under lock)
        if not src_route.metastore.exists(src_path) and not self.metadata.is_implicit_directory(
            src_path
        ):
            raise NexusFileNotFoundError(src_path)

        src_meta = src_route.metastore.get(src_path)
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
        self._kernel.dispatch_pre_hooks("copy", _copy_ctx)

        # VFS I/O Lock: exclusive write on dst, shared read on src
        _first, _second = sorted([src_path, dst_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # Authoritative checks under lock
                src_meta = src_route.metastore.get(src_path)
                if src_meta is None:
                    raise NexusFileNotFoundError(src_path)

                if dst_route.metastore.exists(dst_path):
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
                    dst_size = src_route.backend.get_size_by_path(dst_route.backend_path)
                    dst_version: str | None = None
                    if hasattr(src_route.backend, "get_version_by_path"):
                        dst_version = src_route.backend.get_version_by_path(dst_route.backend_path)

                    from dataclasses import replace as _replace

                    dst_meta = _replace(
                        src_meta,
                        path=dst_path,
                        physical_path=dst_route.backend_path,
                        etag=dst_version or src_meta.etag,
                        size=dst_size,
                    )
                    dst_route.metastore.put(dst_meta)
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
                    dst_route.metastore.put(dst_meta)
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
                        if hasattr(dst_route.backend, "get_version_by_path"):
                            dst_version_id = dst_route.backend.get_version_by_path(
                                dst_route.backend_path
                            )

                        from dataclasses import replace as _replace

                        dst_meta = _replace(
                            src_meta,
                            path=dst_path,
                            physical_path=dst_route.backend_path,
                            etag=dst_version_id or src_meta.etag,
                        )
                        dst_route.metastore.put(dst_meta)
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
                                mount_path=src_route.mount_point,
                            )
                            if context
                            else context
                        )
                        content = self._driver_coordinator.resolve_backend(
                            src_meta.backend_name
                        ).read_content(
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
                        dst_route.metastore.put(dst_meta)
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

        self._kernel.dispatch_post_hooks("copy", _copy_ctx)

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
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE",
                        extra={"is_implicit_directory": True},
                    ),
                )
            except PermissionDeniedError:
                raise PermissionError(
                    f"Access denied: User '{ctx.user_id}' does not have TRAVERSE "
                    f"permission for '{path}'"
                ) from None
        else:
            from nexus.contracts.vfs_hooks import ReadHookContext as _RHC

            self._kernel.dispatch_pre_hooks("read", _RHC(path=path, context=context))

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
            # Virtual .readme/ overlay check (Issue #3728) — before raising,
            # see if the path routes to a skill backend's .readme/ tree.
            _vstat = self._try_virtual_readme_stat(path, ctx)
            if _vstat is not None:
                return _vstat
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
                zone_id=self._zone_id,
            )
            try:
                # Add backend_path to context for path-based connectors
                size_context = context
                if context:
                    from dataclasses import replace

                    size_context = replace(
                        context,
                        backend_path=route.backend_path,
                        mount_path=route.mount_point,
                    )
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
                    self._kernel.dispatch_pre_hooks(
                        "stat", _SHC(path=p, context=ctx, permission="READ")
                    )
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
    def access(self, path: str, *, context: OperationContext | None = None) -> bool:
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
                self._kernel.dispatch_pre_hooks(
                    "stat",
                    _SHC(
                        path=path,
                        context=ctx,
                        permission="TRAVERSE" if is_implicit_dir else "READ",
                        extra={"is_implicit_directory": is_implicit_dir},
                    ),
                )
            except PermissionDeniedError:
                return False

            if self.metadata.exists(path):
                return True
            if is_implicit_dir:
                return True
            # Virtual .readme/ overlay check (Issue #3728) — before reporting
            # not-found, see if the path resolves to a virtual skill doc.
            return self._try_virtual_readme_stat(path, ctx) is not None
        except (InvalidPathError, NexusFileNotFoundError, BackendError):
            return False

    @rpc_expose(description="Check existence of multiple paths in single call")
    def exists_batch(
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
                results[path] = self.access(path, context=context)
            except Exception as exc:
                # Any error means file doesn't exist or isn't accessible
                logger.debug("Exists check failed for %s: %s", path, exc)
                results[path] = False
        return results

    @rpc_expose(description="Get metadata for multiple paths in single call")
    def metadata_batch(
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
                    self._kernel.dispatch_pre_hooks(
                        "stat", _SHC(path=path, context=ctx, permission="READ")
                    )
                except PermissionDeniedError:
                    results[path] = None
                    continue

                # Check if it's a directory
                is_dir = self.is_directory(path, context=context)

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
    def delete_batch(
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
        # Validate all paths first
        validated: list[str] = []
        results: dict[str, dict] = {}
        for path in paths:
            try:
                validated.append(self._validate_path(path))
            except Exception as e:
                results[path] = {"success": False, "error": str(e)}

        if not validated:
            return results

        # Batch metadata lookup (single query instead of N)
        batch_meta = self.metadata.get_batch(validated)

        for path in validated:
            try:
                # Virtual .readme/ paths are read-only (Issue #3728).
                self._reject_if_virtual_readme(path, context, op="delete")

                meta = batch_meta.get(path)

                # Check for implicit directory (exists because it has files beneath it)
                is_implicit_dir = meta is None and self.metadata.is_implicit_directory(path)

                if meta is None and not is_implicit_dir:
                    results[path] = {"success": False, "error": "File not found"}
                    continue

                # Check if this is a directory (explicit or implicit)
                is_dir = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

                if is_dir:
                    self._rmdir_internal(
                        path, recursive=recursive, context=context, is_implicit=is_implicit_dir
                    )
                else:
                    self.sys_unlink(path, context=context)

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
            zone_id=self._zone_id,
        )

        if route.readonly:
            raise PermissionError(f"Cannot remove read-only directory: {path}")

        # PRE-INTERCEPT: pre-write hooks (Issue #899)
        from nexus.contracts.vfs_hooks import WriteHookContext as _WHC

        self._kernel.dispatch_pre_hooks("write", _WHC(path=path, content=b"", context=context))

        # Check if path exists (explicit or implicit)
        meta = route.metastore.get(path)
        if is_implicit is None:
            is_implicit = meta is None and self.metadata.is_implicit_directory(path)

        if meta is None and not is_implicit:
            raise NexusFileNotFoundError(path)

        # Check if it's a directory (skip for implicit dirs - they're always directories)
        if meta is not None and meta.mime_type != "inode/directory":
            raise OSError(errno.ENOTDIR, "Not a directory", path)

        # Get files in directory
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = route.metastore.list(dir_path)

        if files_in_dir and not recursive:
            raise OSError(errno.ENOTEMPTY, "Directory not empty", path)

        if recursive and files_in_dir:
            # Issue #1320/#1772: Content cleanup deferred to CAS reachability
            # GC. Kernel only deletes metadata.

            # Batch delete from metadata store
            file_paths = [file_meta.path for file_meta in files_in_dir]
            route.metastore.delete_batch(file_paths)

        # Remove directory in backend
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Delete the directory metadata (only if explicit directory)
        if not is_implicit:
            route.metastore.delete(path)

    @rpc_expose(description="Rename/move multiple files")
    def rename_batch(
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
                self.sys_rename(old_path, new_path, context=context)
                results[old_path] = {"success": True, "new_path": new_path}
            except Exception as e:
                results[old_path] = {"success": False, "error": str(e)}

        return results

    # ------------------------------------------------------------------
    # Method forwarders — delegate to services.
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
    def sys_readdir(
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
        # ── /__sys__/locks/ virtual namespace (like /proc/locks) ──
        if path.rstrip("/") == "/__sys__/locks":
            locks = self._lock_manager.list_locks()
            if details:
                return [self._format_lock_info(lk) for lk in locks]
            return [lk.path for lk in locks]

        # ── External connector mount listing (S3, GCS, etc.) ──
        # Only intercept ExternalRouteResult — these are mounts with
        # is_external_storage metadata set. Plain RouteResult backends
        # (LocalBackend, CASLocalBackend, etc.) use the normal metastore path.
        if path and path != "/" and getattr(self, "router", None):
            try:
                from nexus.core.router import ExternalRouteResult

                _is_admin = (
                    context.is_admin
                    if context is not None and not isinstance(context, dict)
                    else (context.get("is_admin", False) if isinstance(context, dict) else False)
                )
                _route = self.router.route(
                    path, is_admin=_is_admin, check_write=False, zone_id=self._zone_id
                )
                backend = getattr(_route, "backend", None)
                if isinstance(_route, ExternalRouteResult) and backend is not None:
                    backend_path = getattr(_route, "backend_path", "") or ""
                    mount_point = getattr(_route, "mount_point", "") or ""
                    _ctx = (
                        _dc_replace(
                            context,
                            backend_path=backend_path,
                            virtual_path=path,
                            mount_path=mount_point,
                        )
                        if context
                        else OperationContext(
                            user_id="anonymous",
                            groups=[],
                            backend_path=backend_path,
                            virtual_path=path,
                            mount_path=mount_point,
                        )
                    )
                    # Virtual .readme/ overlay check (Issue #3728).
                    from nexus.backends.connectors.schema_generator import (
                        _has_skill_name,
                        _readme_dir_for,
                        dispatch_virtual_readme_list,
                        get_virtual_readme_tree_for_backend,
                    )

                    _virtual_entries = dispatch_virtual_readme_list(
                        backend, mount_point, backend_path, context=_ctx
                    )
                    # Name this variable distinctly from the metastore
                    # ``entries`` below so mypy doesn't try to unify a
                    # ``list[str]`` narrowed here with the
                    # ``list[FileMetadata]`` produced by ``metadata.list``.
                    external_entries: list[str] | None
                    if _virtual_entries is not None:
                        external_entries = list(_virtual_entries)
                    else:
                        external_entries = list(backend.list_dir(backend_path, context=_ctx))
                        # Mount-root listing (backend_path is empty or just
                        # "/") — inject the virtual ``.readme/`` subtree
                        # (flattened for recursive=True) so the doc overlay
                        # is discoverable from ``ls`` and also indexable by
                        # search/recursive walkers that only enumerate from
                        # the mount root (Issue #3728 findings #5, #8, #18).
                        #
                        # Round 7 finding #18: gate the injection on
                        # ownership — on a deferring backend whose real
                        # ``.readme/`` already exists, the overlay has
                        # handed the subtree over and we must not splice
                        # virtual entries back in.
                        readme_dir_name = _readme_dir_for(backend).strip("/")
                        from nexus.backends.connectors.schema_generator import (
                            overlay_owns_path as _overlay_owns_path,
                        )

                        try:
                            _overlay_owns_root = _overlay_owns_path(
                                backend,
                                mount_point,
                                readme_dir_name,
                                context=_ctx,
                            )
                        except ValueError:
                            _overlay_owns_root = False
                        if (
                            external_entries is not None
                            and _has_skill_name(backend)
                            and not backend_path.strip("/")
                            and _overlay_owns_root
                        ):
                            if recursive:
                                # Flatten the virtual tree to every leaf path
                                # so callers that recurse from the mount root
                                # (indexing, search, TUI tree) descend into
                                # README.md + schemas/ + examples/ without a
                                # second sys_readdir round-trip.
                                try:
                                    _vtree = get_virtual_readme_tree_for_backend(
                                        backend, mount_point
                                    )
                                except Exception:
                                    _vtree = None
                                if _vtree is not None:

                                    def _walk(node: Any, prefix: str) -> list[str]:
                                        out: list[str] = []
                                        if node.is_dir:
                                            # Include the directory itself
                                            if prefix:
                                                out.append(f"{prefix}/")
                                            for child_name, child in node.children.items():
                                                child_prefix = (
                                                    f"{prefix}/{child_name}"
                                                    if prefix
                                                    else child_name
                                                )
                                                out.extend(_walk(child, child_prefix))
                                        else:
                                            out.append(prefix)
                                        return out

                                    flattened = _walk(_vtree, readme_dir_name)
                                    for rel in flattened:
                                        if rel not in external_entries:
                                            external_entries.append(rel)
                            else:
                                virtual_entry = f"{readme_dir_name}/"
                                if (
                                    virtual_entry not in external_entries
                                    and readme_dir_name not in external_entries
                                ):
                                    external_entries.append(virtual_entry)
                    if external_entries is not None:
                        if details:
                            return [
                                {
                                    "path": f"{path.rstrip('/')}/{e}"
                                    if not e.startswith("/")
                                    else e,
                                    "name": e.rstrip("/").rsplit("/", 1)[-1],
                                    "is_directory": e.endswith("/"),
                                    "size": 0,
                                }
                                for e in external_entries
                            ]
                        return [
                            f"{path.rstrip('/')}/{e}" if not e.startswith("/") else e
                            for e in external_entries
                        ]
            except Exception as exc:
                logger.debug("sys_readdir connector route failed for %s: %s", path, exc)

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

        # Issue #3706: Use list_iter() instead of list() to avoid creating a
        # second filtered copy in Python and to bypass RustMetastoreProxy's
        # _dcache (prevents unbounded cache growth).  Note: the underlying
        # Rust/Raft engines still materialise the full result set internally;
        # true streaming requires a Rust-level paginated API (future work).
        entries_iter = (
            e
            for e in self.metadata.list_iter(prefix=prefix, recursive=recursive)
            if not self._is_internal_path(e.path)
        )
        if details:
            return [self._entry_to_detail_dict(e, recursive) for e in entries_iter]
        return [e.path for e in entries_iter]

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
        """Flush the write observer so pending version/audit records are committed.

        The RecordStoreWriteObserver accumulates events dispatched by the
        Rust kernel and flushes them to RecordStore in debounced batches.
        This method forces an immediate flush, guaranteeing that subsequent
        queries (e.g. list_versions) see the data.

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

    # Pipe/stream methods in nexus_fs_ipc.py (IPCMixin)

    def aclose(self) -> None:
        """Shutdown: stop PersistentService + unregister hooks, then close.

        Calls coordinator lifecycle methods first, then
        delegates to close() for sync resource cleanup.
        """
        # Issue #3391: drain deferred OBSERVE background tasks before tearing down.
        self.shutdown()

        coord = self.service_coordinator
        if coord is not None:
            coord.stop_persistent_services()
            coord._unregister_all_hooks()
        self.close()

    # ── IPC primitives (inlined from IPCMixin) ─────────────────────────

    def _pipe_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_PIPE — nowait hot path + Rust blocking slow path (GIL-free)."""
        # Hot path: try nowait first (zero GIL)
        _data = self._kernel.pipe_read_nowait(path)
        if _data is not None:
            if offset or count is not None:
                _data = _data[offset : offset + count] if count is not None else _data[offset:]
            return bytes(_data)

        # Custom backend fallback (SHM/remote)
        _buf = self._custom_pipe_backends.get(path)
        if _buf is not None:
            from nexus.core.pipe import PipeClosedError, PipeEmptyError

            try:
                data: bytes = _buf.read_nowait()
            except PipeEmptyError:
                # [TRANSITIONAL] Sync blocking: custom async backends run in temp event loop.
                # Eliminated when all pipe backends migrate to Rust.
                data = asyncio.run(_buf.read(blocking=True))
            except PipeClosedError:
                raise NexusFileNotFoundError(path, f"Pipe closed: {path}") from None
            if offset or count is not None:
                data = data[offset : offset + count] if count is not None else data[offset:]
            return data

        # Slow path: block in Rust (GIL released by PyO3), 5s timeout
        _data = self._kernel.pipe_read_blocking(path, 5000)
        if offset or count is not None:
            _data = _data[offset : offset + count] if count is not None else _data[offset:]
        return bytes(_data)

    def _pipe_write(self, path: str, data: bytes) -> int:
        """Write to DT_PIPE — non-blocking via Rust kernel (condvar wakes readers)."""
        return self._kernel.pipe_write_nowait(path, data)

    def _pipe_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_PIPE — close Rust buffer + clean up Python state."""
        import contextlib

        with contextlib.suppress(Exception):
            self._kernel.destroy_pipe(path)
        _buf = self._custom_pipe_backends.pop(path, None)
        if _buf is not None:
            _buf.close()
        return {}

    # ------------------------------------------------------------------
    # Tier 2 public sync pipe methods (kernel passthroughs)
    # ------------------------------------------------------------------
    # These are sync because the underlying Rust kernel calls are sync.
    # They exist so callers don't need to reach into ``self._kernel`` —
    # convenience wrappers, not first-class syscalls (no Tier 1 ``sys_*``
    # name). Used by coalescing consumers (audit drain, dedup work queue)
    # and sync teardown contexts (AcpService) where async wrapping would
    # add event-loop ping-pong without buying anything.

    def pipe_read_nowait(self, path: str) -> bytes | None:
        """Non-blocking pipe read. Returns ``None`` if pipe is empty.

        Sync passthrough to ``Kernel.pipe_read_nowait``.
        """
        return self._kernel.pipe_read_nowait(path)

    def pipe_write_nowait(self, path: str, data: bytes) -> int:
        """Non-blocking pipe write. Returns bytes written.

        Sync passthrough to ``Kernel.pipe_write_nowait``.
        """
        return self._kernel.pipe_write_nowait(path, data)

    def pipe_create(self, path: str, capacity: int = 65_536) -> None:
        """Create a DT_PIPE in the kernel registry.

        Sync passthrough to ``Kernel.create_pipe``.
        """
        self._kernel.create_pipe(path, capacity)

    def pipe_close(self, path: str) -> None:
        """Mark a DT_PIPE as closed (signals EOF to readers, keeps registry entry)."""
        self._kernel.close_pipe(path)

    def has_pipe(self, path: str) -> bool:
        """Check if a DT_PIPE exists in the kernel registry."""
        if self._kernel is None:
            return False
        return self._kernel.has_pipe(path)

    def pipe_destroy(self, path: str) -> None:
        """Destroy a DT_PIPE — close Rust kernel buffer + custom backend cleanup.

        Sync alternative to ``await sys_unlink(path)`` for sync teardown
        contexts that don't need full metastore/dcache cleanup. Internally
        delegates to the existing ``_pipe_destroy()`` helper.
        """
        self._pipe_destroy(path)

    # ------------------------------------------------------------------
    # Tier 2 public sync stream methods (kernel passthroughs)
    # ------------------------------------------------------------------
    # Stream counterparts to the pipe convenience methods above. Used by
    # LLM streaming backends (CASOpenAIBackend) where a tight token-pump
    # loop calls ``stream_write_nowait`` per token and ``stream_read_at``
    # for offset-based replay — async wrapping would just add ping-pong.

    def stream_create(self, path: str, capacity: int = 65_536) -> None:
        """Create a DT_STREAM in the kernel registry."""
        self._kernel.create_stream(path, capacity)

    def has_stream(self, path: str) -> bool:
        """Check if a DT_STREAM exists in the kernel registry."""
        if self._kernel is None:
            return False
        return self._kernel.has_stream(path)

    def stream_read_at_blocking(self, path: str, offset: int, timeout_ms: int) -> tuple[bytes, int]:
        """Blocking offset-based stream read. Returns (chunk, next_offset).

        Releases the GIL inside Rust during the wait. Callers that need
        async semantics should wrap in ``asyncio.to_thread``.
        """
        _data, _next = self._kernel.stream_read_at_blocking(path, offset, timeout_ms)
        return (bytes(_data), _next)

    def stream_write_nowait(self, path: str, data: bytes) -> int:
        """Non-blocking stream append. Returns byte offset."""
        return self._kernel.stream_write_nowait(path, data)

    def stream_read_at(self, path: str, offset: int) -> tuple[bytes, int] | None:
        """Non-blocking offset-based stream read. Returns (chunk, next_offset) or None."""
        _result = self._kernel.stream_read_at(path, offset)
        if _result is None:
            return None
        return (bytes(_result[0]), _result[1])

    def stream_collect_all(self, path: str) -> bytes:
        """Collect all message payloads from a DT_STREAM, concatenated.

        Single Rust call — no per-frame PyO3 round-trip. Replaces
        manual ``read_at`` loops in LLM backends.
        """
        return bytes(self._kernel.stream_collect_all(path))

    def stream_close(self, path: str) -> None:
        """Mark a DT_STREAM as closed (signals EOF to readers)."""
        self._kernel.close_stream(path)

    def stream_destroy(self, path: str) -> None:
        """Destroy a DT_STREAM — close kernel buffer + custom backend cleanup.

        Sync alternative to ``await sys_unlink(path)``.
        """
        self._stream_destroy(path)

    def _stream_read(self, path: str, *, count: int | None = None, offset: int = 0) -> bytes:
        """Read from DT_STREAM — nowait hot path + Rust blocking slow path (GIL-free)."""
        # Hot path: try nowait first (zero GIL)
        _result = self._kernel.stream_read_at(path, offset)
        if _result is not None:
            return bytes(_result[0])

        # Custom backend fallback (async stream backends bridged via run_sync)
        _buf = self._custom_stream_backends.get(path)
        if _buf is not None:
            from nexus.core.stream import StreamClosedError, StreamEmptyError
            from nexus.lib.sync_bridge import run_sync

            try:
                if count is not None and count > 1:
                    items, _ = run_sync(_buf.read_batch_blocking(offset, count, blocking=True))
                    return b"".join(items)
                sdata: bytes
                sdata, _ = run_sync(_buf.read(offset, blocking=True))
                return sdata
            except StreamEmptyError:
                raise NexusFileNotFoundError(path, f"Stream empty at offset {offset}") from None
            except StreamClosedError:
                raise NexusFileNotFoundError(path, f"Stream closed: {path}") from None

        # Slow path: block in Rust, release GIL
        _data, _next = self._kernel.stream_read_at_blocking(path, offset, 30000)
        return bytes(_data)

    def _stream_write(self, path: str, data: bytes) -> int:
        """Write to DT_STREAM — non-blocking via Rust kernel (condvar wakes readers), returns byte offset."""
        return self._kernel.stream_write_nowait(path, data)

    def _stream_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_STREAM — close Rust buffer + clean up Python state."""
        import contextlib

        with contextlib.suppress(Exception):
            self._kernel.destroy_stream(path)
        _buf = self._custom_stream_backends.pop(path, None)
        if _buf is not None:
            _buf.close()
        return {}

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

        # Close IPC primitives — Rust kernel (§4.2)
        # _kernel is None in remote connection mode (no local kernel)
        if self._kernel is not None:
            self._kernel.close_all_pipes()
            self._kernel.close_all_streams()
        self._custom_pipe_backends.clear()
        self._custom_stream_backends.clear()
        # Close transport pool (persistent gRPC connections)
        if hasattr(self, "_transport_pool") and self._transport_pool is not None:
            self._transport_pool.close_all()

        # Auto-close all enlisted services that have a close() method
        # (rebac_manager, audit_store, etc.). Reverse registration order.
        self._service_registry.close_all_services()

        # Close metadata store
        self.metadata.close()

        # Close record store (Services layer SQL connections)
        if self._record_store is not None:
            self._record_store.close()

        # Close mounted backends that hold resources (e.g., OAuth connectors with SQLite)
        if hasattr(self, "router"):
            from nexus.core.protocols.connector import OAuthCapableProtocol

            for mp in self.router.get_mount_points():
                try:
                    route = self.router.route(mp, is_admin=True, zone_id=self._zone_id)
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

    def __enter__(self) -> "NexusFS":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Context manager exit."""
        self.close()

    # ── Tier 2: Lock Convenience (moved from ABC) ────────────────

    def lock(
        self,
        path: str,
        mode: str = "exclusive",
        timeout: float = 30.0,
        ttl: float = 60.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> str | None:
        """Acquire lock with blocking wait (Tier 2 over sys_lock).

        Retries sys_lock() until acquired or timeout.
        Like fcntl(F_SETLKW) — blocking variant of sys_lock (F_SETLK).
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        while True:
            lock_id = self.sys_lock(
                path,
                mode=mode,
                ttl=ttl,
                max_holders=max_holders,
                context=context,
            )
            if lock_id is not None:
                return lock_id
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return None
            _time.sleep(min(0.05, remaining))

    def unlock(self, lock_id: str, path: str, *, context: "OperationContext | None" = None) -> bool:
        """Release lock (Tier 2 alias for sys_unlock)."""
        return self.sys_unlock(path, lock_id, context=context)

    @contextlib.contextmanager
    def locked(
        self,
        path: str,
        mode: str = "exclusive",
        timeout: float = 30.0,
        ttl: float = 30.0,
        max_holders: int = 1,
        *,
        context: "OperationContext | None" = None,
    ) -> "Generator[str, None, None]":
        """Context manager for advisory lock (Tier 2).

        Acquires lock via lock() (blocking wait), yields lock_id,
        releases on exit. Raises LockTimeout on failure.
        """
        from nexus.contracts.exceptions import LockTimeout

        lock_id = self.lock(
            path,
            mode=mode,
            timeout=timeout,
            ttl=ttl,
            max_holders=max_holders,
            context=context,
        )
        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)
        try:
            yield lock_id
        finally:
            self.unlock(lock_id, path, context=context)

    # ── Tier 2: glob/grep (moved from ABC) ────────────────────────

    def glob(self, pattern: str, path: str = "/", context: Any = None) -> builtins.list[str]:
        """Find files matching a glob pattern (like glob(3)).

        Requires SearchService.
        """
        raise NotImplementedError("glob requires SearchService")

    def grep(
        self,
        pattern: str,
        path: str = "/",
        file_pattern: str | None = None,
        ignore_case: bool = False,
        max_results: int = 1000,
        search_mode: str = "auto",
        context: Any = None,
        before_context: int = 0,
        after_context: int = 0,
        invert_match: bool = False,
    ) -> builtins.list[dict[str, Any]]:
        """Search file contents using regex patterns (like grep(1)).

        Requires SearchService.
        """
        raise NotImplementedError("grep requires SearchService")
