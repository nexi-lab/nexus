"""Unified filesystem implementation for Nexus."""

import builtins
import contextlib
import inspect
import logging
import time
from collections.abc import Callable, Generator, Iterator
from dataclasses import replace as _dc_replace
from datetime import UTC, datetime
from typing import Any

from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    BackendError,
    ConflictError,
    InvalidPathError,
    NexusFileNotFoundError,
)
from nexus.contracts.filesystem.filesystem_abc import NexusFilesystemABC
from nexus.contracts.metadata import FileMetadata
from nexus.contracts.types import OperationContext, Permission
from nexus.core.config import (
    BrickServices,
    CacheConfig,
    DistributedConfig,
    KernelServices,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
    SystemServices,
)
from nexus.core.file_events import FileEvent, FileEventType
from nexus.core.hash_fast import hash_content
from nexus.core.metastore import MetastoreABC
from nexus.core.router import PathRouter
from nexus.lib.path_utils import validate_path
from nexus.lib.rpc_decorator import rpc_expose
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


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
        is_admin: bool = False,
        cache: CacheConfig | None = None,
        permissions: PermissionConfig | None = None,
        distributed: DistributedConfig | None = None,
        memory: MemoryConfig | None = None,
        parsing: ParseConfig | None = None,
        kernel_services: KernelServices | None = None,
        system_services: SystemServices | None = None,
        brick_services: BrickServices | None = None,
    ):
        """Initialize NexusFS kernel.

        Kernel boots with MetastoreABC (inode layer) and an optional router
        (via KernelServices). Backends are mounted externally via
        ``router.add_mount()`` — like Linux VFS, no global backend.
        """
        # Config defaults
        cache = cache or CacheConfig()
        permissions = permissions or PermissionConfig()
        distributed = distributed or DistributedConfig()
        memory = memory or MemoryConfig()
        parsing = parsing or ParseConfig()
        ksvc = kernel_services or KernelServices()
        sys_svc = system_services or SystemServices()
        brk_svc = brick_services or BrickServices()

        # Per-instance VFS revision counter (H21: must not be class-level)
        import threading as _threading

        self._vfs_revision: int = 0
        self._vfs_revision_lock = _threading.Lock()

        self._cache_config = cache
        self._perm_config = permissions
        self._distributed_config = distributed
        self._memory_config_obj = memory
        self._parse_config = parsing
        # Issue #1767: _kernel_services wrapper removed — only field was router,
        # which is already stored as self.router (set a few lines below).
        self._system_services = sys_svc
        self._brick_services = brk_svc
        self._config: Any | None = None

        # Map config fields to flat attributes
        self._enable_memory_paging = memory.enable_paging
        self._memory_main_capacity = memory.main_capacity
        self._memory_recall_max_age_hours = memory.recall_max_age_hours
        self._enforce_permissions = permissions.enforce
        self._enforce_zone_isolation = permissions.enforce_zone_isolation
        self.allow_admin_bypass = permissions.allow_admin_bypass
        self.is_admin = is_admin

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
        if ksvc.router is not None:
            self.router = ksvc.router
        else:
            self.router = PathRouter(metadata_store)

        # Default context for embedded mode
        self._default_context = OperationContext(
            user_id="anonymous",
            groups=[],
            zone_id=ROOT_ZONE_ID,
            agent_id=None,
            is_admin=is_admin,
            is_system=False,
            admin_capabilities=set(),
        )

        # Issue #1706: sentinel — real value wired by factory._do_link().
        # Kept as sentinel (not deleted) because 8 kernel methods access without hasattr guard.
        self._permission_enforcer: Any = None
        # Issue #1764: sentinel for kernel LSM-style hook (like _permission_enforcer).
        # Real value injected by factory._do_link() via _wired.descendant_checker.
        # Consider rename → _descendant_access_checker for clarity.
        self._descendant_checker: Any = None
        # overlay_resolver removed (Issue #2034) — always None, re-add when #1264 is implemented
        self._overlay_resolver = None
        # Issue #1791: factory-injected overlay config resolver (captures workspace_registry)
        self._overlay_config_fn: Callable[..., Any] | None = None
        # Issue #1788: distributed lock manager — kernel knows (like _permission_enforcer).
        # In-process locks use _vfs_lock_manager (kernel owns); distributed locks use this.
        self._distributed_lock_manager: Any = None
        # Non-hot-path service attrs wired by factory._do_link() (Issue #1570)

        # Lazy-init sentinels
        self._token_manager = None
        self._sandbox_manager: Any = None
        self._coordination_client: Any = None
        self._event_client: Any = None

        # VFS lock manager — kernel-internal, NOT injected via DI.
        # Analogous to Linux i_rwsem: always present, created by kernel at init.
        # Protects same-process coroutine concurrency on sys_write/sys_read.
        from nexus.core.lock_fast import create_vfs_lock_manager

        self._vfs_lock_manager = create_vfs_lock_manager()
        logger.info("VFS lock manager initialized (%s)", type(self._vfs_lock_manager).__name__)

        # Kernel notification dispatch (INTERCEPT + OBSERVE).
        # Kernel owns dispatch infrastructure — creates empty callback lists.
        # Factory registers hooks at boot (KERNEL-ARCHITECTURE §3).
        from nexus.core.kernel_dispatch import KernelDispatch

        self._dispatch: KernelDispatch = KernelDispatch()

        # IPC primitives — kernel-internal, NOT injected via DI.
        # Like VFSLockManager: always present, created by kernel at init.
        # Both use ROOT_ZONE_ID (zone_id is a federation concept, not kernel).
        import os as _os_ipc

        from nexus.core.pipe_manager import PipeManager
        from nexus.core.stream_manager import StreamManager

        _ipc_self_addr = _os_ipc.environ.get("NEXUS_ADVERTISE_ADDR")
        self._pipe_manager = PipeManager(
            metadata_store,
            self_address=_ipc_self_addr,
        )
        self._stream_manager = StreamManager(
            metadata_store,
            self_address=_ipc_self_addr,
        )
        logger.info(
            "IPC primitives initialized: PipeManager + StreamManager (self_address=%s)",
            _ipc_self_addr or "none/single-node",
        )

        # Service registry — /proc/modules of Nexus (Issue #1452).
        # Populated by factory via enlist_wired_services() at link().
        from nexus.core.service_registry import ServiceRegistry

        self._service_registry: ServiceRegistry = ServiceRegistry()

        # Lifecycle state — set by link() / initialize() / bootstrap()
        self._linked: bool = False
        self._initialized: bool = False
        self._bootstrapped: bool = False
        self._bootstrap_callbacks: list[Callable[[], Any]] = []
        self._close_callbacks: list[
            Callable[[], None]
        ] = []  # Issue #1793: factory-registered service close
        self._runtime_closeables: list[Any] = []
        # Factory-injected lifecycle implementations.
        # Keeps nexus.core free of nexus.factory / nexus.bricks imports.
        self._link_fn: Callable[..., Any] | None = None
        self._initialize_fn: Callable[..., Any] | None = None

    # =====================================================================
    # Lifecycle methods: link() → initialize() → bootstrap()
    # =====================================================================

    async def link(
        self,
        *,
        enabled_bricks: "frozenset[str] | None" = None,
        parsing: Any = None,
        workflow_engine: Any = None,
    ) -> None:
        """Phase 1: Wire service topology.  Pure memory — NO I/O.

        Implementation is injected via ``_link_fn`` by the factory layer,
        keeping the kernel free of factory/bricks imports.

        Idempotent — guarded by ``_linked`` flag.
        """
        if self._linked:
            return
        if self._link_fn is not None:
            await self._link_fn(
                self,
                enabled_bricks=enabled_bricks,
                parsing=parsing,
                workflow_engine=workflow_engine,
            )
        self._linked = True

    async def initialize(self) -> None:
        """Phase 2: One-time side effects.  NO background threads.

        Implementation is injected via ``_initialize_fn`` by the factory layer.

        Idempotent — guarded by ``_initialized`` flag.
        """
        if self._initialized:
            return
        if not self._linked:
            await self.link()
        if self._initialize_fn is not None:
            _result = self._initialize_fn(self)
            if inspect.isawaitable(_result):
                await _result
        self._initialized = True

    async def bootstrap(self) -> None:
        """Phase 3: Start async tasks.  Server/Worker only.

        Executes registered bootstrap callbacks.  Reserved for future
        server-specific active components (Feishu WS, EventBus consumers,
        background sync workers, etc.).

        Idempotent — guarded by ``_bootstrapped`` flag.
        """
        if self._bootstrapped:
            return
        if not self._initialized:
            await self.initialize()
        for cb in self._bootstrap_callbacks:
            await cb()
        # Auto-lifecycle: activate HotSwappable hooks, start PersistentService (Issue #1580)
        coord = self.service_coordinator
        if coord is not None:
            await coord.activate_hot_swappable_services()
            await coord.start_persistent_services()
            coord.mark_bootstrapped()  # future enlist() calls auto-start Q3
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
    def service_coordinator(self) -> Any | None:
        """ServiceLifecycleCoordinator (set by factory at initialize time)."""
        return getattr(self, "_service_coordinator", None)

    async def swap_service(self, name: str, new_instance: Any, **kwargs: Any) -> None:
        """Hot-swap a service: atomic replace → drain → hook swap.

        Requires system services to be available (coordinator must exist).
        """
        coord = self.service_coordinator
        if coord is None:
            raise RuntimeError("Service hot-swap requires system services (no coordinator)")
        await coord.swap_service(name, new_instance, **kwargs)

    @property
    def namespace_manager(self) -> Any | None:
        """Public accessor for the NamespaceManager (via PermissionEnforcer)."""
        enforcer = self._permission_enforcer
        if enforcer is not None:
            return getattr(enforcer, "namespace_manager", None)
        return None

    @property
    def config(self) -> Any | None:
        """Public accessor for the runtime configuration object."""
        return self._config

    def _get_created_by(self, context: OperationContext | dict | None = None) -> str | None:
        """Get the created_by value for version history tracking."""
        from nexus.lib.context_utils import get_created_by

        return get_created_by(context, self._default_context)

    def _get_context_identity(
        self, context: OperationContext | dict | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract (zone_id, agent_id, is_admin) from context."""
        if context is None:
            return (
                self._default_context.zone_id,
                self._default_context.agent_id,
                self._default_context.is_admin,
            )
        if isinstance(context, dict):
            return (
                context.get("zone_id", self._default_context.zone_id),
                context.get("agent_id", self._default_context.agent_id),
                context.get("is_admin", self.is_admin),
            )
        return context.zone_id, context.agent_id, getattr(context, "is_admin", self.is_admin)

    # Issue #1790: _check_zone_writable() deleted — now handled by
    # ZoneWriteGuardHook (pre-intercept on all write-like operations).
    # Kernel no longer reads zone_lifecycle from _system_services.

    @property
    def zone_id(self) -> str | None:
        """Default zone_id from the instance context."""
        return self._default_context.zone_id

    @property
    def agent_id(self) -> str | None:
        """Default agent_id from the instance context."""
        return self._default_context.agent_id

    @property
    def user_id(self) -> str | None:
        """Default user_id from the instance context."""
        return getattr(self._default_context, "user_id", None)

    def _parse_context(self, context: OperationContext | dict | None = None) -> OperationContext:
        """Parse context dict or OperationContext into OperationContext."""
        from nexus.lib.context_utils import parse_context

        return parse_context(context)

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

        This is factored out of ``sys_mkdir`` so it can be called both on the
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
            self._create_directory_metadata(parent_dir, context=ctx)
            _hier = (
                getattr(self._system_services, "hierarchy_manager", None)
                if self._system_services
                else None
            )
            if _hier is not None:
                try:
                    logger.debug(
                        f"mkdir: Creating parent tuples for intermediate dir: {parent_dir}"
                    )
                    _hier.ensure_parent_tuples(parent_dir, zone_id=ctx.zone_id or ROOT_ZONE_ID)
                except Exception as e:
                    logger.warning(
                        "mkdir: Failed to create parent tuples for %s: %s", parent_dir, e
                    )

    def _create_directory_metadata(
        self, path: str, context: OperationContext | None = None
    ) -> None:
        """
        Create metadata entry for a directory.

        Args:
            path: Virtual path to directory
            context: Operation context (for zone_id and created_by)
        """
        now = datetime.now(UTC)

        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Note: UNIX permissions (owner/group/mode) are deprecated.
        # All permissions are now managed through ReBAC relationships.
        # We no longer inherit or store UNIX permissions in metadata.

        # Create a marker for the directory in metadata
        # We use an empty content hash as a placeholder
        empty_hash = hash_content(b"")

        # Route to find which backend owns this path
        route = self.router.route(path, is_admin=ctx.is_admin)

        metadata = FileMetadata(
            path=path,
            backend_name=route.backend.name,
            physical_path=empty_hash,  # Placeholder for directory
            size=0,  # Directories have size 0
            etag=empty_hash,
            mime_type="inode/directory",  # MIME type for directories
            created_at=now,
            modified_at=now,
            version=1,
            created_by=self._get_created_by(context),  # Track who created this directory
            zone_id=ctx.zone_id or ROOT_ZONE_ID,  # P0 SECURITY: Set zone_id
        )

        self.metadata.put(metadata)

    # === Directory Operations ===

    @rpc_expose(description="Create directory")
    async def sys_mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory (parents=True for mkdir -p)."""
        path = self._validate_path(path)

        # Use provided context or default
        ctx = context if context is not None else self._default_context

        # Block writes during zone deprovisioning (Issue #2061)

        # PRE-INTERCEPT: pre-mkdir hooks (Issue #899)
        from nexus.contracts.vfs_hooks import MkdirHookContext

        self._dispatch.intercept_pre_mkdir(MkdirHookContext(path=path, context=ctx))

        # Route to backend with write access check (mkdir requires write permission)
        route = self.router.route(
            path,
            is_admin=ctx.is_admin,
            check_write=True,
        )

        # Check if path is read-only
        if route.readonly:
            raise PermissionError(f"Cannot create directory in read-only path: {path}")

        # Check if directory already exists (either as file or implicit directory)
        existing = self.metadata.get(path)
        is_implicit_dir = existing is None and self.metadata.is_implicit_directory(path)

        if existing is not None or is_implicit_dir:
            # When parents=True, behave like mkdir -p (don't raise error if exists)
            if not exist_ok and not parents:
                raise FileExistsError(f"Directory already exists: {path}")
            # If exist_ok=True (or parents=True) and directory exists, we still
            # need to create parent directory metadata entries.  DT_MOUNT entries
            # are created by PathRouter.add_mount() *before* sys_mkdir is called,
            # so the target path already exists in metastore but the parent
            # directories (e.g. /mnt for /mnt/test) have no metadata yet.
            if existing is not None:
                if parents:
                    self._ensure_parent_directories(path, ctx)
                return

        # Create directory in backend
        route.backend.mkdir(route.backend_path, parents=parents, exist_ok=True, context=ctx)

        # Create metadata entries for parent directories if parents=True
        if parents:
            self._ensure_parent_directories(path, ctx)

        # Create explicit metadata entry for the directory
        self._create_directory_metadata(path, context=ctx)

        # P0-3: Create parent relationship tuples for directory inheritance
        # This enables granting access to /workspace to automatically grant access to subdirectories

        logger.debug(
            f"mkdir: Checking for hierarchy_manager: hasattr={hasattr(self, '_hierarchy_manager')}"
        )

        ctx = context or self._default_context

        _hier = (
            getattr(self._system_services, "hierarchy_manager", None)
            if self._system_services
            else None
        )
        if _hier is not None:
            try:
                logger.debug(
                    f"mkdir: Calling ensure_parent_tuples for {path}, zone_id={ctx.zone_id or ROOT_ZONE_ID}"
                )
                created_count = _hier.ensure_parent_tuples(
                    path, zone_id=ctx.zone_id or ROOT_ZONE_ID
                )
                logger.debug("mkdir: Created %d parent tuples for %s", created_count, path)
                if created_count > 0:
                    logger.debug("Created %d parent tuples for %s", created_count, path)
            except Exception as e:
                # Log the error but don't fail the mkdir operation
                # This helps diagnose issues with parent tuple creation
                logger.warning(
                    f"Failed to create parent tuples for {path}: {type(e).__name__}: {e}"
                )
                import traceback

                logger.debug(traceback.format_exc())

        # Grant direct_owner permission to the user who created the directory
        # Note: Use 'direct_owner' (not 'owner') as the base relation.
        # 'owner' is a computed union of direct_owner + parent_owner in the ReBAC schema.
        _rebac = (
            getattr(self._system_services, "rebac_manager", None) if self._system_services else None
        )
        if _rebac and ctx.user_id and not ctx.is_system:
            try:
                logger.debug(
                    "mkdir: Granting direct_owner permission to %s for %s", ctx.user_id, path
                )
                _rebac.rebac_write(
                    subject=("user", ctx.user_id),
                    relation="direct_owner",
                    object=("file", path),
                    zone_id=ctx.zone_id or ROOT_ZONE_ID,
                )
                logger.debug(
                    "mkdir: Granted direct_owner permission to %s for %s", ctx.user_id, path
                )
            except Exception as e:
                logger.warning("Failed to grant direct_owner permission for %s: %s", path, e)

        # Issue #900: Unified two-phase dispatch for mkdir
        from nexus.contracts.vfs_hooks import MkdirHookContext

        await self._dispatch.intercept_post_mkdir(
            MkdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
            )
        )
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_CREATE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Remove directory")
    async def sys_rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
        *,
        subject: tuple[str, str] | None = None,
        zone_id: str | None = None,
        agent_id: str | None = None,
        is_admin: bool | None = None,
    ) -> None:
        """Remove a directory (recursive=True for rm -rf)."""
        import errno

        path = self._validate_path(path)

        # Block writes during zone deprovisioning (Issue #2061)

        # P0 Fixes: Create OperationContext
        if context is not None:
            ctx = (
                context
                if isinstance(context, OperationContext)
                else OperationContext(
                    user_id=context.user_id,
                    groups=context.groups,
                    zone_id=context.zone_id or zone_id,
                    agent_id=context.agent_id or agent_id,
                    is_admin=context.is_admin if is_admin is None else is_admin,
                    is_system=context.is_system,
                    admin_capabilities=set(),
                )
            )
        elif subject is not None:
            ctx = OperationContext(
                user_id=subject[1],
                groups=[],
                zone_id=zone_id,
                agent_id=agent_id,
                is_admin=is_admin or False,
                is_system=False,
                admin_capabilities=set(),
            )
        else:
            ctx = (
                self._default_context
                if isinstance(self._default_context, OperationContext)
                else OperationContext(
                    user_id=self._default_context.user_id,
                    groups=self._default_context.groups,
                    zone_id=zone_id or self._default_context.zone_id,
                    agent_id=agent_id or self._default_context.agent_id,
                    is_admin=(is_admin if is_admin is not None else self._default_context.is_admin),
                    is_system=self._default_context.is_system,
                    admin_capabilities=set(),
                )
            )

        # Check write permission on directory

        logger.debug(
            f"rmdir: path={path}, recursive={recursive}, user={ctx.user_id}, is_admin={ctx.is_admin}"
        )
        from nexus.contracts.vfs_hooks import RmdirHookContext

        self._dispatch.intercept_pre_rmdir(RmdirHookContext(path=path, context=ctx))
        logger.debug("  -> PRE-INTERCEPT passed for rmdir on %s", path)

        # Route to backend with write access check (rmdir requires write permission)
        route = self.router.route(
            path,
            is_admin=ctx.is_admin,
            check_write=True,
        )

        # Check readonly
        if route.readonly:
            raise PermissionError(f"Cannot remove directory from read-only path: {path}")

        # Check if directory contains any files in metadata store
        # Normalize path to ensure it ends with /
        dir_path = path if path.endswith("/") else path + "/"
        files_in_dir = self.metadata.list(dir_path)

        if files_in_dir:
            # Directory is not empty
            if not recursive:
                # Raise OSError with ENOTEMPTY errno (same as os.rmdir behavior)
                raise OSError(errno.ENOTEMPTY, f"Directory not empty: {path}")

            # Recursive mode - delete all files in directory
            # Use batch delete for better performance (single transaction instead of N queries)
            file_paths = [file_meta.path for file_meta in files_in_dir]

            # Delete content from backend for each file
            _errors: list[str] = []
            for file_meta in files_in_dir:
                if file_meta.etag:
                    try:
                        _file_route = self.router.route(file_meta.path)
                        if ctx:
                            _del_ctx = _dc_replace(ctx, backend_path=_file_route.backend_path)
                        else:
                            _del_ctx = OperationContext(
                                user_id="anonymous",
                                groups=[],
                                backend_path=_file_route.backend_path,
                            )
                        _file_route.backend.delete_content(file_meta.etag, context=_del_ctx)
                    except Exception as e:
                        if len(_errors) < 100:
                            _errors.append(f"{file_meta.path}: {e}")
            if _errors:
                logger.debug(
                    "Bulk content delete: %d error(s) (showing up to 100): %s",
                    len(_errors),
                    "; ".join(_errors),
                )

            # Batch delete from metadata store
            self.metadata.delete_batch(file_paths)

        # Remove directory in backend (if it still exists)
        # In CAS systems, the directory may no longer exist after deleting its contents
        with contextlib.suppress(NexusFileNotFoundError):
            route.backend.rmdir(route.backend_path, recursive=recursive)

        # Also delete the directory's own metadata entry if it exists
        # Directories can have metadata entries (created by mkdir)
        try:
            self.metadata.delete(path)
        except Exception as e:
            logger.debug("Failed to delete directory metadata for %s: %s", path, e)

        # Clean up sparse directory index entries (Issue: rmdir not cleaning directory index)
        # This removes entries from DirectoryEntryModel used by non-recursive list()
        if hasattr(self.metadata, "delete_directory_entries_recursive"):
            try:
                self.metadata.delete_directory_entries_recursive(path)
            except Exception as e:
                logger.debug("Failed to clean up directory index for %s: %s", path, e)

        from nexus.contracts.vfs_hooks import RmdirHookContext

        await self._dispatch.intercept_post_rmdir(
            RmdirHookContext(
                path=path,
                context=ctx,
                zone_id=ctx.zone_id,
                agent_id=ctx.agent_id,
                recursive=recursive,
            )
        )
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.DIR_DELETE,
                path=path,
                zone_id=ctx.zone_id or ROOT_ZONE_ID,
                agent_id=ctx.agent_id,
                user_id=ctx.user_id,
            )
        )

    @rpc_expose(description="Check if path is a directory")
    async def sys_is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if path is a directory (explicit or implicit).

        A path is considered a directory if any of the following hold:
        - It is an implicit directory (has children in metastore)
        - Its metastore entry has ``entry_type`` DT_DIR or DT_MOUNT
        - The backend reports it as a directory
        """
        try:
            path = self._validate_path(path)

            # Use provided context or default
            ctx = context if context is not None else self._default_context

            # Check if it's an implicit directory first (for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Check permission (with TRAVERSE optimization for implicit directories)
            if self._enforce_permissions:
                if is_implicit_dir:
                    # OPTIMIZATION: Try TRAVERSE permission first (O(1))
                    # Fall back to descendant access check if TRAVERSE denied
                    if not self._permission_enforcer.check(
                        path, Permission.TRAVERSE, ctx
                    ) and not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        return False
                else:
                    # For explicit directories/files, use hierarchical access check
                    if not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        return False

            # Check metastore entry_type: DT_DIR and DT_MOUNT are directories.
            # This is a fast ~5 us redb read that avoids calling into the backend.
            meta = self.metadata.get(path)
            if meta is not None and (meta.is_dir or meta.is_mount):
                return True

            # Route with access control (read permission needed to check)
            route = self.router.route(
                path,
                is_admin=ctx.is_admin,
                check_write=False,
            )
            # Check if it's an explicit directory in the backend
            if route.backend.is_directory(route.backend_path):
                return True
            # Return cached implicit directory status
            return is_implicit_dir
        except (InvalidPathError, Exception):
            return False

    @rpc_expose(description="Get available namespaces")
    def get_top_level_mounts(self) -> builtins.list[str]:
        """Return top-level mount names visible to the current user.

        Reads DT_MOUNT entries from metastore (kernel's single source of
        truth for mount points). Admin-only filtering uses the runtime
        mount table which carries mount options.
        """
        # Build admin_only set from runtime mount table (mount options)
        admin_only = {m.mount_point for m in self.router.list_mounts() if m.admin_only}

        names: set[str] = set()
        for meta in self.metadata.list("/"):
            if not meta.is_mount:
                continue
            top = meta.path.lstrip("/").split("/")[0]
            if not top:
                continue
            if meta.path in admin_only and not self.is_admin:
                continue
            names.add(top)
        return sorted(names)

    @rpc_expose(description="Get file metadata for FUSE operations")
    async def sys_stat(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> dict[str, Any] | None:
        """Get file metadata without reading content (FUSE getattr)."""
        ctx = context or self._default_context
        normalized = self._validate_path(path, allow_root=True)

        # Check if it's a directory first
        is_dir = await self.sys_is_directory(normalized, context=ctx)

        if is_dir:
            # Return directory metadata
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

        # Try to get file metadata from store
        file_meta = self.metadata.get(normalized)
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
        context: OperationContext | None = None,
        **attrs: Any,
    ) -> dict[str, Any]:
        """Upsert file metadata (chmod/chown/utimensat + mknod analog).

        Upsert semantics — create-on-write for metadata:
        - Path missing + entry_type provided → CREATE inode
        - Path missing + no entry_type → NexusFileNotFoundError
        - Path exists + no entry_type → UPDATE mutable fields
        - Path exists + entry_type → ValueError (immutable after creation)

        Args:
            path: Virtual file path.
            context: Operation context.
            **attrs: Metadata attributes. Include ``entry_type`` to create.

        Returns:
            Dict with path, created flag, and type-specific fields.
        """
        path = self._validate_path(path)

        meta = self.metadata.get(path)

        # --- CREATE path (inode doesn't exist + entry_type provided) ---
        if meta is None:
            entry_type = attrs.get("entry_type")
            if entry_type is None:
                raise NexusFileNotFoundError(path)
            return self._setattr_create(path, entry_type, attrs)

        # --- REJECT: entry_type is immutable after creation ---
        if "entry_type" in attrs:
            raise ValueError(f"entry_type is immutable after creation (current={meta.entry_type})")

        # --- UPDATE path (existing inode, mutable fields only) ---
        from dataclasses import replace

        _MUTABLE_FIELDS = frozenset({"mime_type", "modified_at"})
        valid_attrs = {k: v for k, v in attrs.items() if k in _MUTABLE_FIELDS}
        if not valid_attrs:
            return {"path": path, "created": False, "updated": []}

        new_meta = replace(meta, **valid_attrs)
        self.metadata.put(new_meta)
        return {"path": path, "created": False, "updated": list(valid_attrs.keys())}

    def _setattr_create(self, path: str, entry_type: int, attrs: dict[str, Any]) -> dict[str, Any]:
        """Create an inode via sys_setattr upsert — dispatches by entry_type."""
        from nexus.contracts.metadata import DT_PIPE, DT_STREAM

        capacity = attrs.get("capacity", 65_536)
        owner_id = attrs.get("owner_id")

        if entry_type == DT_PIPE:
            from nexus.core.pipe import PipeError

            try:
                self._pipe_manager.create(path, capacity=capacity, owner_id=owner_id)
            except PipeError as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

        if entry_type == DT_STREAM:
            from nexus.core.stream import StreamError

            try:
                self._stream_manager.create(path, capacity=capacity, owner_id=owner_id)
            except StreamError as exc:
                raise BackendError(str(exc)) from exc
            return {"path": path, "created": True, "entry_type": entry_type, "capacity": capacity}

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

        Issue #1791: Delegates to factory-injected callable. Kernel does NOT
        read workspace_registry from _system_services — the factory captures
        the registry reference in a closure at link() time.

        Returns:
            OverlayConfig if overlay active for this path, None otherwise
        """
        fn = self._overlay_config_fn
        return fn(path) if fn is not None else None

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
        handle = self._vfs_lock_manager.acquire(path, mode, timeout_ms=self._VFS_LOCK_TIMEOUT_MS)
        if handle == 0:
            from nexus.contracts.exceptions import LockTimeout

            raise LockTimeout(
                path=path,
                timeout=self._VFS_LOCK_TIMEOUT_MS / 1000,
                message=f"VFS {mode} lock timeout on {path}",
            )
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

    # ── Distributed lock helpers (sync bridge for write(lock=True)) ──

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: OperationContext | None,
    ) -> str | None:
        """Acquire distributed lock synchronously (for use in sync write()).

        This method bridges sync write() with async lock operations.
        For async contexts, use `async with locked()` instead.
        """
        import asyncio

        _lm = self._distributed_lock_manager
        if _lm is None:
            raise RuntimeError(
                "write(lock=True) called but distributed lock manager not configured. "
                "Ensure NexusFS is initialized with enable_distributed_locks=True."
            )

        from nexus.contracts.exceptions import LockTimeout

        try:
            asyncio.get_running_loop()
            raise RuntimeError(
                "write(lock=True) cannot be used from async context (event loop detected). "
                "Use `async with nx.events_service.locked(path):` and `write(lock=False)` instead."
            )
        except RuntimeError as e:
            if "event loop detected" in str(e):
                raise

        zone_id = (
            context.zone_id
            if context and hasattr(context, "zone_id") and context.zone_id
            else ROOT_ZONE_ID
        )

        async def acquire_lock() -> str | None:
            return await _lm.acquire(
                zone_id=zone_id,
                path=path,
                timeout=timeout,
            )

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
        """Release distributed lock synchronously."""
        if not lock_id:
            return

        _lm = self._distributed_lock_manager
        if _lm is None:
            return

        zone_id = (
            context.zone_id
            if context and hasattr(context, "zone_id") and context.zone_id
            else ROOT_ZONE_ID
        )

        async def release_lock() -> None:
            await _lm.release(lock_id, zone_id, path)

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
        path = self._validate_path(path)
        context = self._parse_context(context)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _resolve_hint = self._dispatch.resolve_read(
            path, return_metadata=False, context=context
        )
        if _handled:
            if isinstance(_resolve_hint, dict):
                _resolve_hint = _resolve_hint.get("content", b"")
            if isinstance(_resolve_hint, str):
                _resolve_hint = _resolve_hint.encode("utf-8")
            return (_resolve_hint, None, None, None, None)

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
        from nexus.core.router import PipeRouteResult, StreamRouteResult

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

        # Dynamic connector bypass
        _caps: frozenset[str] = getattr(route.backend, "capabilities", frozenset())
        is_dynamic_connector = (
            route.backend.user_scoped is True and route.backend.has_token_manager is True
        ) or "external_content" in _caps

        if is_dynamic_connector:
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
        path = self._validate_path(path)
        # Normalize context dict to OperationContext dataclass (CLI passes dicts)
        context = self._parse_context(context)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _resolve_hint = self._dispatch.resolve_read(
            path, return_metadata=False, context=context
        )
        if _handled:
            # Normalize resolver results to bytes
            if isinstance(_resolve_hint, dict):
                _resolve_hint = _resolve_hint.get("content", b"")
            if isinstance(_resolve_hint, str):
                _resolve_hint = _resolve_hint.encode("utf-8")
            content = _resolve_hint
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
        from nexus.core.router import PipeRouteResult, StreamRouteResult

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

        # Check if backend is a dynamic API-backed connector or external content source.
        # TODO(#899): Move this bypass logic out of kernel into service/composition layer.
        _caps: frozenset[str] = getattr(route.backend, "capabilities", frozenset())
        is_dynamic_connector = (
            route.backend.user_scoped is True and route.backend.has_token_manager is True
        ) or "external_content" in _caps

        if is_dynamic_connector:
            # Dynamic connector - read directly from backend without metadata check
            # The backend handles authentication and API calls (no VFS lock needed)
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

            if meta is None or meta.etag is None:
                raise NexusFileNotFoundError(path)

            # Issue #1264: Reject whiteout markers (file was deleted in overlay)
            if getattr(self, "_overlay_resolver", None) and self._overlay_resolver.is_whiteout(
                meta
            ):
                raise NexusFileNotFoundError(path)

            content = route.backend.read_content(meta.etag, context=read_context)

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

        # Batch permission check using filter_list
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
            # Skip permission check if permissions are disabled
            allowed_set = set(validated_paths)
        else:
            try:
                # Use the existing bulk permission check from list()
                # Note: filter_list assumes READ permission, which is what we want
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
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
                # Try parallel I/O for CASLocalBackend using nexus_fast
                if backend.supports_parallel_mmap_read is True and len(paths_for_backend) > 1:
                    # Use Rust parallel mmap reads for CASLocalBackend
                    try:
                        from nexus_fast import read_files_bulk

                        # Build mapping: disk_path -> (virtual_path, meta)
                        disk_to_virtual: dict[str, tuple[str, Any]] = {}
                        disk_paths: list[str] = []
                        for path in paths_for_backend:
                            meta, route = path_info[path]
                            assert meta.etag is not None
                            disk_path = str(backend.root_path / backend._blob_key(meta.etag))
                            disk_to_virtual[disk_path] = (path, meta)
                            disk_paths.append(disk_path)

                        # Parallel mmap read
                        logger.info(
                            f"[READ-BULK] Using parallel mmap for {len(disk_paths)} CASLocalBackend files"
                        )
                        disk_contents = read_files_bulk(disk_paths)

                        # Map results back to virtual paths
                        for disk_path, content in disk_contents.items():
                            vpath, meta = disk_to_virtual[disk_path]
                            assert meta is not None  # Guaranteed by check above
                            if return_metadata:
                                results[vpath] = {
                                    "content": content,
                                    "etag": meta.etag,
                                    "version": meta.version,
                                    "modified_at": meta.modified_at,
                                    "size": len(content),
                                }
                            else:
                                results[vpath] = content

                        # Mark missing files as None if skip_errors
                        for path in paths_for_backend:
                            if path not in results:
                                if skip_errors:
                                    results[path] = None
                                else:
                                    raise NexusFileNotFoundError(path)
                    except ImportError:
                        logger.warning(
                            "[READ-BULK] nexus_fast not available, falling back to sequential"
                        )
                        # Fall through to sequential reads below
                        for path in paths_for_backend:
                            if path in results:
                                continue
                            try:
                                meta, route = path_info[path]
                                assert meta.etag is not None
                                content = route.backend.read_content(meta.etag, context=None)
                                results[path] = (
                                    content
                                    if not return_metadata
                                    else {
                                        "content": content,
                                        "etag": meta.etag,
                                        "version": meta.version,
                                        "modified_at": meta.modified_at,
                                        "size": len(content),
                                    }
                                )
                            except Exception as exc:
                                logger.debug(
                                    "Failed to read content for %s during batch read: %s", path, exc
                                )
                                if skip_errors:
                                    results[path] = None
                                else:
                                    raise
                else:
                    # Sequential reads for other backends or single files
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
        _handled, _resolve_hint = self._dispatch.resolve_read(
            path, return_metadata=False, context=context
        )
        if _handled:
            if isinstance(_resolve_hint, dict):
                _resolve_hint = _resolve_hint.get("content", b"")
            if isinstance(_resolve_hint, str):
                _resolve_hint = _resolve_hint.encode("utf-8")
            return _resolve_hint[start:end]

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

        # Write content via streaming
        write_result = route.backend.write_stream(chunks, context=context)
        content_hash = write_result.content_hash

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
            created_by=self._get_created_by(context),
            zone_id=zone_id or "root",  # Issue #904, #773: Store zone_id for PREWHERE filtering
        )

        self.metadata.put(new_meta)

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
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        lock: bool = False,
        lock_timeout: float = 30.0,
        consistency: str = "sc",
    ) -> dict[str, Any]:
        """Write content to a file (POSIX pwrite(2)).

        Kernel primitive — content-only with create-on-write semantics.
        CAS, locking, and OCC are driver/application concerns.

        Args:
            path: Virtual path to write.
            buf: File content as bytes or str (str will be UTF-8 encoded).
            count: Max bytes to write (None = len(buf)).
            offset: Byte offset to start writing at (currently ignored — whole-file).
            context: Optional operation context for permission checks.

        Returns:
            Dict with path, bytes_written, and created flag.

        Raises:
            InvalidPathError: If path is invalid
            BackendError: If write operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
        # Auto-convert str to bytes for convenience
        if isinstance(buf, str):
            buf = buf.encode("utf-8")

        # Apply count slicing if specified
        if count is not None:
            buf = buf[:count]

        # DT_PIPE fast-path: skip validate/metastore/dispatch (~400ns vs ~21μs)
        # PipeManager._buffers is the authoritative pipe registry; paths are
        # validated at pipe creation time, so re-validation is unnecessary.
        if self._pipe_manager is not None and path in self._pipe_manager._buffers:
            n = self._pipe_write(path, buf)
            return {"path": path, "bytes_written": n, "created": False}

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers (Issue #889)
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            base = {"path": path, "bytes_written": len(buf), "created": False}
            if isinstance(_result, dict):
                base.update(_result)
            return base

        # DT_PIPE / DT_STREAM: kernel-native IPC dispatch (§4.2)
        _meta = self.metadata.get(path)
        if _meta is not None and _meta.is_pipe:
            # Fallback for pipes not in PipeManager (e.g. federation remote pipes)
            n = self._pipe_write(path, buf)
            return {"path": path, "bytes_written": n, "created": False}
        if _meta is not None and _meta.is_stream:
            offset = self._stream_write(path, buf)
            return {"path": path, "bytes_written": len(buf), "created": False, "offset": offset}

        await self._write_internal(path=path, content=buf, context=context)
        return {"path": path, "bytes_written": len(buf), "created": _meta is None}

    # ── Tier 2 overrides (NexusFS-specific) ───────────────────────

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
            offset: Byte offset (currently ignored — whole-file).
            context: Operation context.

        Returns:
            Dict with metadata (etag, version, modified_at, size).
        """
        if isinstance(buf, str):
            buf = buf.encode("utf-8")
        if count is not None:
            buf = buf[:count]

        path = self._validate_path(path)

        # PRE-DISPATCH: virtual path resolvers
        _handled, _result = self._dispatch.resolve_write(path, buf)
        if _handled:
            return _result

        return await self._write_internal(path=path, content=buf, context=context)

    async def _write_internal(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None,
        if_match: str | None = None,
        if_none_match: bool = False,
        force: bool = False,
        consistency: str = "sc",
    ) -> dict[str, Any]:
        """Kernel write implementation — OCC-free.

        Performs content write + metadata update + event dispatch.
        OCC checks (if_match, if_none_match) are done by callers
        (write() convenience method or RPC handlers) BEFORE calling this.

        Used by both sys_write (returns int) and write() (returns dict).

        Issue #1323: OCC params removed from kernel write path.
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

        # External_content backends manage their own content storage.
        # Remote backends (RPC-based) also persist metadata on the remote server,
        # so we skip local metadata.put() to avoid overwriting.
        # Local external_content backends (e.g. LocalConnector) write content
        # to disk but do NOT manage metadata — we must persist metadata locally.
        _backend_caps: frozenset[str] = getattr(route.backend, "capabilities", frozenset())
        _is_remote = hasattr(route.backend, "_rpc_client") or "remote" in route.backend.name
        if "external_content" in _backend_caps:
            wr = route.backend.write_content(content, context=context)
            content_hash = wr.content_hash
            new_version = (meta.version + 1) if meta else 1
            ctx = context if context is not None else self._default_context
            owner_id = meta.owner_id if meta else (ctx.subject_id or ctx.user_id)
            metadata = FileMetadata(
                path=path,
                backend_name=route.backend.name,
                physical_path=content_hash,
                size=len(content),
                etag=content_hash,
                created_at=meta.created_at if meta else now,
                modified_at=now,
                version=new_version,
                created_by=self._get_created_by(context),
                zone_id=zone_id or "root",
                owner_id=owner_id,
            )
            # Local external_content backends need metadata persisted locally
            if not _is_remote:
                self.metadata.put(metadata, consistency=consistency)
        else:
            # VFS I/O Lock: exclusive write lock around backend write + metadata put.
            # Like Linux i_rwsem: held for I/O duration only, released before observers.
            with self._vfs_locked(path, "write"):
                content_hash = route.backend.write_content(content, context=context).content_hash

                # NOTE: sys_write does NOT release old content on overwrite.
                # HDFS/GFS pattern: content cleanup is async via background GC.
                # See: docs/architecture/federation-memo.md §7f Caveat 4.

                # Calculate new version number (increment if updating)
                new_version = (meta.version + 1) if meta else 1

                # Store metadata with content hash as both etag and physical_path
                # Issue #920: Set owner_id for O(1) permission checks (only on new files)
                ctx = context if context is not None else self._default_context
                owner_id = meta.owner_id if meta else (ctx.subject_id or ctx.user_id)

                metadata = FileMetadata(
                    path=path,
                    backend_name=route.backend.name,
                    physical_path=content_hash,
                    size=len(content),
                    etag=content_hash,
                    created_at=meta.created_at if meta else now,
                    modified_at=now,
                    version=new_version,
                    created_by=self._get_created_by(context),
                    zone_id=zone_id or "root",  # Issue #904, #773: pre-existing default
                    owner_id=owner_id,
                )

                self.metadata.put(metadata, consistency=consistency)

        # --- Lock released — event dispatch + side effects (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        await self._dispatch.notify(
            FileEvent(
                type=FileEventType.FILE_WRITE,
                path=path,
                zone_id=zone_id or ROOT_ZONE_ID,
                agent_id=agent_id,
                etag=content_hash,
                size=len(content),
                version=new_version,
                is_new=(meta is None),
            )
        )

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        from nexus.contracts.vfs_hooks import WriteHookContext

        _write_ctx = WriteHookContext(
            path=path,
            content=content,
            context=context,
            zone_id=zone_id,
            agent_id=agent_id,
            is_new_file=(meta is None),
            content_hash=content_hash,
            metadata=metadata,
            old_metadata=meta,
            new_version=new_version,
        )
        await self._dispatch.intercept_post_write(_write_ctx)

        # Return metadata for optimistic concurrency control
        return {
            "etag": content_hash,
            "version": new_version,
            "modified_at": now,
            "size": len(content),
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
        _lm = self._distributed_lock_manager
        if _lm is None:
            raise RuntimeError(
                "atomic_update() requires distributed lock manager. "
                "Set NEXUS_REDIS_URL environment variable "
                "or pass coordination_url to NexusFS constructor."
            )

        lock_id = await _lm.acquire(path, timeout=timeout, ttl=ttl)
        try:
            content = await self.sys_read(path, context=context)
            new_content = update_fn(content)
            return await self.write(path, new_content, context=context)
        finally:
            await _lm.release(lock_id, path)

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
            content_hash = route.backend.write_content(content, context=_write_ctx).content_hash

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
                created_by=getattr(self, "agent_id", None)
                or getattr(self, "user_id", None),  # Track who created/modified this version
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
            zone_id=zone_id,
            agent_id=agent_id,
        )

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
        for metadata in metadata_list:
            is_new = existing_metadata.get(metadata.path) is None
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
                )
            )

        # Issue #548: Create parent tuples and grant direct_owner for new files
        # This ensures agents can read files they create (via user inheritance)
        # PERF OPTIMIZATION: Use batch operations instead of individual calls (20x faster)
        ctx = context if context is not None else self._default_context
        zone_id_for_perms = ctx.zone_id or "root"

        # PERF: Batch hierarchy tuple creation (single transaction instead of N)
        _hierarchy_start = time.perf_counter()
        all_paths = [path for path, _ in validated_files]
        _hier = (
            getattr(self._system_services, "hierarchy_manager", None)
            if self._system_services
            else None
        )
        if _hier is not None and hasattr(_hier, "ensure_parent_tuples_batch"):
            try:
                created_count = _hier.ensure_parent_tuples_batch(
                    all_paths, zone_id=zone_id_for_perms
                )
                logger.info(
                    f"write_batch: Batch created {created_count} parent tuples for {len(all_paths)} files"
                )
            except Exception as e:
                logger.warning(
                    f"write_batch: Batch parent tuples failed, falling back to individual: {e}"
                )
                # Fallback to individual calls if batch fails
                for path in all_paths:
                    try:
                        _hier.ensure_parent_tuples(path, zone_id=zone_id_for_perms)
                    except Exception as e2:
                        logger.warning(
                            f"write_batch: Failed to create parent tuples for {path}: {e2}"
                        )
        elif _hier is not None:
            # No batch method available, use individual calls
            for path in all_paths:
                try:
                    _hier.ensure_parent_tuples(path, zone_id=zone_id_for_perms)
                except Exception as e:
                    logger.warning(
                        "write_batch: Failed to create parent tuples for %s: %s", path, e
                    )
        _hierarchy_elapsed = (time.perf_counter() - _hierarchy_start) * 1000

        # PERF: Batch direct_owner grants (single transaction instead of N)
        _rebac_start = time.perf_counter()
        _rebac = (
            getattr(self._system_services, "rebac_manager", None) if self._system_services else None
        )
        if _rebac and ctx.user_id and not ctx.is_system:
            # Collect all owner grants needed for new files
            owner_grants = []
            for (path, _), _meta in zip(validated_files, metadata_list, strict=False):
                is_new_file = existing_metadata.get(path) is None
                if is_new_file:
                    owner_grants.append(
                        {
                            "subject": ("user", ctx.user_id),
                            "relation": "direct_owner",
                            "object": ("file", path),
                            "zone_id": zone_id_for_perms,
                        }
                    )

            if owner_grants and hasattr(_rebac, "rebac_write_batch"):
                try:
                    grant_count = _rebac.rebac_write_batch(owner_grants)
                    logger.info("write_batch: Batch granted direct_owner to %d files", grant_count)
                except Exception as e:
                    logger.warning(
                        f"write_batch: Batch rebac_write failed, falling back to individual: {e}"
                    )
                    # Fallback to individual calls
                    for grant in owner_grants:
                        try:
                            _rebac.rebac_write(
                                subject=grant["subject"],
                                relation=grant["relation"],
                                object=grant["object"],
                                zone_id=grant["zone_id"],
                            )
                        except Exception as e2:
                            logger.warning("write_batch: Failed to grant direct_owner: %s", e2)
            elif owner_grants:
                # No batch method available, use individual calls
                for grant in owner_grants:
                    try:
                        _rebac.rebac_write(
                            subject=grant["subject"],
                            relation=grant["relation"],
                            object=grant["object"],
                            zone_id=grant["zone_id"],
                        )
                    except Exception as e:
                        logger.warning("write_batch: Failed to grant direct_owner: %s", e)
        _rebac_elapsed = (time.perf_counter() - _rebac_start) * 1000

        # Log detailed timing breakdown for performance analysis
        logger.warning(
            f"[WRITE-BATCH-PERF] files={len(validated_files)}, "
            f"hierarchy={_hierarchy_elapsed:.1f}ms, rebac={_rebac_elapsed:.1f}ms, "
            f"per_file_avg={(_hierarchy_elapsed + _rebac_elapsed) / len(validated_files):.1f}ms"
        )

        return results

    @rpc_expose(description="Delete file")
    async def sys_unlink(
        self, path: str, context: OperationContext | None = None
    ) -> dict[str, Any]:
        """
        Delete a file or memory.

        Removes file from backend and metadata store.
        Decrements reference count in CAS (only deletes when ref_count=0).

        Supports memory virtual paths.

        Args:
            path: Virtual path to delete (supports memory paths)
            context: Optional operation context for permission checks (uses default if not provided)

        Returns:
            Empty dict on success.

        Raises:
            NexusFileNotFoundError: If file doesn't exist
            InvalidPathError: If path is invalid
            BackendError: If delete operation fails
            AccessDeniedError: If access is denied (zone isolation or read-only namespace)
            PermissionError: If path is read-only or user doesn't have write permission
        """
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

        # PRE-INTERCEPT: pre-delete hooks (Issue #899)
        from nexus.contracts.vfs_hooks import DeleteHookContext as _DHC

        self._dispatch.intercept_pre_delete(_DHC(path=path, context=context))

        # Issue #900: Unified two-phase dispatch — INTERCEPT (observer + hooks)
        # Placed BEFORE physical content delete to preserve audit integrity.
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
        # Like Linux i_rwsem: held for I/O duration only, released before observers.
        with self._vfs_locked(path, "write"):
            if meta.etag and meta.mime_type != "inode/directory":
                # CAS file: delete from routed backend (decrements ref count)
                # Content is only physically deleted when ref_count reaches 0
                # Skip content deletion for directories (no CAS entry)
                # Add backend_path to context for path-based backends
                if context:
                    _del_ctx = _dc_replace(context, backend_path=route.backend_path)
                else:
                    _del_ctx = OperationContext(
                        user_id="anonymous", groups=[], backend_path=route.backend_path
                    )
                route.backend.delete_content(meta.etag, context=_del_ctx)

            # Remove from metadata
            self.metadata.delete(path)

        # --- Lock released — event dispatch (like Linux inotify after i_rwsem) ---

        # Issue #900: Unified two-phase dispatch — OBSERVE (fire-and-forget)
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

    @rpc_expose(description="Rename/move file")
    async def sys_rename(
        self, old_path: str, new_path: str, context: OperationContext | None = None
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

        # Check if source exists (explicit metadata or implicit directory)
        is_implicit_dir = not self.metadata.exists(
            old_path
        ) and self.metadata.is_implicit_directory(old_path)
        if not self.metadata.exists(old_path) and not is_implicit_dir:
            raise NexusFileNotFoundError(old_path)

        meta = self.metadata.get(old_path)

        # Check if destination already exists
        # For connector backends, also verify the file exists in backend storage
        # (metadata might be stale if previous operations failed)
        if self.metadata.exists(new_path):
            if new_route.backend.supports_rename is True:
                # Connector backend - verify file actually exists in storage
                # If metadata says it exists but storage doesn't, clean up stale metadata
                try:
                    # Check if this is a GCS connector backend (has bucket attribute)
                    # NOTE: bucket/blob access is GCS-specific, kept as hasattr for now
                    if (
                        hasattr(new_route.backend, "bucket")
                        and hasattr(new_route.backend, "_get_blob_path")
                        and new_route.backend.name == "path_gcs"
                    ):
                        # GCS-specific attributes (dynamically checked with hasattr above)
                        dest_blob = new_route.backend.bucket.blob(
                            new_route.backend._get_blob_path(new_route.backend_path)
                        )
                        if not dest_blob.exists():
                            # Stale metadata - clean it up
                            import logging

                            log = logging.getLogger(__name__)
                            log.warning(
                                f"Cleaning up stale metadata for {new_path} (file not in backend storage)"
                            )
                            self.metadata.delete(new_path)
                        else:
                            # File really exists
                            raise FileExistsError(f"Destination path already exists: {new_path}")
                    else:
                        # Not a GCS connector backend, just check metadata
                        raise FileExistsError(f"Destination path already exists: {new_path}")
                except AttributeError:
                    # Not a GCS connector backend, just check metadata
                    raise FileExistsError(f"Destination path already exists: {new_path}") from None
            else:
                # CAS backend - metadata is source of truth
                raise FileExistsError(f"Destination path already exists: {new_path}")

        # Check if this is a directory BEFORE renaming (important!)
        # After rename, the old path won't have children anymore
        # is_implicit_dir was already computed above - also check for explicit directory
        is_directory = is_implicit_dir or (meta and meta.mime_type == "inode/directory")

        # PRE-INTERCEPT: pre-rename hooks (Issue #900 / #1312)
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

        # VFS I/O Lock: exclusive write lock on BOTH paths (sorted order = deadlock-free).
        # Like Linux i_rwsem on both source and destination inodes.
        _first, _second = sorted([old_path, new_path])
        _h1 = self._vfs_acquire(_first, "write")
        try:
            _h2 = self._vfs_acquire(_second, "write") if _first != _second else 0
            try:
                # For path-based connector backends, move actual file/directory in storage
                # CAS backends have supports_rename = False
                if old_route.backend.supports_rename is True:
                    try:
                        old_route.backend.rename_file(
                            old_route.backend_path, new_route.backend_path
                        )
                    except FileExistsError:
                        raise
                    except Exception as e:
                        raise BackendError(
                            f"Failed to rename in backend: {e}",
                            backend=old_route.backend.name,
                        ) from e

                # Perform metadata rename (recursively for directories)
                self.metadata.rename_path(old_path, new_path)
            finally:
                if _h2:
                    self._vfs_lock_manager.release(_h2)
        finally:
            self._vfs_lock_manager.release(_h1)

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

        # Update ReBAC permissions to follow the renamed file/directory
        # This ensures permissions are preserved when files are moved
        logger.warning("[RENAME-REBAC] Starting ReBAC update: %s -> %s", old_path, new_path)
        _rebac = (
            getattr(self._system_services, "rebac_manager", None) if self._system_services else None
        )
        logger.warning(
            f"[RENAME-REBAC] has rebac_manager: {_rebac is not None}, is truthy: {bool(_rebac)}"
        )

        if _rebac:
            try:
                logger.warning(
                    f"[RENAME-REBAC] Calling update_object_path: old={old_path}, new={new_path}, is_dir={is_directory}"
                )

                # Update all ReBAC tuples that reference this path
                updated_count = _rebac.update_object_path(
                    old_path=old_path,
                    new_path=new_path,
                    object_type="file",
                    is_directory=is_directory,
                )

                # Log if any permissions were updated
                logger.warning(
                    f"[RENAME-REBAC] update_object_path returned: {updated_count} tuples updated"
                )
            except Exception as e:
                # Don't fail the rename operation if ReBAC update fails
                # The file is already renamed in metadata, we just couldn't update permissions
                logger.error(
                    f"[RENAME-REBAC] FAILED to update ReBAC permissions: {e}", exc_info=True
                )
        else:
            logger.warning("[RENAME-REBAC] SKIPPED - no rebac_manager available")

        # POST-INTERCEPT: post-rename hooks (Issue #900)
        await self._dispatch.intercept_post_rename(_rename_ctx)

        return {}

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

        # Check permission: TRAVERSE for implicit directories, READ for files
        # This enables `stat /skills` to work for authenticated users (TRAVERSE is auto-allowed)
        ctx = context if context is not None else self._default_context
        if is_implicit_dir:
            # Only check permissions if enforcement is enabled
            if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                # Try TRAVERSE permission first (O(1))
                # Fall back to descendant access check if TRAVERSE denied (Unix-like behavior)
                has_permission = self._permission_enforcer.check(path, Permission.TRAVERSE, ctx)
                if not has_permission:
                    has_permission = self._descendant_checker.has_access(path, Permission.READ, ctx)  # type: ignore[attr-defined]  # allowed
                if not has_permission:
                    raise PermissionError(
                        f"Access denied: User '{ctx.user_id}' does not have TRAVERSE "
                        f"permission for '{path}'"
                    )
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

        # Batch permission check using filter_list
        perm_start = time.time()
        allowed_set: set[str]
        if not self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
            allowed_set = set(validated_paths)
        else:
            try:
                from nexus.contracts.types import OperationContext

                ctx = context if context is not None else self._default_context
                assert isinstance(ctx, OperationContext), "Context must be OperationContext"
                allowed_paths = self._permission_enforcer.filter_list(validated_paths, ctx)
                allowed_set = set(allowed_paths)
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
    async def sys_access(self, path: str, context: OperationContext | None = None) -> bool:
        """
        Check if a file or directory exists.

        Args:
            path: Virtual path to check
            context: Operation context for permission checks (uses default if None)

        Returns:
            True if file or implicit directory exists AND user has read permission on it
            OR any descendant (enables hierarchical navigation), False otherwise

        Note:
            With permissions enabled, directories are visible if user has access to ANY
            descendant, even if they don't have direct access to the directory itself.
            This enables hierarchical navigation (e.g., /workspace visible if user has
            access to /workspace/joe/file.txt).

        Performance:
            For implicit directories (directories without explicit files, like /zones),
            uses TRAVERSE permission check (O(1)) instead of descendant access check (O(n)).
            This is a major optimization for FUSE path resolution operations.
        """
        try:
            path = self._validate_path(path)

            # Check if it's an implicit directory first (before permission check for optimization)
            is_implicit_dir = self.metadata.is_implicit_directory(path)

            # Check permission if enforcement enabled
            if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                ctx = context if context is not None else self._default_context

                # OPTIMIZATION: For implicit directories, use TRAVERSE permission (O(1))
                # instead of expensive descendant access check (O(n))
                # TRAVERSE is granted on root-level implicit directories like /zones, /sessions, /skills
                if is_implicit_dir:
                    # Try TRAVERSE permission first (O(1) check)
                    if self._permission_enforcer.check(path, Permission.TRAVERSE, ctx):
                        return True
                    # Fall back to descendant access check for non-root implicit dirs
                    # (e.g., /zones/zone_1 where user may have access to children)
                    if not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        return False
                else:
                    # Issue #1147: OPTIMIZATION for real files - use direct permission check (O(1))
                    # instead of descendant access (O(n) fallback).
                    # Real files have no descendants, so descendant check is unnecessary.
                    # This reduces exists() latency from 300-500ms to 10-20ms.
                    if not self._permission_enforcer.check(path, Permission.READ, ctx):
                        # No direct READ permission = treat as non-existent for security
                        return False

            # Check if file exists explicitly
            if self.metadata.exists(path):
                return True
            # Return implicit directory status (already computed above)
            return is_implicit_dir
        except Exception:  # InvalidPathError
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
                results[path] = await self.sys_access(path, context=context)
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

                # Check permission if enforcement enabled
                if self._enforce_permissions:  # type: ignore[attr-defined]  # allowed
                    ctx = context if context is not None else self._default_context
                    if not self._descendant_checker.has_access(path, Permission.READ, ctx):
                        results[path] = None
                        continue

                # Check if it's a directory
                is_dir = await self.sys_is_directory(path, context=context)  # type: ignore[attr-defined]  # allowed

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
    async def delete_bulk(
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
            >>> results = nx.delete_bulk(['/a.txt', '/b.txt', '/folder'])
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
            # Delete content from backend for each file
            _errors: list[str] = []
            for file_meta in files_in_dir:
                if file_meta.etag and file_meta.mime_type != "inode/directory":
                    try:
                        _file_route = self.router.route(file_meta.path)
                        if context:
                            _del_ctx = _dc_replace(context, backend_path=_file_route.backend_path)
                        else:
                            _del_ctx = OperationContext(
                                user_id="anonymous",
                                groups=[],
                                backend_path=_file_route.backend_path,
                            )
                        _file_route.backend.delete_content(file_meta.etag, context=_del_ctx)
                    except Exception as e:
                        if len(_errors) < 100:
                            _errors.append(f"{file_meta.path}: {e}")
            if _errors:
                logger.debug(
                    "Bulk content delete: %d error(s) (showing up to 100): %s",
                    len(_errors),
                    "; ".join(_errors),
                )

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
    async def rename_bulk(
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
            >>> results = nx.rename_bulk([
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
    # ReBAC delegation stubs (Issue #2033)
    # Previously on NexusFSReBACMixin, now forwarded to rebac_service.
    # Generated dynamically below via _rebac_delegate().
    # ------------------------------------------------------------------

    # Service forwarding: __getattr__ routes method calls to services (Issue #2033)

    _SERVICE_METHODS: dict[str, str] = {
        # WorkspaceRPCService
        "workspace_snapshot": "_workspace_rpc_service",
        "workspace_restore": "_workspace_rpc_service",
        "workspace_log": "_workspace_rpc_service",
        "workspace_diff": "_workspace_rpc_service",
        "snapshot_begin": "_workspace_rpc_service",
        "snapshot_commit": "_workspace_rpc_service",
        "snapshot_rollback": "_workspace_rpc_service",
        "load_workspace_config": "_workspace_rpc_service",
        "register_workspace": "_workspace_rpc_service",
        "unregister_workspace": "_workspace_rpc_service",
        "update_workspace": "_workspace_rpc_service",
        "list_workspaces": "_workspace_rpc_service",
        "get_workspace_info": "_workspace_rpc_service",
        # AgentRPCService
        "register_agent": "_agent_rpc_service",
        "update_agent": "_agent_rpc_service",
        "list_agents": "_agent_rpc_service",
        "get_agent": "_agent_rpc_service",
        "delete_agent": "_agent_rpc_service",
        # UserProvisioningService
        "provision_user": "_user_provisioning_service",
        "deprovision_user": "_user_provisioning_service",
        # SandboxRPCService
        "sandbox_create": "_sandbox_rpc_service",
        "sandbox_run": "_sandbox_rpc_service",
        "sandbox_validate": "_sandbox_rpc_service",
        "sandbox_pause": "_sandbox_rpc_service",
        "sandbox_resume": "_sandbox_rpc_service",
        "sandbox_stop": "_sandbox_rpc_service",
        "sandbox_list": "_sandbox_rpc_service",
        "sandbox_status": "_sandbox_rpc_service",
        "sandbox_get_or_create": "_sandbox_rpc_service",
        "sandbox_connect": "_sandbox_rpc_service",
        "sandbox_disconnect": "_sandbox_rpc_service",
        # MetadataExportService
        "export_metadata": "_metadata_export_service",
        "import_metadata": "_metadata_export_service",
        # MountService — routed via _SERVICE_ALIASES (sync suffixed methods)
        # MountPersistService
        "save_mount": "_mount_persist_service",
        "list_saved_mounts": "_mount_persist_service",
        "load_mount": "_mount_persist_service",
        "delete_saved_mount": "_mount_persist_service",
        # SearchService (list/glob/grep are thin forwarders, not __getattr__)
        # asemantic_search* are in _SERVICE_ALIASES (name transformation: a-prefix removed)
        "glob_batch": "search_service",
        # TaskQueueService
        "get_task": "task_queue_service",
        "cancel_task": "task_queue_service",
        # MCPService
        "mcp_list_mounts": "mcp_service",
        # OAuthService
        "oauth_list_providers": "oauth_service",
        # LLMService
        "create_llm_reader": "llm_service",
        # ReBACService direct methods (no _sync suffix)
        "set_rebac_option": "rebac_service",
        "get_rebac_option": "rebac_service",
        "register_namespace": "rebac_service",
        # EventsService (Issue #1166)
        "wait_for_changes": "events_service",
        "lock": "events_service",
        "extend_lock": "events_service",
        "unlock": "events_service",
    }

    # Special aliases where service method name differs
    _SERVICE_ALIASES: dict[str, tuple[str, str]] = {
        "sandbox_available": ("_sandbox_rpc_service", "sandbox_available"),
        "get_sync_job": ("_sync_job_service", "get_job"),
        "list_sync_jobs": ("_sync_job_service", "list_jobs"),
        "load_all_saved_mounts": ("_mount_persist_service", "load_all_mounts"),
        # MountService sync accessors (facade → mount_service.*_sync)
        "add_mount": ("mount_service", "add_mount_sync"),
        "remove_mount": ("mount_service", "remove_mount_sync"),
        "list_connectors": ("mount_service", "list_connectors_sync"),
        "list_mounts": ("mount_service", "list_mounts_sync"),
        "get_mount": ("mount_service", "get_mount_sync"),
        "has_mount": ("mount_service", "has_mount_sync"),
        # SearchService async methods: a-prefix removed when calling service
        "asemantic_search": ("search_service", "semantic_search"),
        "asemantic_search_index": ("search_service", "semantic_search_index"),
        "asemantic_search_stats": ("search_service", "semantic_search_stats"),
        # SyncService / SyncJobService (Issue #2033)
        "sync_mount": ("_sync_service", "sync_mount_flat"),
        "sync_mount_async": ("_sync_job_service", "sync_mount_async"),
        "cancel_sync_job": ("_sync_job_service", "cancel_sync_job"),
        # VersionService async methods (Issue #2033)
        "aget_version": ("version_service", "get_version"),
        "alist_versions": ("version_service", "list_versions"),
        "arollback": ("version_service", "rollback"),
        "adiff_versions": ("version_service", "diff_versions"),
        # ReBACService async methods (Issue #2033)
        "arebac_create": ("rebac_service", "rebac_create"),
        "arebac_delete": ("rebac_service", "rebac_delete"),
        "arebac_check": ("rebac_service", "rebac_check"),
        "arebac_check_batch": ("rebac_service", "rebac_check_batch"),
        "arebac_expand": ("rebac_service", "rebac_expand"),
        "arebac_explain": ("rebac_service", "rebac_explain"),
        "arebac_list_tuples": ("rebac_service", "rebac_list_tuples"),
        "aget_namespace": ("rebac_service", "get_namespace"),
        # ReBACService sync methods with _sync suffix (Issue #2033)
        "rebac_expand": ("rebac_service", "rebac_expand_sync"),
        "rebac_explain": ("rebac_service", "rebac_explain_sync"),
        "share_with_user": ("rebac_service", "share_with_user_sync"),
        "share_with_group": ("rebac_service", "share_with_group_sync"),
        "grant_consent": ("rebac_service", "grant_consent_sync"),
        "revoke_consent": ("rebac_service", "revoke_consent_sync"),
        "make_public": ("rebac_service", "make_public_sync"),
        "make_private": ("rebac_service", "make_private_sync"),
        "apply_dynamic_viewer_filter": ("rebac_service", "apply_dynamic_viewer_filter_sync"),
        "list_outgoing_shares": ("rebac_service", "list_outgoing_shares_sync"),
        "list_incoming_shares": ("rebac_service", "list_incoming_shares_sync"),
        "get_dynamic_viewer_config": ("rebac_service", "get_dynamic_viewer_config_sync"),
        "namespace_create": ("rebac_service", "namespace_create_sync"),
        "namespace_delete": ("rebac_service", "namespace_delete_sync"),
        "namespace_list": ("rebac_service", "namespace_list_sync"),
        "get_namespace": ("rebac_service", "get_namespace_sync"),
        # ReBACService direct methods (no _sync suffix)
        "rebac_expand_with_privacy": ("rebac_service", "rebac_expand_with_privacy_sync"),
        # SkillService (Issue #2035): NexusFS facade → skill_service RPC methods
        "skills_share": ("skill_service", "rpc_share"),
        "skills_discover": ("skill_service", "rpc_discover"),
        "skills_get_prompt_context": ("skill_service", "rpc_get_prompt_context"),
        # SkillPackageService (Issue #2035): NexusFS facade → skill_package_service
        "skills_import": ("skill_package_service", "import_skill"),
        "skills_validate_zip": ("skill_package_service", "validate_zip"),
    }

    def __getattr__(self, name: str) -> Any:
        """Forward extracted facade methods to their service objects.

        This enables callers to continue using nx.method_name() after
        facade methods were removed from NexusFS (Issue #2033).
        """
        # Check aliases first (method name differs on service)
        alias = NexusFS._SERVICE_ALIASES.get(name)
        if alias is not None:
            svc_attr, svc_method = alias
            svc = self.__dict__.get(svc_attr)
            # Fallback: check containers for attrs removed from instance dict (Issue #1570)
            if svc is None:
                _sys = object.__getattribute__(self, "_system_services")
                _brk = object.__getattribute__(self, "_brick_services")
                _bare = svc_attr.lstrip("_")
                if _sys is not None:
                    svc = getattr(_sys, _bare, None)
                if svc is None and _brk is not None:
                    svc = getattr(_brk, _bare, None)
            if svc is not None:
                return getattr(svc, svc_method)

        # Standard forwarding (same method name on service)
        svc_attr_std = NexusFS._SERVICE_METHODS.get(name)
        if svc_attr_std is not None:
            svc = self.__dict__.get(svc_attr_std)
            # Fallback: check containers for attrs removed from instance dict (Issue #1570)
            if svc is None:
                _sys = object.__getattribute__(self, "_system_services")
                _brk = object.__getattribute__(self, "_brick_services")
                _bare = svc_attr_std.lstrip("_")
                if _sys is not None:
                    svc = getattr(_sys, _bare, None)
                if svc is None and _brk is not None:
                    svc = getattr(_brk, _bare, None)
            if svc is not None:
                return getattr(svc, name)

        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")

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

    @rpc_expose(description="List directory entries")
    async def sys_readdir(
        self,
        path: str = "/",
        recursive: bool = True,
        details: bool = False,
        show_parsed: bool = True,
        context: Any = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> builtins.list[str] | builtins.list[dict[str, Any]] | Any:
        prefix = path if path != "/" else ""
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"

        if limit is not None:
            from nexus.core.pagination import paginate_iter

            items_iter = self.metadata.list_iter(prefix=prefix, recursive=recursive)
            result = paginate_iter(items_iter, limit=limit, cursor_path=cursor)
            if details:
                result.items = [
                    {
                        "path": e.path,
                        "size": e.size,
                        "etag": e.etag,
                        "entry_type": 1
                        if (
                            not recursive
                            and e.entry_type == 0
                            and self.metadata.is_implicit_directory(e.path)
                        )
                        else e.entry_type,
                        "zone_id": e.zone_id,
                        "owner_id": e.owner_id,
                        "modified_at": e.modified_at.isoformat() if e.modified_at else None,
                        "version": e.version,
                    }
                    for e in result.items
                ]
            else:
                result.items = [e.path for e in result.items]
            return result

        entries = self.metadata.list(prefix=prefix, recursive=recursive)
        if details:
            return [
                {
                    "path": e.path,
                    "size": e.size,
                    "etag": e.etag,
                    "entry_type": 1
                    if (
                        not recursive
                        and e.entry_type == 0
                        and self.metadata.is_implicit_directory(e.path)
                    )
                    else e.entry_type,
                    "zone_id": e.zone_id,
                    "owner_id": e.owner_id,
                    "modified_at": e.modified_at.isoformat() if e.modified_at else None,
                    "version": e.version,
                }
                for e in entries
            ]
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
        # Issue #1789: delegate to write_observer via service registry or
        # _system_services as last resort.  _close_callbacks handles close().
        wo = None
        _sys = self._system_services
        if _sys is not None:
            wo = getattr(_sys, "write_observer", None)
        if wo is None or not hasattr(wo, "flush"):
            return {"flushed": 0}
        flushed: int = NexusFS._run_async(wo.flush())
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
        """Async shutdown: stop PersistentService + deactivate HotSwappable, then close.

        Preferred over close() when an event loop is available.
        Calls coordinator lifecycle methods first (async), then
        delegates to close() for sync resource cleanup.
        """
        coord = self.service_coordinator
        if coord is not None:
            await coord.stop_persistent_services()
            await coord.deactivate_hot_swappable_services()
        self.close()

    def close(self) -> None:
        """Close the filesystem and release resources."""
        # Close IPC primitives — kernel-internal (§4.2)
        if hasattr(self, "_pipe_manager"):
            self._pipe_manager.close_all()
        if hasattr(self, "_stream_manager"):
            self._stream_manager.close_all()

        # Issue #1793/#1789/#1792: Service close via factory-registered callbacks.
        # Replaces direct _system_services reads for write_observer, rebac_manager,
        # audit_store. Runs BEFORE pillar close so DB connections are still open.
        for _close_cb in self._close_callbacks:
            try:
                _close_cb()
            except Exception as exc:
                logger.debug("close: callback failed (best-effort): %s", exc)

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
