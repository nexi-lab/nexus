"""factory/orchestrator.py — the init/main.c of Nexus.

Complete boot sequence, readable top-to-bottom.
``create_nexus_fs()`` is the single entry point — read it to understand
the entire boot sequence.  ``create_nexus_services()`` is a callable
sub-sequence for callers who need the service containers separately.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.backends.base.backend import Backend
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


# =====================================================================
# Sub-sequence: create service containers (Tier 0/1/2)
# =====================================================================


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

    # --- Background threads deferred to NexusFS.initialize() ---

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
        # (PipeManager + StreamManager are kernel-internal primitives §4.2,
        # constructed in NexusFS.__init__ — not injected via SystemServices.)
        # Scheduler (Issue #2195)
        scheduler_service=system_dict.get("scheduler_service"),
        # Process table + ACP
        process_table=system_dict.get("process_table"),
        acp_service=system_dict.get("acp_service"),
        # Distributed event bus — Tier 1 infrastructure (Issue #1701)
        event_bus=brick_dict["event_bus"],
        # Distributed lock manager — Tier 1 infrastructure (Issue #1702)
        lock_manager=brick_dict["lock_manager"],
    )

    brick_services = _BrickServices(
        workflow_engine=brick_dict["workflow_engine"],
        rebac_circuit_breaker=brick_dict["rebac_circuit_breaker"],
        wallet_provisioner=brick_dict["wallet_provisioner"],
        chunked_upload_service=brick_dict["chunked_upload_service"],
        manifest_resolver=brick_dict["manifest_resolver"],
        tool_namespace_middleware=brick_dict["tool_namespace_middleware"],
        api_key_creator=brick_dict["api_key_creator"],
        snapshot_service=brick_dict["snapshot_service"],
        # IPC Brick (Issue #1727, LEGO §8)
        ipc_storage_driver=brick_dict["ipc_storage_driver"],
        ipc_provisioner=brick_dict["ipc_provisioner"],
        # Sandbox Brick (Issue #1307)
        agent_event_log=brick_dict["agent_event_log"],
        # Delegation Brick (Issue #2131)
        delegation_service=brick_dict["delegation_service"],
        # Version Brick (Issue #2034: moved from kernel)
        version_service=brick_dict["version_service"],
        # Governance Brick (Issue #2129)
        governance_anomaly_service=brick_dict["governance_anomaly_service"],
        governance_collusion_service=brick_dict["governance_collusion_service"],
        governance_graph_service=brick_dict["governance_graph_service"],
        governance_response_service=brick_dict["governance_response_service"],
        # Search Brick (Issue #810)
        zoekt_pipe_consumer=brick_dict.get("zoekt_pipe_consumer"),
        # Task Manager Brick
        task_dispatch_consumer=brick_dict.get("task_dispatch_consumer"),
    )

    return kernel_services, system_services, brick_services


# =====================================================================
# Main boot sequence — the init/main.c of Nexus
# =====================================================================


async def create_nexus_fs(
    backend: "Backend",
    metadata_store: "MetastoreABC",
    record_store: "RecordStoreABC | None" = None,
    *,
    cache_store: Any = None,
    is_admin: bool = False,
    cache: "CacheConfig | None" = None,
    permissions: "PermissionConfig | None" = None,
    audit: "AuditConfig | None" = None,
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
    """Create NexusFS with full service wiring — the recommended entry point.

    Read this function top-to-bottom to understand the entire boot sequence.
    Each call is a black box — drill into the implementation only if needed.

    Phases follow the OS kernel boot model:

      Config  — resolve router, EventBus gating, service containers
      Tier 0  — validate Storage Pillars (via create_nexus_services)
      Tier 1  — system services (ReBAC, permissions, audit)
      Tier 2  — brick services (search, workflows, IPC, governance)
      Link    — wire topology in memory (no I/O)
      Init    — one-time side effects (hook registration, IPC mount)

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
    """
    from dataclasses import replace as _dc_replace

    from nexus.contracts.deployment_profile import DeploymentProfile as _DP
    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import SystemServices as _SystemServices
    from nexus.core.nexus_fs import NexusFS
    from nexus.core.router import PathRouter

    # ── Config resolution ─────────────────────────────────────────────
    router = PathRouter(metadata_store)
    router.add_mount("/", backend)

    # KERNEL-ARCHITECTURE §2: No CacheStore AND no Redis/Dragonfly → EventBus disabled.
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
                distributed = _dc_replace(_base_dist, enable_events=False)
                logger.debug("EventBus disabled: no CacheStore or Redis/Dragonfly URL")

    # ── Tier 0/1/2: Create service containers ─────────────────────────
    # KERNEL mode (Issue #2194): When record_store is None (e.g. profile=kernel),
    # this branch is skipped — bare kernel with empty SystemServices/BrickServices.
    if kernel_services is None and record_store is not None:
        if system_services is not None or brick_services is not None:
            logger.warning(
                "[FACTORY] system_services/brick_services provided without kernel_services — "
                "they will be overwritten by create_nexus_services(). Pass kernel_services "
                "to use pre-built service containers."
            )
        kernel_services, system_services, brick_services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            router=router,
            permissions=permissions,
            audit=audit,
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
            kernel_services = _dc_replace(kernel_services, router=router)

    if system_services is None:
        system_services = _SystemServices()
    if brick_services is None:
        brick_services = _BrickServices()

    # ── Construct kernel ──────────────────────────────────────────────
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

    # ── LINK — wire topology (pure memory, no I/O) ────────────────────
    _sys = nx._system_services
    nx._permission_enforcer = _sys.permission_enforcer  # Issue #1706: override sentinel

    _parsing = parsing if parsing is not None else nx._parse_config

    # ParsersBrick (owns both registries — Issue #1523)
    from nexus.bricks.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=_parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # CacheBrick (owns all cache domain services — Issue #1524)
    from nexus.cache.brick import CacheBrick

    _cache_brick = CacheBrick(
        cache_store=nx.cache_store,
        record_store=nx._record_store,
    )

    # ContentCache (Issue #657)
    _content_cache = None
    _cache_cfg = nx._cache_config
    if _cache_cfg.enable_content_cache:
        _root_backend: Any = None
        try:
            _root_backend = nx.router.route("/").backend
        except Exception:
            logger.debug("No root backend mounted — ContentCache disabled")
        if _root_backend is not None and getattr(_root_backend, "has_root_path", False):
            from nexus.storage.content_cache import ContentCache

            _content_cache = ContentCache(max_size_mb=_cache_cfg.content_cache_size_mb)

    # Pack factory-created bricks into BrickServices (Issue #2134)
    _brick_updates: dict[str, Any] = {
        "cache_brick": _cache_brick,
        "parse_fn": _parse_fn,
        "content_cache": _content_cache,
        "parser_registry": parsers_brick.parser_registry,
        "provider_registry": parsers_brick.provider_registry,
    }
    if workflow_engine is not None:
        _brick_updates["workflow_engine"] = workflow_engine
    nx._brick_services = _dc_replace(nx._brick_services, **_brick_updates)

    # Resolve enabled_bricks for profile gating
    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in _resolved_bricks

    # PermissionChecker (services layer — Issue #899, #1766)
    from nexus.bricks.rebac.checker import PermissionChecker as _PC

    _permission_checker = _PC(
        permission_enforcer=_sys.permission_enforcer,
        metadata_store=nx.metadata,
        default_context=nx._default_context,
        enforce_permissions=nx._enforce_permissions,
    )

    # Boot wired services (Tier 2b — services needing NexusFS reference)
    from nexus.factory._wired import _boot_wired_services

    _wired = await _boot_wired_services(
        nx,
        nx.router,
        nx._system_services,
        nx._brick_services,
        _brick_on,
    )

    # Create ServiceLifecycleCoordinator (Issue #1708)
    from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
        ServiceLifecycleCoordinator,
    )

    _blm = getattr(nx._system_services, "brick_lifecycle_manager", None)
    coordinator = ServiceLifecycleCoordinator(nx._service_registry, _blm, nx._dispatch)
    nx._service_coordinator = coordinator

    # Enlist wired services into coordinator
    from nexus.factory.service_routing import enlist_wired_services

    await enlist_wired_services(coordinator, _wired)

    # Enlist system-tier PersistentService instances (Issue #1666)
    _dpb = getattr(nx._system_services, "deferred_permission_buffer", None)
    if _dpb is not None:
        await coordinator.enlist("deferred_permission_buffer", _dpb)
    _dw = getattr(nx._system_services, "delivery_worker", None)
    if _dw is not None:
        await coordinator.enlist("delivery_worker", _dw)

    # Inject kernel components (descendant_checker — like Linux LSM hook)
    _dc = getattr(_wired, "descendant_checker", None)
    if _dc is not None:
        nx._descendant_checker = _dc

    nx._linked = True

    # ── INITIALIZE — one-time side effects (no background threads) ────

    # IPC adapter bind + mount
    from nexus.factory._wired import _initialize_wired_ipc

    _initialize_wired_ipc(nx, nx._brick_services)

    # Register VFS hooks (INTERCEPT + OBSERVE — Issue #900)
    from nexus.factory._hooks import register_vfs_hooks

    await register_vfs_hooks(
        nx,
        permission_checker=_permission_checker,
        auto_parse=nx._parse_config.auto_parse if nx._parse_config else True,
        brick_on=_brick_on,
        parse_fn=_parse_fn,
    )

    # Register late bricks with lifecycle manager (Issue #1704, #2991)
    if _blm is not None:
        from nexus.factory._helpers import _register_late_bricks

        _register_late_bricks(_blm, {"cache": _cache_brick})

    # Register bootstrap callbacks for deferred background work
    _zl = getattr(nx._system_services, "zone_lifecycle", None) if nx._system_services else None
    if _zl is not None and hasattr(_zl, "load_terminating_zones"):

        async def _load_zones() -> None:
            try:
                _sf = getattr(_zl, "_session_factory", None)
                if _sf is not None:
                    with _sf() as session:
                        _zl.load_terminating_zones(session)
                    logger.debug(
                        "[LIFECYCLE] ZoneLifecycleService loaded terminating zones (bootstrap)"
                    )
            except Exception as exc:
                logger.warning("[LIFECYCLE] Failed to load terminating zones: %s", exc)

        nx._bootstrap_callbacks.append(_load_zones)

    nx._initialized = True

    return nx
