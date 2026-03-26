"""NexusFS lifecycle implementations — _wire_services() / _initialize_services().

These factory-layer functions are called directly by create_nexus_fs()
in the orchestrator, keeping the kernel free of factory/bricks imports.

Linearized in PR #3371 Phase 2: partial injection eliminated.
"""

import dataclasses
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class _InitContext:
    """Context captured during _wire_services() for _initialize_services().

    Replaces the old functools.partial injection pattern. All factory-phase
    locals that _initialize_services needs are captured here.
    """

    services: dict[str, Any]
    svc_on: Callable[[str], bool]
    parse_fn: Any
    permission_checker: Any


async def _wire_services(
    nx: Any,
    *,
    services: dict[str, Any] | None = None,
    zone_id: str | None = None,
    enabled_bricks: "frozenset[str] | None" = None,
    parsing: Any = None,
    workflow_engine: Any = None,
    federation: Any = None,
) -> _InitContext:
    """Phase 1: wire service topology.  Pure memory — NO I/O.

    Creates ParsersBrick, CacheBrick, ContentCache; packs them into
    the services dict; boots wired services that need a NexusFS reference;
    binds them onto ``nx``; creates PermissionChecker.

    Returns _InitContext for _initialize_services().
    """
    from nexus.contracts.deployment_profile import DeploymentProfile as _DP
    from nexus.factory._wired import _boot_post_kernel_services
    from nexus.factory.service_routing import enlist_wired_services

    _svc = services or {}
    nx._permission_enforcer = _svc.get("permission_enforcer")  # Issue #1706: override sentinel

    _parsing = parsing if parsing is not None else nx._parse_config

    # --- ParsersBrick (owns both registries — Issue #1523) ---
    from nexus.bricks.parsers.brick import ParsersBrick

    parsers_brick = ParsersBrick(parsing_config=_parsing)
    _parse_fn = parsers_brick.create_parse_fn()

    # --- CacheBrick (owns all cache domain services — Issue #1524) ---
    from nexus.cache.brick import CacheBrick

    _cache_brick = CacheBrick(
        cache_store=nx.cache_store,
        record_store=nx._record_store,
    )

    # --- ContentCache (Issue #657) ---
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

    # Factory-created brick artifacts (not runtime services — enlisted separately)
    _brick_updates: dict[str, Any] = {
        "cache_brick": _cache_brick,
        "parse_fn": _parse_fn,
        "content_cache": _content_cache,
        "parser_registry": parsers_brick.parser_registry,
        "provider_registry": parsers_brick.provider_registry,
    }
    if workflow_engine is not None:
        _brick_updates["workflow_engine"] = workflow_engine

    # Merge factory-phase additions into the unified services dict
    _svc.update(_brick_updates)

    # --- Resolve enabled_bricks for profile gating ---
    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()

    def svc_on(name: str) -> bool:
        return name in _resolved_bricks

    # --- PermissionChecker (services layer — Issue #899, #1766) ---
    # Factory-local: _permission_checker is only needed by _register_vfs_hooks()
    # at initialize() time. Captured in _InitContext.
    from nexus.bricks.rebac.checker import PermissionChecker as _PC

    _permission_checker = _PC(
        permission_enforcer=_svc.get("permission_enforcer"),
        metadata_store=nx.metadata,
        default_context=nx._init_cred,
        enforce_permissions=nx._enforce_permissions,
    )

    # --- Boot wired services → register into ServiceRegistry ---
    _wired = await _boot_post_kernel_services(
        nx,
        nx.router,
        _svc,
        svc_on,
    )

    # Issue #1708: ServiceRegistry now has integrated lifecycle (formerly SLC).
    await enlist_wired_services(nx._service_registry, _wired)

    # Issue #1811: DriverLifecycleCoordinator is kernel-owned (created in
    # NexusFS.__init__). Root mount ("/") was added to PathRouter in
    # create_nexus_fs() before __init__ — adopt retroactively registers
    # the backend's hook_spec (fixes CAS ref_count wiring bug #1320).
    await nx._service_registry.enlist("driver_coordinator", nx._driver_coordinator)
    nx._driver_coordinator.adopt_existing_mount("/")

    # Issue #1811 Phase 2: Inject coordinator into MountService so dynamic
    # mounts go through coordinator (hook_spec registration + KernelDispatch).
    _mount_svc = getattr(_wired, "mount_service", None)
    if _mount_svc is not None:
        _mount_svc._driver_coordinator = nx._driver_coordinator

    # Enlist all services into ServiceRegistry (unified loop).
    # After this, every service is available via nx.service("name").
    # Note: permission_enforcer stays as kernel-owns DI (Issue #1815).
    # Canonical name mapping for services that need aliasing.
    _CANONICAL_ALIASES = {
        "context_branch_service": "context_branch",
    }
    for _attr, _val in _svc.items():
        if _val is None:
            continue
        _canonical = _CANONICAL_ALIASES.get(_attr, _attr)
        await nx._service_registry.enlist(_canonical, _val)

    # Federation — wire from parameter (profile-gated, created before kernel).
    if federation is not None:
        await nx._service_registry.enlist("federation", federation)
        nx._zone_mgr = federation.zone_manager  # backward compat for health checks
        logger.debug("[LINK] Federation service enlisted")

        # Upgrade lock manager: LocalLockManager → RaftLockManager
        try:
            from nexus.raft.lock_manager import RaftLockManager

            _raft_lm = RaftLockManager(nx.metadata, zone_id=zone_id or "root")
            # Find EventsService and upgrade its lock manager.
            _events_ref = nx._service_registry.service("events_service")
            _events_svc = _events_ref._service_instance if _events_ref is not None else None
            if _events_svc is not None and hasattr(_events_svc, "upgrade_lock_manager"):
                _events_svc.upgrade_lock_manager(_raft_lm)
            logger.info("[LINK] RaftLockManager upgraded into EventsService")
        except Exception as exc:
            logger.debug("[LINK] RaftLockManager upgrade skipped: %s", exc)

    # Kernel DI: _descendant_checker is a kernel component (like Linux LSM hook),
    # not an external service — inject directly onto the kernel instance.
    _dc = getattr(_wired, "descendant_checker", None)
    if _dc is not None:
        nx._descendant_checker = _dc

    # Issue #1788: Lock manager owned by EventsService (LocalLockManager by default).
    # Upgraded to RaftLockManager above if federation is available.

    # --- Register close callbacks (Issue #1793, #1789) ---
    # Services that need cleanup at close() register callbacks here.
    # Callbacks run BEFORE pillar
    # close (metadata_store, record_store) to ensure DB connections are still open.
    _wo = _svc.get("write_observer")
    if _wo is not None and hasattr(_wo, "flush_sync"):

        def _close_write_observer() -> None:
            try:
                _wo.flush_sync()
            except Exception as exc:
                logger.debug("close: write_observer flush_sync failed (best-effort): %s", exc)

        nx._close_callbacks.append(_close_write_observer)

    # Cancel PipedRecordStoreWriteObserver's consumer task on sync close.
    # Without this, the pipe consumer blocks event loop cleanup in tests.
    if _wo is not None and hasattr(_wo, "_consumer_task"):

        def _close_write_observer_task() -> None:
            task = getattr(_wo, "_consumer_task", None)
            if task is not None and not task.done():
                task.cancel()
                _wo._consumer_task = None

        nx._close_callbacks.append(_close_write_observer_task)

    # Issue #3193: Cancel the delivery worker asyncio.Task on sync close.
    # The coordinator's stop_persistent_services() is async and only runs
    # during lifespan shutdown. For sync close (tests, CLI), we cancel
    # the task directly so it doesn't block event loop cleanup.
    _dw = _svc.get("delivery_worker")
    if _dw is not None and hasattr(_dw, "_consumer_task"):

        def _close_delivery_worker() -> None:
            task = getattr(_dw, "_consumer_task", None)
            if task is not None and not task.done():
                task.cancel()
                _dw._consumer_task = None
                _dw._stopped = True

        nx._close_callbacks.append(_close_delivery_worker)

    # Issue #1801: _flush_write_observer_fn closure removed — kernel now reads
    # write_observer directly from service registry via nx.service("write_observer").

    _rebac = _svc.get("rebac_manager")
    if _rebac is not None and hasattr(_rebac, "close"):

        def _close_rebac() -> None:
            try:
                _rebac.close()
            except Exception as exc:
                logger.debug("close: rebac_manager.close() failed: %s", exc)

        nx._close_callbacks.append(_close_rebac)

    _audit = _svc.get("audit_store")
    if _audit is not None and hasattr(_audit, "close"):

        def _close_audit() -> None:
            try:
                _audit.close()
            except Exception as exc:
                logger.debug("close: audit_store.close() failed: %s", exc)

        nx._close_callbacks.append(_close_audit)

    # Issue #1792: AgentRegistry — lazy construct via ServiceRegistry.register_factory().
    # Only created on first access (ACP/TaskManager/EvictionManager need it).
    # No-agent profiles (REMOTE) never access it → never created.
    def _create_agent_registry() -> Any:
        from nexus.core.agent_registry import AgentRegistry

        _ar = AgentRegistry()
        # Wire close callback
        if hasattr(_ar, "close_all"):

            def _close_agent_registry() -> None:
                try:
                    _ar.close_all()
                except Exception as exc:
                    logger.debug("close: agent_registry.close_all() failed: %s", exc)

            nx._close_callbacks.append(_close_agent_registry)

        # Keep kernel sentinel in sync for backward compat
        nx._agent_registry = _ar
        logger.debug("[BOOT:LINK] AgentRegistry lazy-constructed on first access")
        return _ar

    nx._service_registry.register_factory("agent_registry", _create_agent_registry)

    # Issue #1801: _overlay_config_fn closure removed — kernel now reads
    # workspace_registry directly from service registry via nx.service("workspace_registry").

    # --- Deferred EvictionManager + AcpService (Issue #1792) ---
    # AgentRegistry is lazy-constructed via register_factory().
    # Accessing it here triggers construction only if EvictionManager/AcpService exist.
    _agent_ref = nx._service_registry.service("agent_registry")
    _agent_reg = _agent_ref._service_instance if _agent_ref is not None else None
    if _agent_reg is not None:
        try:
            from nexus.contracts.deployment_profile import DeploymentProfile as _DP
            from nexus.lib.performance_tuning import resolve_profile_tuning
            from nexus.services.agents.eviction_manager import EvictionManager
            from nexus.services.agents.eviction_policy import QoSEvictionPolicy
            from nexus.services.agents.resource_monitor import ResourceMonitor

            _profile_tuning = resolve_profile_tuning(_DP.FULL)
            _eviction_tuning = _profile_tuning.eviction
            _resource_monitor = ResourceMonitor(tuning=_eviction_tuning)
            _eviction_policy = QoSEvictionPolicy()
            _eviction_manager = EvictionManager(
                agent_registry=_agent_reg,
                monitor=_resource_monitor,
                policy=_eviction_policy,
                tuning=_eviction_tuning,
            )
            await nx._service_registry.enlist("eviction_manager", _eviction_manager)
            logger.debug("[BOOT:LINK] EvictionManager created (deferred, QoS-aware)")
        except Exception as exc:
            logger.warning("[BOOT:LINK] EvictionManager unavailable: %s", exc)

        try:
            from nexus.contracts.constants import ROOT_ZONE_ID
            from nexus.services.acp.service import AcpService

            _acp_service = AcpService(
                agent_registry=_agent_reg,
                zone_id=zone_id or ROOT_ZONE_ID,
            )
            await nx._service_registry.enlist("acp_service", _acp_service)
            logger.debug("[BOOT:LINK] AcpService created (deferred)")
        except Exception as exc:
            logger.warning("[BOOT:LINK] AcpService unavailable: %s", exc)

    return _InitContext(
        services=_svc,
        svc_on=svc_on,
        parse_fn=_parse_fn,
        permission_checker=_permission_checker,
    )


async def _initialize_services(
    nx: Any,
    ctx: _InitContext,
) -> None:
    """Phase 2: one-time side effects.  NO background threads.

    Prepares resources but remains static — no active threads or async loops.
    Background .start() calls are deferred to bootstrap() via callbacks.

    IPC adapter bind + mount, VFS hook registration, BLM brick
    registration, bootstrap callback registration for background workers.
    """
    # --- IPC adapter bind + mount (extracted from _boot_wired_services) ---
    from nexus.factory._wired import _initialize_wired_ipc

    # Build services dict from ServiceRegistry for IPC initialization
    _ipc_services: dict[str, Any] = {}
    _ipc_svc_fn = getattr(nx, "service", None)
    if _ipc_svc_fn is not None:
        for _ipc_name in ("ipc_storage_driver", "ipc_provisioner"):
            _ipc_val = _ipc_svc_fn(_ipc_name)
            if _ipc_val is not None:
                _ipc_services[_ipc_name] = _ipc_val
    _initialize_wired_ipc(nx, _ipc_services)

    # --- Register VFS hooks (INTERCEPT + OBSERVE — Issue #900) ---
    # Issue #1610/#1612/#1613/#1616: All hooks declare hook_spec() (duck-typed).
    # When coordinator exists, hooks are enlisted as services here and
    # dispatch-registered immediately at enlist() time.
    # _build_retroactive_hook_specs() has been deleted — hooks self-describe.
    from nexus.factory.orchestrator import _register_vfs_hooks

    await _register_vfs_hooks(
        nx,
        services=ctx.services,
        permission_checker=ctx.permission_checker,
        auto_parse=nx._parse_config.auto_parse if nx._parse_config else True,
        svc_on=ctx.svc_on,
        parse_fn=ctx.parse_fn,
    )

    # --- Register background services as bootstrap callbacks ---
    # TL directive: initialize() prepares resources but stays static.
    # bootstrap() is the only phase allowed to spawn active threads/async loops.
    #
    # Issue #1666: DeferredPermissionBuffer and EventDeliveryWorker now
    # implement PersistentService and are auto-started by the coordinator's
    # start_persistent_services() at bootstrap.  Manual callbacks deleted.

    _zl = ctx.services.get("zone_lifecycle")
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


# Backward compatibility aliases
_do_link = _wire_services
_do_initialize = _initialize_services
