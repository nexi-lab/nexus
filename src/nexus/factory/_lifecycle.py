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
    security: Any = None,
) -> _InitContext:
    """Phase 1: wire service topology.  Pure memory — NO I/O.

    Creates ParsersBrick, CacheBrick, ContentCache; packs them into
    the services dict; boots wired services that need a NexusFS reference;
    binds them onto ``nx``; creates PermissionChecker.

    Returns _InitContext for _initialize_services().
    """
    from nexus.contracts.deployment_profile import DeploymentProfile as _DP
    from nexus.factory._wired import _boot_post_kernel_services
    from nexus.factory.service_routing import enlist_services

    _svc = services or {}

    # Set kernel zone identity from factory (federation provides actual zone_id)
    if zone_id is not None:
        nx._zone_id = zone_id

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

    # Factory-created brick artifacts (not runtime services — enlisted separately)
    _brick_updates: dict[str, Any] = {
        "cache_brick": _cache_brick,
        "parse_fn": _parse_fn,
        "parser_registry": parsers_brick.parser_registry,
        "provider_registry": parsers_brick.provider_registry,
    }
    if workflow_engine is not None:
        _brick_updates["workflow_engine"] = workflow_engine

    # --- Resolve enabled_bricks for profile gating ---
    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()
    _brick_updates["enabled_bricks"] = _resolved_bricks

    # Merge factory-phase additions into the unified services dict
    _svc.update(_brick_updates)

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
        enforce_permissions=nx._perm_config.enforce,
    )

    # --- Boot wired services → register into ServiceRegistry ---
    _wired = await _boot_post_kernel_services(
        nx,
        nx.router,
        _svc,
        svc_on,
        security_config=security,
    )

    # Issue #1708: ServiceRegistry now has integrated lifecycle (formerly SLC).
    await enlist_services(nx, _wired)

    # Issue #1811: DriverLifecycleCoordinator is kernel-owned (created in
    # NexusFS.__init__). Root mount ("/") registered via sys_setattr(DT_MOUNT)
    # + _store_mount_info() in create_nexus_fs().
    nx.sys_setattr("/__sys__/services/driver_coordinator", service=nx._driver_coordinator)

    # Issue #1811 Phase 2: Inject coordinator into MountService so dynamic
    # mounts go through coordinator (hook_spec registration + KernelDispatch).
    _mount_svc = _wired.get("mount_service")
    if _mount_svc is not None:
        _mount_svc._driver_coordinator = nx._driver_coordinator

    # Enlist all system+brick services into ServiceRegistry.
    # Canonical name mapping consolidated in service_routing.py.
    await enlist_services(nx, _svc)

    # R20.18.5: federation is kernel-internal now. The federation
    # parameter is vestigial (always None post-cutover). Kernel::new()
    # reads env vars and bootstraps raft::ZoneManager in Rust. DLC
    # wiring is driven by the per-zone mount_apply_cb installed by
    # Kernel::install_federation_mount_coherence — no Python bridge.
    _ = federation  # kept in signature for caller compat; unused

    # descendant_checker is now accessed via PermissionCheckHook (KernelDispatch INTERCEPT).
    # No kernel DI needed — PermissionCheckHook holds the reference internally.

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

    # Cancel RecordStoreWriteObserver's debounce timer + flush on sync close.
    if _wo is not None and hasattr(_wo, "cancel"):

        def _close_write_observer_cancel() -> None:
            try:
                _wo.cancel()
            except Exception as exc:
                logger.debug("close: write_observer cancel failed (best-effort): %s", exc)

        nx._close_callbacks.append(_close_write_observer_cancel)

    # Issue #3193: Cancel the delivery worker asyncio.Task on sync close.
    # The coordinator's stop_background_services() is async and only runs
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

    # rebac_manager.close() and audit_store.close() are now handled by
    # ServiceRegistry.close_all_services() — no manual callbacks needed.

    # Issue #1792: AgentRegistry, EvictionManager, AcpService are now
    # constructed in _boot_post_kernel_services (_wired.py) by the services
    # that need them. No factory lazy pattern needed.

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
    from nexus.factory.service_routing import enlist_services

    # Build services dict from ServiceRegistry for IPC initialization
    _ipc_services: dict[str, Any] = {}
    _ipc_svc_fn = getattr(nx, "service", None)
    if _ipc_svc_fn is not None:
        for _ipc_name in ("ipc_zone_id", "ipc_provisioner"):
            _ipc_val = _ipc_svc_fn(_ipc_name)
            if _ipc_val is not None:
                # service_lookup() returns raw instances — no unwrapping needed.
                _ipc_services[_ipc_name] = _ipc_val
    _initialize_wired_ipc(nx, _ipc_services)
    # _initialize_wired_ipc may have created ipc_provisioner — enlist newly
    # produced services so /api/v2/ipc/* and lifespan IPC startup can resolve them.
    await enlist_services(nx, _ipc_services)

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

    # Background services (DeferredPermissionBuffer, EventDeliveryWorker,
    # ZoneLifecycleService) implement BackgroundService and are auto-started
    # by the coordinator's start_background_services() at bootstrap.
    # No manual _bootstrap_callbacks needed.


# Backward compatibility aliases
_do_link = _wire_services
_do_initialize = _initialize_services
