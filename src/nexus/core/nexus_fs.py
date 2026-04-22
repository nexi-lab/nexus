"""Unified filesystem implementation for Nexus."""
# Kernel interface unification — see KERNEL-ARCHITECTURE.md §4.5

import builtins
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from nexus.contracts.cache_store import CacheStoreABC, NullCacheStore
from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.types import OperationContext
from nexus.core.config import (
    CacheConfig,
    DistributedConfig,
    MemoryConfig,
    ParseConfig,
    PermissionConfig,
)
from nexus.core.metastore import MetastoreABC
from nexus.core.nexus_fs_content import ContentMixin
from nexus.core.nexus_fs_dispatch import DispatchMixin
from nexus.core.nexus_fs_internal import InternalMixin
from nexus.core.nexus_fs_metadata import MetadataMixin
from nexus.core.nexus_fs_watch import WatchMixin
from nexus.core.router import PathRouter
from nexus.lib.rpc_decorator import rpc_expose
from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


class NexusFS(  # type: ignore[misc]
    ContentMixin,
    MetadataMixin,
    InternalMixin,
    DispatchMixin,
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
        init_cred: OperationContext | None = None,
    ):
        """Initialize NexusFS kernel.

        Kernel boots with MetastoreABC (inode layer). Backends are mounted
        via ``DriverLifecycleCoordinator.mount()`` (which writes to the
        Rust kernel's MountTable) — like Linux VFS, no global backend.

        Args:
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

        # Issue #1801: kernel process credential — like Linux init_task.cred.
        # Immutable after construction. Used as fallback identity for internal
        # operations. External callers should pass explicit context= to syscalls.
        self._init_cred: OperationContext | None = init_cred

        # ── Kernel-owned primitives (always present, created here) ──────
        # See KERNEL-ARCHITECTURE.md §1 DI patterns table.

        # Advisory locks handled by Rust kernel LockManager (sys_lock / sys_unlock).

        self._init_dispatch()

        import os as _os_ipc

        _ipc_self_addr = _os_ipc.environ.get("NEXUS_ADVERTISE_ADDR")

        self._transport_pool = None
        if _ipc_self_addr:
            from nexus.remote.rpc_transport import RPCTransportPool as _RPCTransportPool

            self._transport_pool = _RPCTransportPool()

        # ── Kernel (Issue #1817 — single-FFI sys_read/sys_write) ──
        # Constructed BEFORE DriverLifecycleCoordinator and PathRouter so
        # that both see the kernel from birth (F2 MountTable migration:
        # kernel is the single source of truth for routing).
        from nexus._rust_compat import RUST_AVAILABLE

        self._kernel = None
        if RUST_AVAILABLE:
            try:
                from nexus.core.metastore import RustMetastoreProxy

                if isinstance(metadata_store, RustMetastoreProxy):
                    self._kernel = metadata_store._rust_kernel
                    metadata_store._kernel = self._kernel
                else:
                    from nexus_kernel import Kernel as _Kernel

                    self._kernel = _Kernel()
                    metadata_store._kernel = self._kernel
                    _redb_path = getattr(metadata_store, "_redb_path", None)
                    if _redb_path is not None:
                        self._kernel.set_metastore_path(str(_redb_path))
            except Exception as exc:
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "Kernel init failed — falling back to Python path: %s", exc
                )
                self._kernel = None

        from nexus.core.driver_lifecycle_coordinator import DriverLifecycleCoordinator

        self._driver_coordinator: DriverLifecycleCoordinator = DriverLifecycleCoordinator(
            self,
            kernel=self._kernel,
            self_address=_ipc_self_addr,
            transport_pool=self._transport_pool,
        )

        # PathRouter reads from DLC + delegates LPM to the kernel.
        self.router = PathRouter(
            self._driver_coordinator,
            metadata_store,
            self._kernel,
        )

        logger.info(
            "IPC primitives initialized: DriverCoordinator (self_address=%s)",
            _ipc_self_addr or "none/single-node",
        )

        from nexus.core.service_registry import ServiceRegistry

        self._service_registry: ServiceRegistry = ServiceRegistry(dispatch=self)

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
        """Phase 3: Start background services.  Server/Worker only.

        Auto-starts all BackgroundService instances (ZoneLifecycleService,
        EventDeliveryWorker, DeferredPermissionBuffer, etc.) via
        ServiceRegistry.start_background_services().

        Idempotent — guarded by ``_bootstrapped`` flag.
        """
        if self._bootstrapped:
            return
        if not self._initialized:
            self.initialize()
        # Auto-lifecycle: start BackgroundService instances (Issue #1580)
        coord = self.service_coordinator
        if coord is not None:
            coord.start_background_services()
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

    # _resolve_cred, _build_rust_ctx, _get_context_identity, _validate_path,
    # _parse_context, _ensure_context_ttl, _reject_if_virtual_readme,
    # _try_virtual_readme_stat, _try_virtual_readme_bytes,
    # _dispatch_write_events — all moved to InternalMixin (nexus_fs_internal.py)

    @rpc_expose(description="Acquire or extend advisory lock on a path")
    def sys_lock(
        self,
        path: str,
        mode: str = "exclusive",
        ttl: float = 30.0,
        max_holders: int = 1,
        lock_id: str | None = None,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> str | None:
        """Acquire or extend advisory lock (POSIX fcntl(F_SETLK)).

        When lock_id is None: try-acquire a new lock.
        When lock_id is provided: extend TTL of an existing lock (heartbeat).

        Returns lock_id on success, None on failure.
        """
        path = self._validate_path(path)
        return self._kernel.sys_lock(
            path,
            lock_id=lock_id or "",
            mode=mode,
            max_holders=max_holders,
            ttl_secs=int(ttl),
        )

    @rpc_expose(description="Release advisory lock (normal or force)")
    def sys_unlock(
        self,
        path: str,
        lock_id: str | None = None,
        force: bool = False,
        *,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> bool:
        """Release advisory lock.

        When force=False (default): release lock by lock_id (requires lock_id).
        When force=True: force-release ALL holders (admin operation, ignores lock_id).

        Returns True if released.
        """
        path = self._validate_path(path)
        if not force and not lock_id:
            raise ValueError("lock_id is required for non-force release")
        return self._kernel.sys_unlock(path, lock_id=lock_id or "", force=force)

    def _acquire_lock_sync(
        self,
        path: str,
        timeout: float,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> str | None:
        """Acquire advisory lock synchronously via kernel sys_lock."""
        from nexus.contracts.exceptions import LockTimeout

        lock_id = self.sys_lock(path, ttl=timeout)
        if lock_id is None:
            raise LockTimeout(path=path, timeout=timeout)
        return lock_id

    def _release_lock_sync(
        self,
        lock_id: str,
        path: str,
        context: OperationContext | None = None,  # noqa: ARG002
    ) -> None:
        """Release advisory lock synchronously via kernel sys_unlock."""
        if not lock_id:
            return
        try:
            self.sys_unlock(path, lock_id=lock_id)
        except Exception as e:
            logger.error(f"Failed to release lock {lock_id} for {path}: {e}")

    # sys_watch is in nexus_fs_watch.py (WatchMixin)

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
        context: Any = None,
    ) -> str:
        return self.mount_service.add_mount_sync(
            mount_point=mount_point,
            backend_type=backend_type,
            backend_config=backend_config,
            context=context,
        )

    def remove_mount(self, mount_point: str, context: Any = None) -> dict[str, Any]:
        return self.mount_service.remove_mount_sync(mount_point=mount_point, context=context)

    def list_mounts(self, context: Any = None) -> builtins.list[dict[str, Any]]:
        return self.mount_service.list_mounts_sync(context=context)

    def get_mount(self, mount_point: str, context: Any = None) -> dict[str, Any] | None:
        return self.mount_service.get_mount_sync(mount_point=mount_point, context=context)

    def aclose(self) -> None:
        """Shutdown: stop BackgroundService + unregister hooks, then close.

        Calls coordinator lifecycle methods first, then
        delegates to close() for sync resource cleanup.
        """
        # Issue #3391: drain deferred OBSERVE background tasks before tearing down.
        self.shutdown()

        coord = self.service_coordinator
        if coord is not None:
            coord.stop_background_services()
            coord._unregister_all_hooks()
        self.close()

    # ── IPC primitives (inlined from IPCMixin) ─────────────────────────

    def _pipe_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_PIPE — close Rust buffer."""

        with contextlib.suppress(Exception):
            self._kernel.destroy_pipe(path)
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

    # ------------------------------------------------------------------
    # Tier 2 public sync stream methods (kernel passthroughs)
    # ------------------------------------------------------------------
    # Stream counterparts to the pipe convenience methods above. Used by
    # LLM streaming backends (Rust OpenAIBackend / AnthropicBackend via
    # nx.llm_start_streaming) where a tight token-pump loop calls
    # ``stream_write_nowait`` per token and ``stream_read_at`` for
    # offset-based replay — async wrapping would just add ping-pong.

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

        # Slow path: block in Rust, release GIL
        _data, _next = self._kernel.stream_read_at_blocking(path, offset, 30000)
        return bytes(_data)

    def _stream_write(self, path: str, data: bytes) -> int:
        """Write to DT_STREAM — non-blocking via Rust kernel (condvar wakes readers), returns byte offset."""
        return self._kernel.stream_write_nowait(path, data)

    def _stream_destroy(self, path: str) -> dict[str, Any]:
        """Destroy DT_STREAM — close Rust buffer."""

        with contextlib.suppress(Exception):
            self._kernel.destroy_stream(path)
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
        # Close transport pool (persistent gRPC connections)
        if hasattr(self, "_transport_pool") and self._transport_pool is not None:
            self._transport_pool.close_all()

        # Auto-close all enlisted services that have a close() method
        # (rebac_manager, audit_store, etc.). Reverse registration order.
        self._service_registry.close_all_services()

        # Close metadata store
        self.metadata.close()

        # Release Rust-owned redb/SQLite file handles. Without this call the
        # Rust kernel keeps the metastore Box alive until Python GC runs —
        # process-lifetime tests that open the same redb path in a second
        # NexusFS hit ``Database already open. Cannot acquire lock.`` (Issue
        # #3765 Cat-5/6). ``release_metastores`` is idempotent.
        if self._kernel is not None:
            try:
                _release = getattr(self._kernel, "release_metastores", None)
                if _release is not None:
                    _release()
            except Exception as exc:  # pragma: no cover - best-effort teardown
                logger.debug("kernel.release_metastores failed: %s", exc)
            # Drop this kernel from the shared SQLiteMetastore cache so the
            # next ``SQLiteMetastore(path)`` in this process gets a fresh
            # kernel with its own metastore wired up (Issue #3765 Cat-5/6).
            try:
                from nexus.fs._sqlite_meta import _evict_kernel_cache

                _evict_kernel_cache(self._kernel)
            except Exception as exc:  # pragma: no cover - best-effort
                logger.debug("_evict_kernel_cache failed: %s", exc)

        # Close record store (Services layer SQL connections)
        if self._record_store is not None:
            self._record_store.close()

        # Close mounted backends that hold resources (e.g., OAuth connectors with SQLite)
        if hasattr(self, "router"):
            from nexus.core.protocols.connector import OAuthCapableProtocol

            for mp in self.router.get_mount_points():
                try:
                    route = self.router.route(mp, zone_id=self._zone_id)
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
