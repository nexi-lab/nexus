"""NexusFS lifecycle implementations — link() / initialize() / bootstrap().

These factory-layer functions are injected into NexusFS as callables,
keeping the kernel free of factory/bricks/system_services imports.

See NexusFS.link(), NexusFS.initialize(), NexusFS.bootstrap().
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def _do_link(
    nx: Any,
    *,
    system_services: Any = None,
    zone_id: str | None = None,
    enabled_bricks: "frozenset[str] | None" = None,
    parsing: Any = None,
    workflow_engine: Any = None,
) -> None:
    """Phase 1 implementation: wire service topology.  Pure memory — NO I/O.

    Creates ParsersBrick, CacheBrick, ContentCache; packs them into
    BrickServices; boots wired services that need a NexusFS reference;
    binds them onto ``nx``; creates PermissionChecker.
    """
    from dataclasses import replace as _dc_replace

    from nexus.contracts.deployment_profile import DeploymentProfile as _DP
    from nexus.factory._wired import _boot_wired_services
    from nexus.factory.service_routing import enlist_wired_services

    _sys = system_services
    _brk = nx._brick_services
    nx._permission_enforcer = _sys.permission_enforcer  # Issue #1706: override sentinel

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

    # --- Pack factory-created bricks into BrickServices (Issue #2134) ---
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

    # --- Resolve enabled_bricks for profile gating ---
    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in _resolved_bricks

    # --- PermissionChecker (services layer — Issue #899, #1766) ---
    # Factory-local: _permission_checker is only needed by _register_vfs_hooks()
    # at initialize() time. Captured via partial — never stored on nx.
    from nexus.bricks.rebac.checker import PermissionChecker as _PC

    _permission_checker = _PC(
        permission_enforcer=_sys.permission_enforcer,
        metadata_store=nx.metadata,
        default_context=nx._default_context,
        enforce_permissions=nx._enforce_permissions,
    )

    # Issue #1740/#1765/#1766: capture factory-phase locals via partial so they
    # never touch nx.__dict__. _do_initialize receives them as keyword args.
    import functools

    nx._initialize_fn = functools.partial(
        _do_initialize,
        system_services=system_services,
        brick_on=_brick_on,
        parse_fn=_parse_fn,
        permission_checker=_permission_checker,
    )

    # --- Boot wired services → register into ServiceRegistry ---
    _wired = await _boot_wired_services(
        nx,
        nx.router,  # Issue #1767: KernelServices wrapper removed
        system_services,
        nx._brick_services,
        _brick_on,
    )

    # Issue #1708: Coordinator is always created — BLM is optional.
    # Single entry point for all service registration (no fallback path).
    from nexus.system_services.lifecycle.service_lifecycle_coordinator import (
        ServiceLifecycleCoordinator,
    )

    _blm = getattr(system_services, "brick_lifecycle_manager", None)
    coordinator = ServiceLifecycleCoordinator(nx._service_registry, _blm, nx._dispatch)
    nx._service_coordinator = coordinator
    await enlist_wired_services(coordinator, _wired)

    # Issue #1666: Register system-tier PersistentService instances.
    # These are Q3 (PersistentService) — enlist() defers start() because
    # coordinator is not yet bootstrapped (mark_bootstrapped at bootstrap).
    _dpb = getattr(system_services, "deferred_permission_buffer", None)
    if _dpb is not None:
        await coordinator.enlist("deferred_permission_buffer", _dpb)
    _dw = getattr(system_services, "delivery_worker", None)
    if _dw is not None:
        await coordinator.enlist("delivery_worker", _dw)

    # Kernel DI: _descendant_checker is a kernel component (like Linux LSM hook),
    # not an external service — inject directly onto the kernel instance.
    _dc = getattr(_wired, "descendant_checker", None)
    if _dc is not None:
        nx._descendant_checker = _dc

    # Issue #1788: inject distributed lock_manager directly (kernel knows pattern)
    nx._distributed_lock_manager = getattr(_sys, "lock_manager", None)

    # --- Register close callbacks (Issue #1793, #1789) ---
    # Services that need cleanup at close() register callbacks here instead of
    # kernel reading _system_services directly.  Callbacks run BEFORE pillar
    # close (metadata_store, record_store) to ensure DB connections are still open.
    _wo = getattr(_sys, "write_observer", None)
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
    _dw = getattr(_sys, "delivery_worker", None)
    if _dw is not None and hasattr(_dw, "_consumer_task"):

        def _close_delivery_worker() -> None:
            task = getattr(_dw, "_consumer_task", None)
            if task is not None and not task.done():
                task.cancel()
                _dw._consumer_task = None
                _dw._stopped = True

        nx._close_callbacks.append(_close_delivery_worker)

    # Issue #1771: inject _flush_write_observer_fn so kernel flush_write_observer()
    # no longer reads _system_services.
    if _wo is not None and hasattr(_wo, "flush"):

        async def _flush_wo() -> int:
            result: int = await _wo.flush()
            return result

        nx._flush_write_observer_fn = _flush_wo

    _rebac = getattr(_sys, "rebac_manager", None)
    if _rebac is not None and hasattr(_rebac, "close"):

        def _close_rebac() -> None:
            try:
                _rebac.close()
            except Exception as exc:
                logger.debug("close: rebac_manager.close() failed: %s", exc)

        nx._close_callbacks.append(_close_rebac)

    _audit = getattr(_sys, "audit_store", None)
    if _audit is not None and hasattr(_audit, "close"):

        def _close_audit() -> None:
            try:
                _audit.close()
            except Exception as exc:
                logger.debug("close: audit_store.close() failed: %s", exc)

        nx._close_callbacks.append(_close_audit)

    # Issue #1792: agent_registry close via callback (kernel-owned primitive)
    _pt = getattr(nx, "_agent_registry", None)
    if _pt is not None and hasattr(_pt, "close_all"):

        def _close_agent_registry() -> None:
            try:
                _pt.close_all()
            except Exception as exc:
                logger.debug("close: agent_registry.close_all() failed: %s", exc)

        nx._close_callbacks.append(_close_agent_registry)

    # Issue #1791: overlay config resolver — kernel calls self._overlay_config_fn(path)
    # instead of reading workspace_registry from _system_services.
    _ws_reg = getattr(_sys, "workspace_registry", None)
    if _ws_reg is not None:

        def _resolve_overlay(path: str) -> "Any":
            ws_config = _ws_reg.find_workspace_for_path(path)
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

        nx._overlay_config_fn = _resolve_overlay

    # --- Deferred EvictionManager + AcpService (Issue #1792) ---
    # AgentRegistry is a kernel-owned primitive (created in NexusFS.__init__).
    # EvictionManager and AcpService depend on it, so they are created here
    # at link() time where nx._agent_registry is available.
    _agent_reg = getattr(nx, "_agent_registry", None)
    if _agent_reg is not None:
        try:
            from nexus.contracts.deployment_profile import DeploymentProfile as _DP
            from nexus.lib.performance_tuning import resolve_profile_tuning
            from nexus.system_services.agents.eviction_manager import EvictionManager
            from nexus.system_services.agents.eviction_policy import QoSEvictionPolicy
            from nexus.system_services.agents.resource_monitor import ResourceMonitor

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
            nx._eviction_manager = _eviction_manager
            logger.debug("[BOOT:LINK] EvictionManager created (deferred, QoS-aware)")
        except Exception as exc:
            logger.warning("[BOOT:LINK] EvictionManager unavailable: %s", exc)

        try:
            from nexus.contracts.constants import ROOT_ZONE_ID
            from nexus.system_services.acp.service import AcpService

            _acp_service = AcpService(
                agent_registry=_agent_reg,
                zone_id=zone_id or ROOT_ZONE_ID,
            )
            nx._acp_service = _acp_service
            logger.debug("[BOOT:LINK] AcpService created (deferred)")
        except Exception as exc:
            logger.warning("[BOOT:LINK] AcpService unavailable: %s", exc)


async def _do_initialize(
    nx: Any,
    *,
    system_services: Any = None,
    brick_on: "Any" = None,
    parse_fn: "Any" = None,
    permission_checker: "Any" = None,
) -> None:
    """Phase 2 implementation: one-time side effects.  NO background threads.

    Prepares resources but remains static — no active threads or async loops.
    Background .start() calls are deferred to bootstrap() via callbacks.

    IPC adapter bind + mount, VFS hook registration, BLM brick
    registration, bootstrap callback registration for background workers.
    """
    # --- IPC adapter bind + mount (extracted from _boot_wired_services) ---
    from nexus.factory._wired import _initialize_wired_ipc

    _initialize_wired_ipc(nx, nx._brick_services)

    # --- Register VFS hooks (INTERCEPT + OBSERVE — Issue #900) ---
    # Issue #1610/#1612/#1613/#1616: All hooks now implement HotSwappable.
    # When coordinator exists, hooks are registered as services here and
    # dispatch-registered at bootstrap via activate_hot_swappable_services().
    # _build_retroactive_hook_specs() has been deleted — hooks self-describe.
    from nexus.factory.orchestrator import _register_vfs_hooks

    await _register_vfs_hooks(
        nx,
        system_services=system_services,
        permission_checker=permission_checker,
        auto_parse=nx._parse_config.auto_parse if nx._parse_config else True,
        brick_on=brick_on,
        parse_fn=parse_fn,
    )

    # --- BLM registration for late bricks (Issue #1704, #2991) ---
    _blm = getattr(system_services, "brick_lifecycle_manager", None)
    if _blm is not None:
        from nexus.factory._helpers import _register_late_bricks

        _cache_brick = getattr(nx._brick_services, "cache_brick", None)
        _register_late_bricks(_blm, {"cache": _cache_brick})

    # --- Register background services as bootstrap callbacks ---
    # TL directive: initialize() prepares resources but stays static.
    # bootstrap() is the only phase allowed to spawn active threads/async loops.
    #
    # Issue #1666: DeferredPermissionBuffer and EventDeliveryWorker now
    # implement PersistentService and are auto-started by the coordinator's
    # start_persistent_services() at bootstrap.  Manual callbacks deleted.

    _zl = getattr(system_services, "zone_lifecycle", None) if system_services else None
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
