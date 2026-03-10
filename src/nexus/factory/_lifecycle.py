"""NexusFS lifecycle implementations — link() / initialize() / bootstrap().

These factory-layer functions are injected into NexusFS as callables,
keeping the kernel free of factory/bricks/system_services imports.

See NexusFS.link(), NexusFS.initialize(), NexusFS.bootstrap().
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _do_link(
    nx: Any,
    *,
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
    from nexus.factory.service_routing import populate_service_registry

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

    # Update kernel-side references set by __init__ from original BrickServices
    nx.parser_registry = parsers_brick.parser_registry
    nx.provider_registry = parsers_brick.provider_registry
    nx._virtual_view_parse_fn = _parse_fn
    nx._parsers_brick = parsers_brick  # kept for BLM registration in initialize()

    # --- Resolve enabled_bricks for profile gating ---
    _resolved_bricks = enabled_bricks
    if _resolved_bricks is None:
        _resolved_bricks = _DP.FULL.default_bricks()

    def _brick_on(name: str) -> bool:
        return name in _resolved_bricks

    # --- Boot wired services → register into ServiceRegistry ---
    _wired = _boot_wired_services(
        nx,
        nx._kernel_services,
        nx._system_services,
        nx._brick_services,
        _brick_on,
    )
    populate_service_registry(nx._service_registry, _wired)

    # Kernel DI: _descendant_checker is a kernel component (like Linux LSM hook),
    # not an external service — inject directly onto the kernel instance.
    _dc = getattr(_wired, "descendant_checker", None)
    if _dc is not None:
        nx._descendant_checker = _dc

    # --- PermissionChecker (services layer — Issue #899) ---
    from nexus.bricks.rebac.checker import PermissionChecker as _PC

    nx._permission_checker = _PC(
        permission_enforcer=nx._permission_enforcer,
        metadata_store=nx.metadata,
        default_context=nx._default_context,
        enforce_permissions=nx._enforce_permissions,
    )


def _do_initialize(nx: Any) -> None:
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
    from nexus.factory.orchestrator import _register_vfs_hooks

    _register_vfs_hooks(
        nx,
        permission_checker=nx._permission_checker,
        auto_parse=nx._parse_config.auto_parse if nx._parse_config else True,
    )

    # --- BLM registration for parsers/cache bricks (Issue #1704) ---
    _blm = getattr(nx._system_services, "brick_lifecycle_manager", None)
    if _blm is not None:
        _cache_brick = getattr(nx._brick_services, "cache_brick", None)
        _parsers_brick = getattr(nx, "_parsers_brick", None)
        if _parsers_brick is not None:
            _blm.register("parsers", _parsers_brick, protocol_name="ParsersProtocol")
        if _cache_brick is not None:
            _blm.register("cache", _cache_brick, protocol_name="CacheProtocol")

    # --- Register background services as bootstrap callbacks ---
    # TL directive: initialize() prepares resources but stays static.
    # bootstrap() is the only phase allowed to spawn active threads/async loops.

    _dpb = nx._deferred_permission_buffer
    if _dpb is not None and hasattr(_dpb, "start"):

        async def _start_dpb() -> None:
            _dpb.start()
            logger.debug("[LIFECYCLE] DeferredPermissionBuffer started (bootstrap)")

        nx._bootstrap_callbacks.append(_start_dpb)

    _dw = getattr(nx._system_services, "delivery_worker", None)
    if _dw is not None and hasattr(_dw, "start"):

        async def _start_dw() -> None:
            _dw.start()
            logger.debug("[LIFECYCLE] EventDeliveryWorker started (bootstrap)")

        nx._bootstrap_callbacks.append(_start_dw)

    _zl = nx._zone_lifecycle
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
