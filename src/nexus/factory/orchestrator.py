"""Factory orchestrator — create_nexus_services, create_nexus_fs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.contracts.types import AuditConfig
    from nexus.core.config import (
        BrickServices,
        CacheConfig,
        DistributedConfig,
        KernelServices,
        PermissionConfig,
        SystemServices,
    )
    from nexus.core.metastore import MetastoreABC
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def create_nexus_services(
    record_store: "RecordStoreABC",
    metadata_store: "MetastoreABC",
    backend: "Backend",
    router: "PathRouter",
    *,
    permissions: PermissionConfig | None = None,
    audit: AuditConfig | None = None,
    cache: CacheConfig | None = None,
    distributed: DistributedConfig | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    enable_write_buffer: bool | None = None,
    resiliency_raw: dict[str, Any] | None = None,
    enabled_bricks: frozenset[str] | None = None,
) -> "tuple[KernelServices, SystemServices, BrickServices]":
    """Create default services for NexusFS dependency injection.

    Orchestrates 3-tier boot sequence:

    1. **Kernel** — validates Storage Pillars (VFS router, Metastore).
       Failure raises ``BootError``.
    2. **System** — critical services (ReBAC, permissions, write-sync →
       ``BootError``) + degradable services (workspace, agent registry,
       namespace, observability → WARNING + ``None``).
    3. **Brick** — optional (search, wallet, manifest, upload, distributed).
       Failure is silent (DEBUG) + ``None``.

    Background threads (``.start()``) are deferred until all three tiers
    are constructed.

    Args:
        record_store: RecordStoreABC instance (provides engine + session_factory).
        metadata_store: MetastoreABC instance (for PermissionEnforcer).
        backend: Backend instance (for WorkspaceManager).
        router: PathRouter instance (for PermissionEnforcer object type resolution).
        permissions: Permission config (defaults from PermissionConfig()).
        cache: Cache config (for TTL values, defaults from CacheConfig()).
        distributed: Distributed config (for event bus/locks).
        zone_id: Default zone ID (for WorkspaceManager, embedded mode only).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode only).
        enable_write_buffer: Use async DT_PIPE observer for PG sync (Issue #809).
        resiliency_raw: Raw resiliency policy dict from YAML config.
        enabled_bricks: Set of brick names to enable. When None, all bricks
            are enabled (backward-compatible default = FULL profile).

    Returns:
        Tuple of (KernelServices, SystemServices, BrickServices).

    .. versionchanged:: Issue #2034
        Returns a 3-tuple instead of a single KernelServices.
    """
    # --- Profile-based brick gating (Issue #1389) ---
    from nexus.contracts.deployment_profile import DeploymentProfile
    from nexus.contracts.types import AuditConfig as _AuditConfig
    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import PermissionConfig as _PermissionConfig
    from nexus.core.config import SystemServices as _SystemServices
    from nexus.factory._background import _start_background_services
    from nexus.factory._boot_context import _BootContext
    from nexus.factory._bricks import _boot_dependent_bricks, _boot_independent_bricks
    from nexus.factory._helpers import _register_factory_bricks
    from nexus.factory._kernel import _boot_kernel_services
    from nexus.factory._system import _boot_system_services

    if enabled_bricks is None:
        enabled_bricks = DeploymentProfile.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in enabled_bricks

    from nexus.contracts.deployment_profile import ALL_BRICK_NAMES as _ALL_BRICKS

    logger.info(
        "Factory: enabled_bricks=%d/%d %s",
        len(enabled_bricks),
        len(_ALL_BRICKS),
        sorted(enabled_bricks),
    )

    # --- Performance tuning (Issue #2071) ---
    import os

    from nexus.lib.performance_tuning import resolve_profile_tuning

    _profile_str = os.environ.get("NEXUS_PROFILE", "full")
    if _profile_str == "auto":
        # "auto" is config-only, not in enum — use FULL for tuning defaults
        _factory_profile = DeploymentProfile.FULL
    else:
        try:
            _factory_profile = DeploymentProfile(_profile_str)
        except ValueError:
            _factory_profile = DeploymentProfile.FULL
    _profile_tuning = resolve_profile_tuning(_factory_profile)

    perm = permissions or _PermissionConfig()
    audit_cfg = audit or _AuditConfig()
    cache_cfg = cache or _CacheConfig()
    dist = distributed or _DistributedConfig()

    ctx = _BootContext(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        router=router,
        engine=record_store.engine,
        read_engine=record_store.read_engine,
        perm=perm,
        audit=audit_cfg,
        cache_ttl_seconds=cache_cfg.ttl_seconds,
        dist=dist,
        zone_id=zone_id,
        agent_id=agent_id,
        enable_write_buffer=enable_write_buffer,
        resiliency_raw=resiliency_raw,
        db_url=getattr(record_store, "database_url", ""),
        profile_tuning=_profile_tuning,
    )

    # --- Tier 0: KERNEL (validate Storage Pillars) ---
    _boot_kernel_services(ctx)

    # --- Tier 1: SYSTEM (critical + degradable, gated by profile) ---
    system_dict = _boot_system_services(ctx, _brick_on)

    # --- Tier 2: BRICK (optional, gated by profile) ---
    brick_dict = _boot_independent_bricks(ctx, system_dict, _brick_on)

    # --- Tier 2b: DEPENDENT BRICK (Issue #1861: artifact auto-indexing) ---
    _boot_dependent_bricks(ctx, system_dict, brick_dict)

    # --- Start background threads post-construction ---
    _start_background_services(system_dict)

    # --- Register factory-created bricks with lifecycle manager (Issue #1704) ---
    _blm = system_dict.get("brick_lifecycle_manager")
    if _blm is not None:
        _register_factory_bricks(_blm, brick_dict)

    # --- Assemble 3-tier containers (Issue #2034, #2193) ---
    kernel_services = _KernelServices(router=router)

    system_services = _SystemServices(
        # Former-kernel critical (Issue #2193)
        rebac_manager=system_dict["rebac_manager"],
        audit_store=system_dict["audit_store"],
        entity_registry=system_dict["entity_registry"],
        permission_enforcer=system_dict["permission_enforcer"],
        write_observer=system_dict["write_observer"],
        # Former-kernel degradable (Issue #2193)
        dir_visibility_cache=system_dict["dir_visibility_cache"],
        hierarchy_manager=system_dict["hierarchy_manager"],
        deferred_permission_buffer=system_dict["deferred_permission_buffer"],
        workspace_registry=system_dict["workspace_registry"],
        mount_manager=system_dict["mount_manager"],
        workspace_manager=system_dict["workspace_manager"],
        # Original system services
        agent_registry=system_dict["agent_registry"],
        async_agent_registry=system_dict["async_agent_registry"],
        namespace_manager=system_dict["namespace_manager"],
        async_namespace_manager=system_dict["async_namespace_manager"],
        context_branch_service=system_dict.get("context_branch_service"),
        brick_lifecycle_manager=system_dict.get("brick_lifecycle_manager"),
        brick_reconciler=system_dict.get("brick_reconciler"),
        delivery_worker=system_dict["delivery_worker"],
        observability_subsystem=system_dict["observability_subsystem"],
        resiliency_manager=system_dict["resiliency_manager"],
        eviction_manager=system_dict.get("eviction_manager"),
        zone_lifecycle=system_dict.get("zone_lifecycle"),
        # DT_PIPE manager (Issue #809)
        pipe_manager=system_dict.get("pipe_manager"),
        # EventLog + Scheduler (Issue #2195)
        event_log=system_dict.get("event_log"),
        scheduler_service=system_dict.get("scheduler_service"),
    )

    brick_services = _BrickServices(
        event_bus=brick_dict["event_bus"],
        lock_manager=brick_dict["lock_manager"],
        workflow_engine=brick_dict["workflow_engine"],
        rebac_circuit_breaker=brick_dict["rebac_circuit_breaker"],
        wallet_provisioner=brick_dict["wallet_provisioner"],
        chunked_upload_service=brick_dict["chunked_upload_service"],
        manifest_resolver=brick_dict["manifest_resolver"],
        tool_namespace_middleware=brick_dict["tool_namespace_middleware"],
        api_key_creator=brick_dict["api_key_creator"],
        snapshot_service=brick_dict["snapshot_service"],
        task_queue_service=brick_dict["task_queue_service"],
        # IPC Brick (Issue #1727, LEGO §8)
        ipc_storage_driver=brick_dict["ipc_storage_driver"],
        ipc_provisioner=brick_dict["ipc_provisioner"],
        # Sandbox Brick (Issue #1307)
        agent_event_log=brick_dict["agent_event_log"],
        # Delegation & Reputation Bricks (Issue #2131)
        delegation_service=brick_dict["delegation_service"],
        reputation_service=brick_dict["reputation_service"],
        # Version Brick (Issue #2034: moved from kernel)
        version_service=brick_dict["version_service"],
        # Memory Brick (Issue #2177)
        memory_permission=brick_dict["memory_permission"],
        # Governance Brick (Issue #2129)
        governance_anomaly_service=brick_dict["governance_anomaly_service"],
        governance_collusion_service=brick_dict["governance_collusion_service"],
        governance_graph_service=brick_dict["governance_graph_service"],
        governance_response_service=brick_dict["governance_response_service"],
        # Search Brick (Issue #810)
        zoekt_pipe_consumer=brick_dict.get("zoekt_pipe_consumer"),
    )

    return kernel_services, system_services, brick_services


def create_nexus_fs(
    backend: "Backend",
    metadata_store: "MetastoreABC",
    record_store: "RecordStoreABC | None" = None,
    *,
    cache_store: Any = None,
    is_admin: bool = False,
    cache: "CacheConfig | None" = None,
    permissions: "PermissionConfig | None" = None,
    distributed: "DistributedConfig | None" = None,
    memory: Any = None,
    parsing: Any = None,
    kernel_services: "KernelServices | None" = None,
    system_services: "SystemServices | None" = None,
    brick_services: "BrickServices | None" = None,
    enable_write_buffer: bool | None = None,
    enabled_bricks: frozenset[str] | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    workflow_engine: "WorkflowProtocol | None" = None,
) -> "NexusFS":
    """Create NexusFS with default services — the recommended entry point.

    Args:
        backend: Backend instance for file storage.
        metadata_store: MetastoreABC instance.
        record_store: Optional RecordStoreABC. When provided, all services
            (ReBAC, Audit, Permissions, etc.) are created and injected.
        cache_store: CacheStoreABC instance for ephemeral cache.
        is_admin: Whether the instance has admin privileges.
        cache: CacheConfig object.
        permissions: PermissionConfig object.
        distributed: DistributedConfig object.
        memory: MemoryConfig object.
        parsing: ParseConfig object.
        kernel_services: Pre-built KernelServices (skips create_nexus_services).
        system_services: Pre-built SystemServices.
        brick_services: Pre-built BrickServices.
        enable_write_buffer: Use async DT_PIPE observer for PG sync.
        enabled_bricks: Set of brick names to enable.
        zone_id: Default zone ID (for WorkspaceManager, embedded mode).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode).
        workflow_engine: Pre-built workflow engine override.

    Returns:
        Fully configured NexusFS instance with services injected.

    .. versionchanged:: Issue #2034
        ``services`` param replaced by ``kernel_services``, ``system_services``,
        ``brick_services`` (3-tier split).
    """
    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import (
        DistributedConfig as _DistributedConfig,
    )
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import SystemServices as _SystemServices
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter
    from nexus.factory._wired import _boot_wired_services

    # Create and configure router
    router = PathRouter(metadata_store)
    router.add_mount("/", backend)

    # KERNEL-ARCHITECTURE §2: No CacheStore AND no Redis/Dragonfly → EventBus disabled.
    # EventBus uses Redis/Dragonfly pub/sub independently of CacheStore, so only
    # disable when neither a real CacheStore nor a Redis URL is available.
    _has_real_cache = cache_store is not None
    if _has_real_cache:
        from nexus.contracts.cache_store import NullCacheStore as _NullCacheStore

        if isinstance(cache_store, _NullCacheStore):
            _has_real_cache = False
    if not _has_real_cache:
        from nexus.lib.env import get_dragonfly_url, get_redis_url

        _has_event_url = bool(get_redis_url() or get_dragonfly_url())
        if not _has_event_url:
            _base_dist = distributed or _DistributedConfig()
            if _base_dist.enable_events:
                from dataclasses import replace as _dc_replace

                distributed = _dc_replace(_base_dist, enable_events=False)
                logger.debug("EventBus disabled: no CacheStore or Redis/Dragonfly URL")

    # Create services if record_store is provided and no pre-built services.
    # KERNEL mode (Issue #2194): When record_store is None (e.g. profile=kernel),
    # this branch is skipped — bare kernel with empty SystemServices/BrickServices.
    if kernel_services is None and record_store is not None:
        kernel_services, system_services, brick_services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            permissions=permissions,
            cache=cache,
            distributed=distributed,
            zone_id=zone_id,
            agent_id=agent_id,
            enable_write_buffer=enable_write_buffer,
            enabled_bricks=enabled_bricks,
        )
    elif kernel_services is None:
        kernel_services = _KernelServices(router=router)
    else:
        # Use provided services but ensure router is set (frozen — use replace)
        if kernel_services.router is None:
            from dataclasses import replace as _dc_replace

            kernel_services = _dc_replace(kernel_services, router=router)

    # Default system/brick to empty containers when not provided
    if system_services is None:
        system_services = _SystemServices()
    if brick_services is None:
        brick_services = _BrickServices()

    from dataclasses import replace as _dc_replace

    # Create ParsersBrick — owns both registries (Issue #1523)
    from nexus.bricks.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # Create CacheBrick — owns all cache domain services (Issue #1524)
    from nexus.cache.brick import CacheBrick

    _cache_brick = CacheBrick(
        cache_store=cache_store,
        record_store=record_store,
    )

    # Create content cache (Issue #657)
    _content_cache = None
    if cache is None:
        from nexus.core.config import CacheConfig as _CC

        _cache_for_cc = _CC()
    else:
        _cache_for_cc = cache
    if _cache_for_cc.enable_content_cache and backend.has_root_path is True:
        from nexus.storage.content_cache import ContentCache

        _content_cache = ContentCache(max_size_mb=_cache_for_cc.content_cache_size_mb)

    # Create VFS lock manager (Issue #657)
    from nexus.lib.lock_fast import create_vfs_lock_manager

    _vfs_lock_manager = create_vfs_lock_manager()

    # Pack factory-created bricks into BrickServices container (Issue #2134)
    _brick_updates: dict[str, Any] = {
        "cache_brick": _cache_brick,
        "parse_fn": _parse_fn,
        "content_cache": _content_cache,
        "parser_registry": parsers_brick.parser_registry,
        "provider_registry": parsers_brick.provider_registry,
        "vfs_lock_manager": _vfs_lock_manager,
    }
    if workflow_engine is not None:
        _brick_updates["workflow_engine"] = workflow_engine
    brick_services = _dc_replace(brick_services, **_brick_updates)

    nx = NexusFS(
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        is_admin=is_admin,
        cache=cache,
        permissions=permissions,
        distributed=distributed,
        memory=memory,
        parsing=parsing,
        kernel_services=kernel_services,
        system_services=system_services,
        brick_services=brick_services,
    )

    # --- Phase 2: Wire services needing NexusFS reference (Issue #643) ---
    # Resolve enabled_bricks for brick gating (same pattern as create_nexus_services)
    from nexus.contracts.deployment_profile import DeploymentProfile as _DP

    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in _resolved_bricks

    _wired = _boot_wired_services(nx, kernel_services, system_services, brick_services, _brick_on)
    nx._bind_wired_services(_wired)
    _mds = getattr(_wired, "metadata_export_service", None)
    if _mds is not None:
        cast(Any, nx)._metadata_export_service = _mds

    # Create PermissionChecker (services layer) — Issue #899
    # Not stored on NexusFS; passed to hook registration below.
    from nexus.bricks.rebac.checker import PermissionChecker as _PC

    _permission_checker = _PC(
        permission_enforcer=nx._permission_enforcer,
        metadata_store=nx.metadata,
        default_context=nx._default_context,
        enforce_permissions=nx._enforce_permissions,
    )

    # Register bricks created in create_nexus_fs with lifecycle manager (Issue #1704)
    _blm = getattr(system_services, "brick_lifecycle_manager", None)
    if _blm is not None:
        _blm.register("parsers", parsers_brick, protocol_name="ParsersProtocol")
        _blm.register("cache", _cache_brick, protocol_name="CacheProtocol")

    # --- Register INTERCEPT hooks on KernelDispatch (Issue #900) ---
    _register_vfs_hooks(nx, permission_checker=_permission_checker)

    return nx


def _register_vfs_hooks(nx: "NexusFS", *, permission_checker: Any = None) -> None:
    """Register hooks + observers into kernel-owned dispatch (Issue #900).

    Kernel creates KernelDispatch with empty callback lists at init.
    This function populates them at boot — modules register into
    kernel-owned infrastructure, kernel never auto-constructs hooks.

    Called by ``create_nexus_fs()`` after NexusFS construction + wired
    services binding, keeping the kernel free of service-layer imports.
    """
    dispatch = nx._dispatch

    # ── Permission pre-intercept hook (Issue #899) ────────────────
    if permission_checker is not None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        _perm_hook = PermissionCheckHook(
            checker=permission_checker,
            metadata_store=nx.metadata,
            default_context=nx._default_context,
            enforce_permissions=nx._enforce_permissions,
            permission_enforcer=nx._permission_enforcer,
            descendant_checker=getattr(nx, "_descendant_checker", None),
        )
        dispatch.register_intercept_read(_perm_hook)
        dispatch.register_intercept_write(_perm_hook)
        dispatch.register_intercept_delete(_perm_hook)
        dispatch.register_intercept_rename(_perm_hook)
        dispatch.register_intercept_mkdir(_perm_hook)
        dispatch.register_intercept_rmdir(_perm_hook)

    # ── Audit write observer as interceptor (Issue #900) ──────────
    # Registered FIRST so it runs before other hooks (audit before side effects).
    write_observer = (
        getattr(nx._system_services, "write_observer", None) if nx._system_services else None
    )
    if write_observer is not None:
        from nexus.storage.write_observer_hooks import AuditWriteInterceptor

        strict = getattr(write_observer, "_strict_mode", True)
        audit = AuditWriteInterceptor(write_observer, strict_mode=strict)
        dispatch.register_intercept_write(audit)
        dispatch.register_intercept_write_batch(audit)
        dispatch.register_intercept_delete(audit)
        dispatch.register_intercept_rename(audit)
        dispatch.register_intercept_mkdir(audit)
        dispatch.register_intercept_rmdir(audit)

    # DynamicViewerReadHook (post-read: column-level CSV filtering)
    rebac_mgr = getattr(nx, "_rebac_manager", None)
    has_viewer = (
        rebac_mgr is not None
        and hasattr(nx, "_get_subject_from_context")
        and hasattr(nx, "get_dynamic_viewer_config")
        and hasattr(nx, "apply_dynamic_viewer_filter")
    )
    if has_viewer:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook

        dispatch.register_intercept_read(
            DynamicViewerReadHook(
                get_subject=nx._get_subject_from_context,
                get_viewer_config=nx.get_dynamic_viewer_config,
                apply_filter=nx.apply_dynamic_viewer_filter,
            )
        )

    # AutoParseWriteHook (post-write: background parsing)
    parser_reg = getattr(nx, "parser_registry", None)
    parse_fn = getattr(nx, "_virtual_view_parse_fn", None)
    if parser_reg is not None and parse_fn is not None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        dispatch.register_intercept_write(
            AutoParseWriteHook(
                get_parser=parser_reg.get_parser,
                parse_fn=parse_fn,
            )
        )

    # TigerCacheRenameHook (post-rename: bitmap updates)
    tiger_cache = getattr(rebac_mgr, "_tiger_cache", None) if rebac_mgr else None
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        def _metadata_list_iter(
            prefix: str,
            recursive: bool = True,
            zone_id: str = "root",  # noqa: ARG001
        ) -> Any:
            return nx.metadata.list(prefix=prefix, recursive=recursive)

        dispatch.register_intercept_rename(
            TigerCacheRenameHook(
                tiger_cache=tiger_cache,
                metadata_list_iter=_metadata_list_iter,
            )
        )

    # TigerCacheWriteHook (post-write: add new files to ancestor directory grants)
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        dispatch.register_intercept_write(TigerCacheWriteHook(tiger_cache=tiger_cache))

    # ── PRE-DISPATCH: Pipe resolver for DT_PIPE paths (Issue #1201) ────
    _pipe_mgr = getattr(nx._system_services, "pipe_manager", None) if nx._system_services else None
    if _pipe_mgr is not None:
        from nexus.system_services.pipe_resolver import PipeResolver

        dispatch.register_resolver(PipeResolver(pipe_manager=_pipe_mgr, metastore=nx.metadata))

    # ── PRE-DISPATCH: Memory virtual path resolver (Issue #889) ────────
    # memory_router removed from BrickServices — get it from MemoryPermissionEnforcer
    _mem_perm = getattr(nx._brick_services, "memory_permission", None)
    _mem_router = getattr(_mem_perm, "memory_router", None) if _mem_perm else None
    _mem_provider = getattr(nx, "_memory_provider", None)
    if _mem_router is not None and _mem_provider is not None:
        from nexus.bricks.memory.io_handler import MemoryIOHandler

        dispatch.register_resolver(
            MemoryIOHandler(
                memory_router=_mem_router,
                memory_provider=_mem_provider,
                path_router=nx.router,
            )
        )

    # ── PRE-DISPATCH: Virtual view resolver (Issue #332, #889) ────────
    from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

    dispatch.register_resolver(
        VirtualViewResolver(
            metadata=nx.metadata,
            path_router=nx.router,
            permission_checker=permission_checker,
            parse_fn=getattr(nx, "_virtual_view_parse_fn", None),
            viewer_filter_fn=getattr(nx, "_apply_dynamic_viewer_filter_if_needed", None),
            read_tracker_fn=getattr(nx, "_record_read_if_tracking", None),
        )
    )

    # ── OBSERVE observers (Issue #900, #922) ──────────────────────────
    # EventBusObserver: forwards FileEvents to distributed EventBus (Redis/NATS).
    # Replaces _publish_file_event() direct calls — single dispatch exit point.
    # Late-binding (Issue #969): always register with bus_provider=nx so that
    # post-construction overrides of nx._event_bus (e.g. E2E test fixtures
    # injecting a shared Redis bus) are picked up automatically.
    from nexus.system_services.event_subsystem.bus.observer import EventBusObserver

    dispatch.register_observe(EventBusObserver(bus_provider=nx))

    # ── Test hooks (Issue #2) ────────────────────────────────────────
    # Only registered when NEXUS_TEST_HOOKS=true for E2E hook testing.
    import os

    if os.getenv("NEXUS_TEST_HOOKS") == "true":
        from nexus.core.test_hooks import register_test_hooks

        register_test_hooks(dispatch)
