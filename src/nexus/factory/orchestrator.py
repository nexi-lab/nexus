"""Factory orchestrator — create_nexus_services, create_nexus_fs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

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
        zone_lifecycle=system_dict.get("zone_lifecycle"),
        # (PipeManager + StreamManager + AgentRegistry are kernel-internal primitives §4.2,
        # constructed in NexusFS.__init__ — not injected via SystemServices.
        # EvictionManager + AcpService deferred to _do_link() — Issue #1792.)
        # Scheduler (Issue #2195)
        scheduler_service=system_dict.get("scheduler_service"),
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
            from dataclasses import replace as _dc_replace

            kernel_services = _dc_replace(kernel_services, router=router)

    # Default system/brick to empty containers when not provided
    if system_services is None:
        system_services = _SystemServices()
    if brick_services is None:
        brick_services = _BrickServices()

    import functools

    from nexus.factory._lifecycle import _do_initialize, _do_link

    nx = NexusFS(
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        cache=cache,
        permissions=permissions,
        distributed=distributed,
        memory=memory,
        parsing=parsing,
        kernel_services=kernel_services,
        brick_services=brick_services,
    )
    # Issue #1801: factory owns identity — kernel never fabricates it.
    from nexus.contracts.types import OperationContext as _OC

    nx._default_context = _OC(user_id="system", groups=[], is_admin=is_admin)
    nx._link_fn = functools.partial(_do_link, system_services=system_services, zone_id=zone_id)
    nx._initialize_fn = _do_initialize
    # Backward compat: server/CLI/tests may read nx._system_services directly.
    nx._system_services = system_services
    await nx.link(
        enabled_bricks=enabled_bricks,
        parsing=parsing,
        workflow_engine=workflow_engine,
    )
    await nx.initialize()
    return nx


async def _register_vfs_hooks(
    nx: "NexusFS",
    *,
    system_services: Any = None,
    permission_checker: Any = None,
    auto_parse: bool = True,
    brick_on: "Callable[[str], bool] | None" = None,
    parse_fn: Any = None,
) -> None:
    """Register hooks + observers via coordinator.enlist() (Issue #900, #1709).

    Kernel creates KernelDispatch with empty callback lists at init.
    This function populates them at boot — modules register into
    kernel-owned infrastructure, kernel never auto-constructs hooks.

    Issue #1708/1709: All hooks enlisted via coordinator.enlist() —
    single entry point, no fallback.  Coordinator is always available
    (created in _do_link for local profiles, _boot_remote_services for REMOTE).
    """
    from nexus.factory._helpers import _make_gate

    _on = _make_gate(brick_on)

    _coordinator = nx._service_coordinator

    async def _enlist(name: str, hook: Any) -> None:
        """Enlist hook via coordinator — the single entry point."""
        await _coordinator.enlist(name, hook)

    # ── Zone write guard hook (Issue #1790) ────────────────────────
    # Rejects writes to zones being deprovisioned (Issue #2061).
    # Replaces _check_zone_writable() in nexus_fs — kernel no longer
    # reads zone_lifecycle from _system_services.
    _zl = getattr(system_services, "zone_lifecycle", None) if system_services else None
    if _zl is not None:
        from nexus.system_services.lifecycle.zone_write_guard_hook import ZoneWriteGuardHook

        await _enlist("zone_write_guard", ZoneWriteGuardHook(zone_lifecycle=_zl))

    # ── Permission pre-intercept hook (Issue #899) ────────────────
    if permission_checker is not None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        _perm_hook = PermissionCheckHook(
            checker=permission_checker,
            metadata_store=nx.metadata,
            default_context=nx._default_context,
            enforce_permissions=nx._enforce_permissions,
            permission_enforcer=system_services.permission_enforcer if system_services else None,
            descendant_checker=getattr(nx, "_descendant_checker", None),
        )
        await _enlist("permission", _perm_hook)

    # ── Audit write interceptor (Issue #900, #1772) ──
    # Piped observer → async AuditWriteInterceptor serializes mutations → DT_PIPE.
    # Sync observer  → sync SyncAuditWriteInterceptor calls on_write() directly.
    write_observer = getattr(system_services, "write_observer", None) if system_services else None
    if write_observer is not None:
        from nexus.storage.piped_record_store_write_observer import PipedRecordStoreWriteObserver

        strict = getattr(write_observer, "_strict_mode", True)
        if isinstance(write_observer, PipedRecordStoreWriteObserver):
            from nexus.storage.piped_record_store_write_observer import _AUDIT_PIPE_PATH
            from nexus.storage.write_observer_hooks import AuditWriteInterceptor

            audit: AuditWriteInterceptor | SyncAuditWriteInterceptor = AuditWriteInterceptor(
                nx, _AUDIT_PIPE_PATH, strict_mode=strict
            )
        else:
            from nexus.storage.write_observer_hooks import SyncAuditWriteInterceptor

            audit = SyncAuditWriteInterceptor(write_observer, strict_mode=strict)
        await _enlist("audit", audit)

    # DynamicViewerReadHook (post-read: column-level CSV filtering)
    has_viewer = (
        (getattr(system_services, "rebac_manager", None) if system_services else None) is not None
        and hasattr(nx, "get_dynamic_viewer_config")
        and hasattr(nx, "apply_dynamic_viewer_filter")
    )
    if has_viewer:
        from nexus.bricks.rebac.dynamic_viewer_hook import DynamicViewerReadHook
        from nexus.lib.context_utils import get_subject_from_context

        _viewer_hook = DynamicViewerReadHook(
            get_subject=get_subject_from_context,
            get_viewer_config=nx.get_dynamic_viewer_config,
            apply_filter=nx.apply_dynamic_viewer_filter,
        )
        await _enlist("viewer", _viewer_hook)

    # ContentParserEngine (on-demand parsed reads — Issue #1383)
    from nexus.bricks.parsers.engine import ContentParserEngine

    ContentParserEngine(
        metadata=nx.metadata,
        provider_registry=nx._brick_services.provider_registry,
    )

    # AutoParseWriteHook (post-write: background parsing + cache invalidation)
    parser_reg = nx._brick_services.parser_registry
    if auto_parse and parser_reg is not None and parse_fn is not None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        _auto_parse_hook = AutoParseWriteHook(
            get_parser=parser_reg.get_parser,
            parse_fn=parse_fn,
            metadata=nx.metadata,
        )
        await _enlist("auto_parse", _auto_parse_hook)

    # TigerCacheRenameHook (post-rename: bitmap updates)
    _rebac_mgr = getattr(system_services, "rebac_manager", None) if system_services else None
    tiger_cache = getattr(_rebac_mgr, "_tiger_cache", None) if _rebac_mgr else None
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        def _metadata_list_iter(
            prefix: str,
            recursive: bool = True,
            zone_id: str = "root",  # noqa: ARG001
        ) -> Any:
            return nx.metadata.list(prefix=prefix, recursive=recursive)

        _tiger_rename_hook = TigerCacheRenameHook(
            tiger_cache=tiger_cache,
            metadata_list_iter=_metadata_list_iter,
        )
        await _enlist("tiger_rename", _tiger_rename_hook)

    # TigerCacheWriteHook (post-write: add new files to ancestor directory grants)
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        _tiger_write_hook = TigerCacheWriteHook(tiger_cache=tiger_cache)
        await _enlist("tiger_write", _tiger_write_hook)

    # ── PRE-DISPATCH: Virtual view resolver (Issue #332, #889) ────────
    from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

    _vview_resolver = VirtualViewResolver(
        metadata=nx.metadata,
        path_router=nx.router,
        permission_checker=permission_checker,
        parse_fn=parse_fn,
        read_tracker_fn=None,
    )
    await _enlist("virtual_view", _vview_resolver)

    # ── ProcResolver (procfs virtual filesystem for AgentRegistry — Issue #1570) ──
    _proc_table = getattr(nx, "_agent_registry", None)
    if _proc_table is not None:
        try:
            from nexus.system_services.proc.proc_resolver import ProcResolver

            _proc_resolver = ProcResolver(_proc_table)
            await _enlist("proc", _proc_resolver)
        except Exception as exc:
            logger.debug("[BOOT:HOOKS] ProcResolver unavailable: %s", exc)

    # ── TaskWriteHook + TaskDispatchPipeConsumer + TaskAgentResolver ───────────
    if _on("task_manager"):
        try:
            from nexus.bricks.task_manager.service import TaskManagerService
            from nexus.bricks.task_manager.task_agent_resolver import TaskAgentResolver
            from nexus.bricks.task_manager.write_hook import TaskWriteHook

            _task_svc = TaskManagerService(nexus_fs=nx)
            _task_write_hook = TaskWriteHook()

            # Wire consumer from brick_services (created in _bricks.py)
            _task_consumer = getattr(nx._brick_services, "task_dispatch_consumer", None)
            if _task_consumer is not None:
                _task_write_hook.register_handler(_task_consumer)
                _task_consumer.set_task_service(_task_svc)

            await _enlist("task_write", _task_write_hook)
            await _enlist("task_agent_resolver", TaskAgentResolver(_proc_table))
            await _enlist("task_manager", _task_svc)  # Issue #1768: Q1 service via coordinator
        except Exception as exc:
            logger.warning("[BOOT:BRICK] task_manager wiring failed: %s", exc)
    else:
        logger.debug("[BOOT:BRICK] task_manager disabled by profile")

    # ── Snapshot write tracker (Issue #1770) ─────────────────────────
    _snapshot_svc = getattr(nx._brick_services, "snapshot_service", None)
    if _snapshot_svc is not None:
        from nexus.bricks.snapshot.snapshot_hook import SnapshotWriteHook

        await _enlist("snapshot_write", SnapshotWriteHook(_snapshot_svc))

    # ── Deferred permission buffer (Issue #1773, #1682) ────────────────
    _dpb = getattr(system_services, "deferred_permission_buffer", None) if system_services else None
    _rebac_for_perm = getattr(system_services, "rebac_manager", None) if system_services else None
    if _dpb is not None:
        from nexus.bricks.rebac.deferred_permission_hook import DeferredPermissionHook

        await _enlist(
            "deferred_permission",
            DeferredPermissionHook(_dpb, rebac_manager=_rebac_for_perm),
        )
    else:
        # Sync fallback — same logic, runs as post-write hook instead of inline kernel code
        _hier = getattr(system_services, "hierarchy_manager", None) if system_services else None
        if _hier is not None or _rebac_for_perm is not None:
            from nexus.bricks.rebac.sync_permission_hook import SyncPermissionWriteHook

            await _enlist(
                "sync_permission",
                SyncPermissionWriteHook(hierarchy_manager=_hier, rebac_manager=_rebac_for_perm),
            )

    # ── Zone writability gate (Issue #1371, #2061) ─────────────────────
    # Replaces NexusFS._check_zone_writable() — kernel should not know
    # about zone lifecycle.  PRE hooks on all mutating ops block writes
    # to zones being deprovisioned.
    _zl = getattr(system_services, "zone_lifecycle", None) if system_services else None
    if _zl is not None:
        from nexus.system_services.lifecycle.zone_writability_hook import ZoneWritabilityHook

        await _enlist("zone_writability", ZoneWritabilityHook(_zl))

    # ── OBSERVE observers (Issue #900, #922) ──────────────────────────
    # EventBusObserver: forwards FileEvents to distributed EventBus (Redis/NATS).
    # Replaces _publish_file_event() direct calls — single dispatch exit point.
    # Issue #1701: event_bus is now Tier 1 (SystemServices).  Direct injection —
    # no bus_provider late-binding needed.  Tests use swap_service() to replace.
    from nexus.system_services.event_bus.observer import EventBusObserver

    _bus_observer = EventBusObserver(
        event_bus=system_services.event_bus if system_services else None
    )
    await _enlist("event_bus_observer", _bus_observer)

    # EventsService observer: self-registered via HotSwappable.hook_spec()
    # at bootstrap() → activate_hot_swappable_services() (Issue #1611).

    # RevisionTrackingObserver: feeds RevisionNotifier on versioned mutations.
    # Replaces the old kernel-internal _increment_vfs_revision() (Issue #1382).
    from nexus.lib.revision_notifier import RevisionNotifier
    from nexus.system_services.lifecycle.revision_tracking_observer import RevisionTrackingObserver

    _rev_notifier = RevisionNotifier()
    _rev_observer = RevisionTrackingObserver(revision_notifier=_rev_notifier)
    await _enlist("revision_tracking", _rev_observer)

    # ── Test hooks (Issue #2) ────────────────────────────────────────
    # Only registered when NEXUS_TEST_HOOKS=true for E2E hook testing.
    import os

    if os.getenv("NEXUS_TEST_HOOKS") == "true":
        from nexus.core.test_hooks import register_test_hooks

        register_test_hooks(nx._dispatch)
