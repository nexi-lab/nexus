"""Factory orchestrator — create_nexus_services, create_nexus_fs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.contracts.constants import ROOT_ZONE_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from nexus.backends.base.backend import Backend
    from nexus.bricks.workflows.protocol import WorkflowProtocol
    from nexus.contracts.types import AuditConfig
    from nexus.core.config import (
        CacheConfig,
        DistributedConfig,
        PermissionConfig,
    )
    from nexus.core.metastore import MetastoreABC
    from nexus.core.nexus_fs import NexusFS
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)


def create_nexus_services(
    record_store: "RecordStoreABC",
    metadata_store: "MetastoreABC",
    backend: "Backend",
    dlc: Any = None,
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
) -> "dict[str, Any]":
    """Create default services for NexusFS dependency injection.

    Orchestrates 3-tier boot sequence:

    1. **Kernel** — validates Storage Pillars (VFS router, Metastore).
       Failure raises ``BootError``.  Inlined (no separate function).
    2. **Services** — critical services (ReBAC, permissions, write-sync →
       ``BootError``) + degradable services (workspace, namespace,
       observability → WARNING + ``None``).
    3. **Brick** — optional (search, wallet, manifest, upload, distributed).
       Failure is silent (DEBUG) + ``None``.

    Background threads (``.start()``) are deferred until all three tiers
    are constructed.

    Args:
        record_store: RecordStoreABC instance (provides engine + session_factory).
        metadata_store: MetastoreABC instance (for PermissionEnforcer).
        backend: Backend instance (for WorkspaceManager).
        dlc: DriverLifecycleCoordinator for routing + backend refs.
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
        dict[str, Any] — all services keyed by canonical name.
    """
    # --- Profile-based brick gating (Issue #1389) ---
    from nexus.contracts.deployment_profile import DeploymentProfile
    from nexus.contracts.types import AuditConfig as _AuditConfig
    from nexus.core.config import CacheConfig as _CacheConfig
    from nexus.core.config import DistributedConfig as _DistributedConfig
    from nexus.core.config import PermissionConfig as _PermissionConfig
    from nexus.factory._boot_context import _BootContext
    from nexus.factory._bricks import _boot_dependent_bricks, _boot_independent_bricks
    from nexus.factory._system import _boot_pre_kernel_services

    if enabled_bricks is None:
        enabled_bricks = DeploymentProfile.FULL.default_bricks()

    def svc_on(name: str) -> bool:
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

    # --- Driver gate (DeploymentProfile-driven) ----------------------------
    # Install the profile's enabled-driver set into the kernel's BackendFactory
    # gate before any sys_setattr(DT_MOUNT) fires.  Disabled drivers fail at
    # mount time with a clear error instead of silently falling through to the
    # local-default branch.  Local CAS / path / connector backends are kernel
    # defaults and skip the gate (see
    # `rust/kernel/src/hal/backend_factory.rs::is_driver_enabled`).
    try:
        import nexus_runtime as _nx_runtime

        _enabled_drivers = sorted(_factory_profile.default_drivers())
        _nx_runtime.nx_set_enabled_drivers(_enabled_drivers)
        logger.info(
            "Factory: enabled_drivers=%d %s (profile=%s)",
            len(_enabled_drivers),
            _enabled_drivers,
            _factory_profile.value,
        )
    except Exception as _exc:  # pragma: no cover — startup-only path
        logger.warning(
            "Factory: driver gate install skipped (%s): %s",
            type(_exc).__name__,
            _exc,
        )

    perm = permissions or _PermissionConfig()
    audit_cfg = audit or _AuditConfig()
    cache_cfg = cache or _CacheConfig()
    dist = distributed or _DistributedConfig()

    ctx = _BootContext(
        record_store=record_store,
        metadata_store=metadata_store,
        backend=backend,
        dlc=dlc,
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

    # --- Tier 0: KERNEL (validate Storage Pillars — inlined from _kernel.py) ---
    from nexus.contracts.exceptions import BootError

    if ctx.dlc is None:
        raise BootError("DLC is None", tier="kernel")
    if ctx.metadata_store is None:
        raise BootError("Metadata store is None", tier="kernel")
    if ctx.record_store is None:
        logger.warning("[BOOT:KERNEL] RecordStore is None — services layer disabled")
    logger.info("[BOOT:KERNEL] Storage pillars validated")

    # --- Tier 1: Services (critical + degradable, gated by profile) ---
    system_dict = _boot_pre_kernel_services(ctx, svc_on)

    # --- Tier 2: BRICK (optional, gated by profile) ---
    brick_dict = _boot_independent_bricks(ctx, system_dict, svc_on)

    # --- Tier 2b: DEPENDENT BRICK (Issue #1861: artifact auto-indexing) ---
    _boot_dependent_bricks(ctx, system_dict, brick_dict)

    # --- Background threads deferred to NexusFS.initialize() ---

    # --- Assemble unified services dict (Issue #2034, #2193) ---

    # Merge brick services into the unified dict (event_bus/lock_manager
    # already in system_dict after boot phase unification).
    system_dict.update(brick_dict)

    return system_dict


def create_nexus_fs(
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
    services: "dict[str, Any] | None" = None,
    enable_write_buffer: bool | None = None,
    enabled_bricks: frozenset[str] | None = None,
    zone_id: str | None = None,
    agent_id: str | None = None,
    workflow_engine: "WorkflowProtocol | None" = None,
    init_cred: Any = None,
    federation: Any = None,
    security: Any = None,
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
        services: Pre-built services dict. When None and record_store is
            provided, create_nexus_services() is called automatically.
        enable_write_buffer: Use async DT_PIPE observer for PG sync.
        enabled_bricks: Set of brick names to enable.
        zone_id: Default zone ID (for WorkspaceManager, embedded mode).
        agent_id: Default agent ID (for WorkspaceManager, embedded mode).
        workflow_engine: Pre-built workflow engine override.
        init_cred: Override kernel process identity (default: system user with is_admin flag).

    Returns:
        Fully configured NexusFS instance with services injected.
    """
    from nexus.core.config import (
        DistributedConfig as _DistributedConfig,
    )
    from nexus.core.nexus_fs import NexusFS

    # Mount table is owned by the Rust kernel (F2). Root mount is deferred
    # to sys_setattr(DT_MOUNT) after NexusFS construction.

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

    from nexus.contracts.types import OperationContext as _OC
    from nexus.factory._lifecycle import _initialize_services, _wire_services

    _init_cred = (
        init_cred if init_cred is not None else _OC(user_id="system", groups=[], is_admin=is_admin)
    )

    # F2: construct NexusFS first — services are built next using kernel + DLC.
    nx = NexusFS(
        metadata_store=metadata_store,
        record_store=record_store,
        cache_store=cache_store,
        cache=cache,
        permissions=permissions,
        distributed=distributed,
        memory=memory,
        parsing=parsing,
        init_cred=_init_cred,
    )

    # Root mount — Rust kernel DLC handles routing + metastore + dcache.
    from nexus.contracts.metadata import DT_MOUNT

    nx.sys_setattr("/", entry_type=DT_MOUNT, backend=backend)

    # Service-tier routing: kernel.route() + DLC for backend refs.
    # PathRouter eliminated — callers use kernel + DLC directly.

    # Create services if record_store is provided and no pre-built services.
    # KERNEL mode (Issue #2194): When record_store is None (e.g. profile=kernel),
    # this branch is skipped — bare kernel with no services.
    if services is None and record_store is not None:
        services = create_nexus_services(
            record_store=record_store,
            metadata_store=metadata_store,
            backend=backend,
            dlc=nx._driver_coordinator,
            permissions=permissions,
            audit=audit,
            cache=cache,
            distributed=distributed,
            zone_id=zone_id,
            agent_id=agent_id,
            enable_write_buffer=enable_write_buffer,
            enabled_bricks=enabled_bricks,
        )

    # Default to empty dict when not provided
    if services is None:
        services = {}

    # Linearized lifecycle — no partial injection (PR #3371 Phase 2)
    init_ctx = _wire_services(
        nx,
        services=services,
        zone_id=zone_id,
        enabled_bricks=enabled_bricks,
        parsing=parsing,
        workflow_engine=workflow_engine,
        federation=federation,
        security=security,
    )
    nx._linked = True

    _initialize_services(nx, init_ctx)
    nx._initialized = True

    return nx


def _register_vfs_hooks(
    nx: "NexusFS",
    *,
    services: Any = None,
    permission_checker: Any = None,
    auto_parse: bool = True,
    svc_on: "Callable[[str], bool] | None" = None,
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

    _on = _make_gate(svc_on)

    def _enlist(name: str, hook: Any) -> None:
        """Enlist hook via sys_setattr — factory is the first user."""
        nx.sys_setattr(f"/__sys__/services/{name}", service=hook)

    # ── Zone write guard hook (Issue #1790) ────────────────────────
    # Rejects writes to zones being deprovisioned (Issue #2061).
    # Replaces _check_zone_writable() in nexus_fs.
    _ss = services or {}
    _zl = _ss.get("zone_lifecycle")
    if _zl is not None:
        from nexus.services.lifecycle.zone_write_guard_hook import ZoneWriteGuardHook

        _enlist("zone_write_guard", ZoneWriteGuardHook(zone_lifecycle=_zl))

    # ── Permission pre-intercept hook (Issue #899) ────────────────
    if permission_checker is not None:
        from nexus.bricks.rebac.permission_hook import PermissionCheckHook

        # Permission write leases — check once, write many (Issue #3394)
        _lease_table = None
        if nx._perm_config.enforce:
            from nexus.bricks.rebac.cache.permission_lease import PermissionLeaseTable

            _lease_table = PermissionLeaseTable()

            # Wire invalidation: CacheCoordinator → path-targeted or zone-wide
            # (Issue #3398 decisions 3A/7A).
            _lt_ref = _lease_table  # capture for closures below

            def _lease_invalidation_callback(
                _zone_id: str,
                _subject: tuple[str, str],
                _relation: str,
                object: tuple[str, str],  # noqa: A002
            ) -> None:
                obj_type, obj_id = object
                # Direct grant on a file → invalidate only that path's leases
                if obj_type == "file":
                    _lt_ref.invalidate_path(obj_id)
                else:
                    # Group/directory/inherited/wildcard → zone-wide clear
                    _lt_ref.invalidate_all()

            _rebac_mgr = _ss.get("rebac_manager")
            if _rebac_mgr is not None and hasattr(_rebac_mgr, "_cache_coordinator"):
                _rebac_mgr._cache_coordinator.register_lease_invalidator(
                    "perm-write-lease", _lease_invalidation_callback
                )

                # Cross-zone lease invalidation via Pub/Sub (Issue #3398 decision 4A).
                # Subscribe to "lease" layer hints published by remote zones.
                _coord = _rebac_mgr._cache_coordinator
                if getattr(_coord, "_pubsub", None) is not None:
                    _zone_id = getattr(nx, "_zone_id", "root")

                    def _on_cross_zone_lease_hint(payload: dict) -> None:
                        obj_type = payload.get("object_type", "")
                        obj_id = payload.get("object_id", "")
                        if obj_type == "file" and obj_id:
                            _lt_ref.invalidate_path(obj_id)
                        else:
                            _lt_ref.invalidate_all()

                    _coord._pubsub.subscribe(_zone_id, "lease", _on_cross_zone_lease_hint)

        _perm_hook = PermissionCheckHook(
            checker=permission_checker,
            metadata_store=nx.metadata,
            default_context=nx._init_cred,
            enforce_permissions=nx._perm_config.enforce,
            permission_enforcer=_ss.get("permission_enforcer"),
            descendant_checker=nx.service("descendant_checker"),
            lease_table=_lease_table,
        )
        _enlist("permission", _perm_hook)

        # Expose lease table for late-binding consumers
        # (e.g., AcpService agent termination → lease revocation, Issue #3398).
        if _lease_table is not None:
            nx._permission_lease_table = _lease_table

    # ── Audit write interceptor (Issue #900, #1772) ──
    # Both sync and debounced observers now implement on_write/on_delete/etc.
    # SyncAuditWriteInterceptor bridges kernel dispatch_post_hooks → observer.
    write_observer = _ss.get("write_observer")
    if write_observer is not None:
        from nexus.storage.write_observer_hooks import SyncAuditWriteInterceptor

        strict = getattr(write_observer, "_strict_mode", True)
        audit: SyncAuditWriteInterceptor = SyncAuditWriteInterceptor(
            write_observer, strict_mode=strict
        )
        _enlist("audit", audit)

    # DynamicViewerReadHook (post-read: column-level CSV filtering)
    has_viewer = (
        _ss.get("rebac_manager") is not None
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
        _enlist("viewer", _viewer_hook)

    # ContentParserEngine (on-demand parsed reads — Issue #1383)
    from nexus.bricks.parsers.engine import ContentParserEngine

    _provider_reg = nx.service("provider_registry") if hasattr(nx, "service") else None
    ContentParserEngine(
        metadata=nx.metadata,
        provider_registry=_provider_reg,
    )

    # AutoParseWriteHook (post-write: background parsing + cache invalidation)
    parser_reg = nx.service("parser_registry") if hasattr(nx, "service") else None
    if auto_parse and parser_reg is not None and parse_fn is not None:
        from nexus.bricks.parsers.auto_parse_hook import AutoParseWriteHook

        _auto_parse_hook = AutoParseWriteHook(
            get_parser=parser_reg.get_parser,
            parse_fn=parse_fn,
            metadata=nx.metadata,
        )
        _enlist("auto_parse", _auto_parse_hook)

    # MarkdownStructureWriteHook (post-write: sync structural index — Issue #3718)
    from nexus.bricks.parsers.md_structure_hook import MarkdownStructureWriteHook

    _md_struct_hook = MarkdownStructureWriteHook(metadata=nx.metadata)
    _enlist("md_structure", _md_struct_hook)

    # TigerCacheRenameHook (post-rename: bitmap updates)
    _rebac_mgr = _ss.get("rebac_manager")
    tiger_cache = getattr(_rebac_mgr, "_tiger_cache", None) if _rebac_mgr else None
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.rename_hook import TigerCacheRenameHook

        def _metadata_list_iter(
            prefix: str,
            recursive: bool = True,
            zone_id: str = ROOT_ZONE_ID,  # noqa: ARG001
        ) -> Any:
            return nx.metadata.list(prefix=prefix, recursive=recursive)

        _tiger_rename_hook = TigerCacheRenameHook(
            tiger_cache=tiger_cache,
            metadata_list_iter=_metadata_list_iter,
        )
        _enlist("tiger_rename", _tiger_rename_hook)

    # TigerCacheWriteHook (post-write: add new files to ancestor directory grants)
    if tiger_cache is not None:
        from nexus.bricks.rebac.cache.tiger.write_hook import TigerCacheWriteHook

        _tiger_write_hook = TigerCacheWriteHook(tiger_cache=tiger_cache)
        _enlist("tiger_write", _tiger_write_hook)

    # ── PRE-DISPATCH: Virtual view resolver (Issue #332, #889) ────────
    from nexus.bricks.parsers.virtual_view_resolver import VirtualViewResolver

    _vview_resolver = VirtualViewResolver(
        metadata=nx.metadata,
        dlc=nx._driver_coordinator,
        permission_checker=permission_checker,
        parse_fn=parse_fn,
        read_tracker_fn=None,
    )
    _enlist("virtual_view", _vview_resolver)

    # ── PRE-DISPATCH: ReadmePathResolver (Issue #3827) ───────────────────
    from nexus.bricks.parsers.readme_resolver import ReadmePathResolver

    _readme_resolver = ReadmePathResolver(nexus_fs=nx)
    _enlist("readme_resolver", _readme_resolver)

    # ── AgentStatusResolver (procfs virtual filesystem for AgentRegistry — Issue #1570, #1810) ──
    _proc_ref = nx.service("agent_registry") if hasattr(nx, "service") else None
    _proc_table = _proc_ref if _proc_ref is not None else None
    if _proc_table is not None:
        try:
            from nexus.services.agents.agent_status_resolver import AgentStatusResolver

            _agent_status_resolver = AgentStatusResolver(_proc_table)
            _enlist("agent_status", _agent_status_resolver)
        except Exception as exc:
            logger.debug("[BOOT:HOOKS] AgentStatusResolver unavailable: %s", exc)

    # ── TaskWriteHook + TaskDispatchPipeConsumer + TaskAgentResolver ───────────
    if _on("task_manager"):
        try:
            from nexus.bricks.task_manager.service import TaskManagerService
            from nexus.bricks.task_manager.task_agent_resolver import TaskAgentResolver
            from nexus.bricks.task_manager.write_hook import TaskWriteHook

            _task_svc = TaskManagerService(nexus_fs=nx)
            _task_write_hook = TaskWriteHook()

            # Wire consumer from ServiceRegistry (created in _bricks.py, enlisted in _do_link)
            _task_consumer = (
                nx.service("task_dispatch_consumer") if hasattr(nx, "service") else None
            )
            if _task_consumer is not None:
                _task_write_hook.register_handler(_task_consumer)
                _task_consumer.set_task_service(_task_svc)

            _enlist("task_write", _task_write_hook)
            _enlist("task_agent_resolver", TaskAgentResolver(_proc_table))
            _enlist("task_manager", _task_svc)  # Issue #1768: Q1 service via coordinator
        except Exception as exc:
            logger.warning("[BOOT:BRICK] task_manager wiring failed: %s", exc)
    else:
        logger.debug("[BOOT:BRICK] task_manager disabled by profile")

    # ── Snapshot write tracker (Issue #1770) ─────────────────────────
    _snapshot_svc = nx.service("snapshot_service") if hasattr(nx, "service") else None
    if _snapshot_svc is not None:
        from nexus.bricks.snapshot.snapshot_hook import SnapshotWriteHook

        _enlist("snapshot_write", SnapshotWriteHook(_snapshot_svc))

    # ── Deferred permission buffer (Issue #1773, #1682) ────────────────
    _dpb = _ss.get("deferred_permission_buffer")
    _rebac_for_perm = _ss.get("rebac_manager")
    if _dpb is not None:
        from nexus.bricks.rebac.deferred_permission_hook import DeferredPermissionHook

        _enlist(
            "deferred_permission",
            DeferredPermissionHook(_dpb, rebac_manager=_rebac_for_perm),
        )
    else:
        # Sync fallback — same logic, runs as post-write hook instead of inline kernel code
        _hier = getattr(_rebac_for_perm, "hierarchy_manager", None) if _rebac_for_perm else None
        if _hier is not None or _rebac_for_perm is not None:
            from nexus.bricks.rebac.sync_permission_hook import SyncPermissionWriteHook

            _enlist(
                "sync_permission",
                SyncPermissionWriteHook(hierarchy_manager=_hier, rebac_manager=_rebac_for_perm),
            )

    # ── Zone writability gate (Issue #1371, #2061) ─────────────────────
    # Replaces NexusFS._check_zone_writable() — kernel should not know
    # about zone lifecycle.  PRE hooks on all mutating ops block writes
    # to zones being deprovisioned.
    _zl2 = _ss.get("zone_lifecycle")
    if _zl2 is not None:
        from nexus.services.lifecycle.zone_writability_hook import ZoneWritabilityHook

        _enlist("zone_writability", ZoneWritabilityHook(_zl2))

    # ── OBSERVE observers (Issue #900, #922) ──────────────────────────
    # FileWatcher is now Rust kernel-internal (sys_watch + dispatch_observers).
    # No Python FileWatcher registration needed.
    # StreamRemoteWatcher/StreamEventObserver also Rust kernel-internal
    # (stream_observer.rs MutationObserver). No Python wiring needed.

    # EventBus (optional): NATS/Dragonfly for distributed pub/sub.
    _event_bus = None
    _dist_cfg = getattr(nx, "_distributed_config", None)
    if _dist_cfg and getattr(_dist_cfg, "enable_events", False):
        try:
            from nexus.services.event_bus.factory import create_event_bus

            _event_bus = create_event_bus()
            nx._event_bus = _event_bus
        except Exception as exc:
            logger.warning("EventBus creation skipped: %s", exc)

    from nexus.services.event_bus.observer import EventBusObserver

    _bus_observer = EventBusObserver(event_bus=_event_bus)
    _enlist("event_bus_observer", _bus_observer)

    # RevisionTrackingObserver deleted (§10 A2): zone revision counter is now
    # a kernel primitive (AtomicU64 per zone). The kernel auto-increments on
    # sys_write/sys_unlink/sys_rename/sys_mkdir/sys_rmdir. No observer needed.

    # ── CAS GC (Issue #1320, #1772) ────────────────────────────────────
    # ref_count eliminated; reachability-based GC via CASGarbageCollector.
    # GC is owned by CASLocalBackend, metastore injected via set_metastore().

    # ── Test hooks (Issue #2) ────────────────────────────────────────
    # Only registered when NEXUS_TEST_HOOKS=true for E2E hook testing.
    import os

    if os.getenv("NEXUS_TEST_HOOKS") == "true":
        from nexus.core.test_hooks import register_test_hooks

        register_test_hooks(nx)
