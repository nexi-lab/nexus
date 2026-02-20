"""Factory orchestrator — create_nexus_services, create_nexus_fs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from nexus.backends.backend import Backend
    from nexus.bricks.workflows.protocol import WorkflowProtocol
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
    record_store: RecordStoreABC,
    metadata_store: MetastoreABC,
    backend: Backend,
    router: PathRouter,
    *,
    permissions: PermissionConfig | None = None,
    cache: CacheConfig | None = None,
    distributed: DistributedConfig | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    enable_write_buffer: bool | None = None,
    resiliency_raw: dict[str, Any] | None = None,
    enabled_bricks: frozenset[str] | None = None,
) -> tuple[KernelServices, SystemServices, BrickServices]:
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
        enable_write_buffer: Use async WriteBuffer for PG sync (Issue #1246).
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
    from nexus.core.config import BrickServices as _BrickServices
    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import KernelServices as _KernelServices
    from nexus.core.config import PermissionConfig as _PermissionConfig
    from nexus.core.config import SystemServices as _SystemServices
    from nexus.factory._background import _start_background_services
    from nexus.factory._boot_context import _BootContext
    from nexus.factory._bricks import _boot_independent_bricks
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

    from nexus.core.performance_tuning import resolve_profile_tuning

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
        scoped_hook_engine=system_dict.get("scoped_hook_engine"),
        brick_lifecycle_manager=system_dict.get("brick_lifecycle_manager"),
        brick_reconciler=system_dict.get("brick_reconciler"),
        delivery_worker=system_dict["delivery_worker"],
        observability_subsystem=system_dict["observability_subsystem"],
        resiliency_manager=system_dict["resiliency_manager"],
        eviction_manager=system_dict.get("eviction_manager"),
        zone_lifecycle=system_dict.get("zone_lifecycle"),
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
        ipc_vfs_driver=brick_dict["ipc_vfs_driver"],
        ipc_provisioner=brick_dict["ipc_provisioner"],
        # Sandbox Brick (Issue #1307)
        agent_event_log=brick_dict["agent_event_log"],
        # Skills Brick (Issue #2035)
        skill_service=brick_dict["skill_service"],
        skill_package_service=brick_dict["skill_package_service"],
        # Delegation & Reputation Bricks (Issue #2131)
        delegation_service=brick_dict["delegation_service"],
        reputation_service=brick_dict["reputation_service"],
        # Version Brick (Issue #2034: moved from kernel)
        version_service=brick_dict["version_service"],
        # Memory Brick (Issue #2177)
        memory_router=brick_dict["memory_router"],
        memory_permission=brick_dict["memory_permission"],
        # Governance Brick (Issue #2129)
        governance_anomaly_service=brick_dict["governance_anomaly_service"],
        governance_collusion_service=brick_dict["governance_collusion_service"],
        governance_graph_service=brick_dict["governance_graph_service"],
        governance_response_service=brick_dict["governance_response_service"],
    )

    return kernel_services, system_services, brick_services


def create_nexus_fs(
    backend: Backend,
    metadata_store: MetastoreABC,
    record_store: RecordStoreABC | None = None,
    *,
    cache_store: Any = None,
    is_admin: bool = False,
    custom_namespaces: list[Any] | None = None,
    cache: CacheConfig | None = None,
    permissions: PermissionConfig | None = None,
    distributed: DistributedConfig | None = None,
    memory: Any = None,
    parsing: Any = None,
    kernel_services: KernelServices | None = None,
    system_services: SystemServices | None = None,
    brick_services: BrickServices | None = None,
    enable_write_buffer: bool | None = None,
    enabled_bricks: frozenset[str] | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    workflow_engine: WorkflowProtocol | None = None,
) -> NexusFS:
    """Create NexusFS with default services — the recommended entry point.

    Args:
        backend: Backend instance for file storage.
        metadata_store: MetastoreABC instance.
        record_store: Optional RecordStoreABC. When provided, all services
            (ReBAC, Audit, Permissions, etc.) are created and injected.
        cache_store: CacheStoreABC instance for ephemeral cache.
        is_admin: Whether the instance has admin privileges.
        custom_namespaces: Custom namespace configurations.
        cache: CacheConfig object.
        permissions: PermissionConfig object.
        distributed: DistributedConfig object.
        memory: MemoryConfig object.
        parsing: ParseConfig object.
        kernel_services: Pre-built KernelServices (skips create_nexus_services).
        system_services: Pre-built SystemServices.
        brick_services: Pre-built BrickServices.
        enable_write_buffer: Use async WriteBuffer for PG sync.
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
    from nexus.core.router import NamespaceConfig, PathRouter
    from nexus.factory._wired import _boot_wired_services

    # Create and configure router
    router = PathRouter()
    if custom_namespaces:
        for ns_config in custom_namespaces:
            if isinstance(ns_config, dict):
                ns_config = NamespaceConfig(**ns_config)
            router.register_namespace(ns_config)
    router.add_mount("/", backend, priority=0)

    # KERNEL-ARCHITECTURE §2: No CacheStore → EventBus disabled.
    _has_real_cache = cache_store is not None
    if _has_real_cache:
        from nexus.core.cache_store import NullCacheStore as _NullCacheStore

        if isinstance(cache_store, _NullCacheStore):
            _has_real_cache = False
    if not _has_real_cache:
        _base_dist = distributed or _DistributedConfig()
        if _base_dist.enable_events:
            from dataclasses import replace as _dc_replace

            distributed = _dc_replace(_base_dist, enable_events=False)
            logger.debug("EventBus disabled: no CacheStore provided (KERNEL-ARCHITECTURE §2)")

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
    from nexus.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # Create CacheBrick — owns all cache domain services (Issue #1524)
    from nexus.bricks.cache.brick import CacheBrick

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
    from nexus.core.lock_fast import create_vfs_lock_manager

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
        backend=backend,
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        is_admin=is_admin,
        custom_namespaces=custom_namespaces,
        cache=cache,
        permissions=permissions,
        distributed=distributed,
        memory=memory,
        parsing=parsing,
        kernel_services=kernel_services,
        system_services=system_services,
        brick_services=brick_services,
    )

    # --- Phase 1b: Wire service-layer components onto NexusFS ---
    from nexus.services.service_wiring import wire_services

    wire_services(nx)

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

    # Register bricks created in create_nexus_fs with lifecycle manager (Issue #1704)
    _blm = getattr(system_services, "brick_lifecycle_manager", None)
    if _blm is not None:
        _blm.register("parsers", parsers_brick, protocol_name="ParsersProtocol")
        _blm.register("cache", _cache_brick, protocol_name="CacheProtocol")

    return nx
