"""FastAPI server for Nexus filesystem.

This module implements an async HTTP server using FastAPI that exposes all
NexusFileSystem operations through a JSON-RPC API. This provides significantly
better performance under concurrent load compared to the ThreadingHTTPServer.

Performance improvements:
- Async database operations (asyncpg/aiosqlite)
- Connection pooling
- Non-blocking I/O
- 10-50x throughput improvement under concurrent load

The server exposes the following API contract:
- POST /api/nfs/{method} - JSON-RPC endpoints
- GET /health - Health check
- GET /api/auth/whoami - Authentication info

Example:
    from nexus.server.fastapi_server import create_app, run_server

    app = create_app(nexus_fs, database_url="postgresql://...")
    run_server(app, host="0.0.0.0", port=2026)
"""

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
)
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Route as _StarletteRoute

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import (
    NexusError,
)
from nexus.server.auth.oauth_init import (  # noqa: E402
    initialize_oauth_provider as _initialize_oauth_provider,
)
from nexus.server.dependencies import (  # noqa: E402
    require_auth,
)
from nexus.server.error_handlers import (  # noqa: E402
    nexus_error_handler as _nexus_error_handler,
)
from nexus.server.rate_limiting import (  # noqa: E402
    RATE_LIMIT_ANONYMOUS,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_PREMIUM,
    _get_rate_limit_key,
    _rate_limit_exceeded_handler,
)
from nexus.server.rpc.discovery import (  # noqa: E402
    discover_exposed_methods as _discover_exposed_methods,
)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)

# Module-level limiter instance; initialized in create_app().
limiter: Limiter

# ============================================================================
# Thread Pool Utilities (Issue #932)
# ============================================================================

T = TypeVar("T")


async def to_thread_with_timeout(
    func: Callable[..., T],
    *args: Any,
    timeout: float | None = None,
    **kwargs: Any,
) -> T:
    """Run sync function in thread with timeout.

    Wraps asyncio.to_thread() with asyncio.wait_for() to prevent thread pool
    exhaustion from slow operations (Issue #932).

    Args:
        func: Sync function to run in thread
        *args: Positional arguments for func
        timeout: Timeout in seconds. Falls back to app.state.operation_timeout
            if None and _fastapi_app is initialized, otherwise defaults to 30.0.
        **kwargs: Keyword arguments for func

    Returns:
        Result from func

    Raises:
        TimeoutError: If operation exceeds timeout
    """
    if timeout is not None:
        effective_timeout = timeout
    elif _fastapi_app is not None:
        effective_timeout = getattr(_fastapi_app.state, "operation_timeout", 30.0)
    else:
        effective_timeout = 30.0
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=effective_timeout,
        )
    except TimeoutError:
        raise TimeoutError(f"Operation timed out after {effective_timeout}s") from None


# ============================================================================
# Issue #3778: SANDBOX HTTP allowlist
# ============================================================================

SANDBOX_HTTP_ALLOWLIST: frozenset[str] = frozenset(
    {
        # Issue #3778: SANDBOX HTTP surface
        "/health",
        "/api/v2/features",
        # FastAPI built-ins
        "/openapi.json",
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
    }
)


def _filter_routes_for_sandbox(app: "FastAPI") -> None:
    """Issue #3778: remove every `Route` not in SANDBOX_HTTP_ALLOWLIST.

    Idempotent. Leaves `Mount`s, `WebSocketRoute`s, and startup/shutdown
    event handlers untouched — only path-bound `Route` instances are
    filtered. Called once after all routers have been included, when the
    profile is sandbox.
    """
    kept = []
    for r in app.router.routes:
        if isinstance(r, _StarletteRoute) and r.path not in SANDBOX_HTTP_ALLOWLIST:
            continue
        kept.append(r)
    app.router.routes = kept


# ============================================================================
# Application State (Issue #1288: Eliminated AppState class)
# ============================================================================
#
# All application state is stored on FastAPI's ``app.state`` namespace,
# populated during ``create_app()``.  Kernel code (route handlers, RPC
# dispatch, lifespan) accesses state via ``_fastapi_app.state``.
# Extracted domain routers in ``api/v1/routers/`` use typed ``Depends()``
# functions from ``api/v1/dependencies.py`` instead.
#
# Module-level reference to the FastAPI app instance.
# Set once during ``create_app()``.
_fastapi_app: FastAPI | None = None

# Lifespan is now managed by the modular lifespan/ package (Issue #2049).

# ============================================================================
# Application Factory
# ============================================================================


def create_app(
    nexus_fs: "NexusFS",
    api_key: str | None = None,
    auth_provider: Any = None,
    database_url: str | None = None,
    thread_pool_size: int | None = None,
    operation_timeout: float | None = None,
    data_dir: str | None = None,
) -> FastAPI:
    """Create FastAPI application.

    Args:
        nexus_fs: NexusFS instance
        api_key: Static API key for authentication
        auth_provider: Auth provider instance
        database_url: Database URL for async operations
        thread_pool_size: Thread pool size for sync operations (default: 200)
        operation_timeout: Timeout for sync operations in seconds (default: 30.0)
        data_dir: Server data directory for persistent storage

    Returns:
        Configured FastAPI application
    """
    global _fastapi_app

    # Use the modular lifespan orchestrator (Issue #2049)
    from nexus.server.lifespan import lifespan as _modular_lifespan

    # Create app first so we can store state on it
    app = FastAPI(
        title="Nexus RPC Server",
        description="Nexus = filesystem/context plane.",
        version="1.0.0",
        lifespan=_modular_lifespan,
    )

    # Set module-level reference for kernel code access
    _fastapi_app = app

    # Initialize all app.state fields with typed defaults (Issue #2135)
    from nexus.server.app_state import init_app_state

    init_app_state(
        app,
        nexus_fs=nexus_fs,
        api_key=api_key,
        auth_provider=auth_provider,
        database_url=database_url,
        data_dir=data_dir,
    )

    # Issue #1399: BrickContainer for DI (auth brick + future bricks)
    from nexus.lib.brick_container import BrickContainer

    app.state.brick_container = BrickContainer()
    if auth_provider is not None:
        from nexus.bricks.auth.protocol import AuthBrickProtocol

        if isinstance(auth_provider, AuthBrickProtocol):
            app.state.brick_container.register(AuthBrickProtocol, auth_provider)

    # Deployment profile (Issue #1389, #1708, #3778 R3):
    # Resolve from the NexusFS's attached `_config.profile` when available so
    # the SANDBOX route allowlist is enforced even when the caller selected
    # sandbox via the config object/CLI without exporting NEXUS_PROFILE into
    # env. Env is used only as a fallback when no config is attached.
    from nexus.contracts.deployment_profile import DeploymentProfile, resolve_enabled_bricks

    _cfg_profile = None
    _nx_cfg_for_profile = getattr(nexus_fs, "_config", None)
    if _nx_cfg_for_profile is not None:
        _cfg_profile_val = getattr(_nx_cfg_for_profile, "profile", None)
        if _cfg_profile_val:
            _cfg_profile = str(_cfg_profile_val)

    _profile_str = _cfg_profile or os.environ.get("NEXUS_PROFILE", "full")
    if _profile_str == "auto":
        from nexus.lib.device_capabilities import detect_capabilities, suggest_profile

        _caps = detect_capabilities()
        _profile = suggest_profile(_caps)
        logger.info(
            "Auto-detected profile: %s (RAM=%dMB, GPU=%s, cores=%d)",
            _profile,
            _caps.memory_mb,
            _caps.has_gpu,
            _caps.cpu_cores,
        )
    else:
        try:
            _profile = DeploymentProfile(_profile_str)
        except ValueError:
            logger.warning("Unknown NEXUS_PROFILE '%s', defaulting to 'full'", _profile_str)
            _profile = DeploymentProfile.FULL
        # Warn if explicit profile may exceed device capabilities
        from nexus.lib.device_capabilities import (
            detect_capabilities as _detect_caps,
        )
        from nexus.lib.device_capabilities import (
            warn_if_profile_exceeds_device,
        )

        _caps = _detect_caps()
        warn_if_profile_exceeds_device(_profile, _caps)

    # Apply FeaturesConfig overrides (Issue #1389 — was unused in server)
    _features_overrides: dict[str, bool] = {}
    _nx_config = getattr(nexus_fs, "_config", None)
    if _nx_config is not None and hasattr(_nx_config, "features") and _nx_config.features:
        _features_overrides = _nx_config.features.to_overrides()

    app.state.deployment_profile = _profile.value
    app.state.enabled_bricks = resolve_enabled_bricks(_profile, overrides=_features_overrides)

    # Performance tuning (Issue #2071): resolve per-profile thresholds
    app.state.profile_tuning = _profile.tuning()

    # Expose RecordStoreABC and its session factories on app.state (if available).
    # This is the canonical way for async endpoints to get database sessions
    # without bypassing the RecordStore abstraction with raw URLs.
    _record_store = getattr(nexus_fs, "_record_store", None)
    app.state.record_store = _record_store
    if _record_store is not None:
        try:
            app.state.async_session_factory = _record_store.async_session_factory
        except NotImplementedError:
            app.state.async_session_factory = None
    else:
        app.state.async_session_factory = None

    # Expose RecordStoreABC on app.state (Issue #2200).
    # This is the canonical way for endpoints to access the storage pillar.
    app.state.record_store = _record_store

    # Expose sync session_factory from RecordStoreABC (Issue #1519).
    # Kept for backward compatibility with handlers not yet migrated.
    if _record_store is not None:
        app.state.session_factory = _record_store.session_factory
    elif (
        nexus_fs is not None
        and hasattr(nexus_fs, "SessionLocal")
        and nexus_fs.SessionLocal is not None
    ):
        app.state.session_factory = nexus_fs.SessionLocal
    else:
        app.state.session_factory = None

    # Expose read replica factories (Issue #725).
    # Read-only routes (graph, search) use these for read replica routing.
    if _record_store is not None:
        app.state.read_session_factory = _record_store.read_session_factory
        try:
            app.state.async_read_session_factory = _record_store.async_read_session_factory
        except NotImplementedError:
            app.state.async_read_session_factory = None
    else:
        app.state.read_session_factory = None
        app.state.async_read_session_factory = None

    # Expose services on app.state so routers never reach into
    # NexusFS private attributes (Issue #701).
    # Prefer the ServiceRegistry lookup (``nexus_fs.service(...)``) because
    # the private ``_rebac_manager`` attribute is None in deployments where
    # the brick is registered on-demand; the registry returns the live
    # instance in that case.
    def _resolve_service(name: str, private_attr: str) -> Any:
        if hasattr(nexus_fs, "service"):
            svc = nexus_fs.service(name)
            if svc is not None:
                return svc
        return getattr(nexus_fs, private_attr, None)

    app.state.rebac_manager = _resolve_service("rebac_manager", "_rebac_manager")
    app.state.entity_registry = _resolve_service("entity_registry", "_entity_registry")
    app.state.namespace_manager = _resolve_service("namespace_manager", "_namespace_manager")

    # Thread pool and timeout settings (Issue #932, #2071)
    _tuning_pool_size = str(app.state.profile_tuning.concurrency.thread_pool_size)
    app.state.thread_pool_size = thread_pool_size or int(
        os.environ.get("NEXUS_THREAD_POOL_SIZE", _tuning_pool_size)
    )
    app.state.operation_timeout = operation_timeout or float(
        os.environ.get("NEXUS_OPERATION_TIMEOUT", "30.0")
    )

    # Discover exposed methods — includes brick + RPC services (Issue #2035, Follow-up 1)
    # Services with @rpc_expose override kernel stubs (later sources win).
    if nexus_fs is not None:
        _rpc_sources: list[Any] = []
        for _svc_name in (
            "mcp",
            "oauth",
            "mount",
            "search",
            "share_link",
            "rebac",
        ):
            _brick_svc = nexus_fs.service(_svc_name)
            if _brick_svc is not None:
                _rpc_sources.append(_brick_svc)
        _version_svc = getattr(nexus_fs, "version_service", None)
        if _version_svc is not None:
            _rpc_sources.append(_version_svc)
        # AgentRPCService
        _agent_rpc = nexus_fs.service("agent_rpc")
        if _agent_rpc is not None:
            _rpc_sources.append(_agent_rpc)
        # WorkspaceRPCService
        _workspace_rpc = nexus_fs.service("workspace_rpc")
        if _workspace_rpc is not None:
            _rpc_sources.append(_workspace_rpc)
        # AcpRPCService
        _acp_rpc = nexus_fs.service("acp_rpc")
        if _acp_rpc is not None:
            _rpc_sources.append(_acp_rpc)
        # Issue #841: MetadataExportService lives outside kernel
        try:
            from nexus.factory import create_metadata_export_service

            _meta_export_svc = create_metadata_export_service(nexus_fs)
            if _meta_export_svc is not None:
                _rpc_sources.append(_meta_export_svc)
        except Exception as _exc:
            logger.debug("MetadataExportService unavailable: %s", _exc)
        # Issue #1410: VersionService @rpc_expose methods (moved from NexusFS)
        _version_svc = nexus_fs.service("version_service") if hasattr(nexus_fs, "service") else None
        if _version_svc is not None:
            _rpc_sources.append(_version_svc)
        # Issue #1520: FederationRPCService — zone lifecycle, share/join, mounts
        _fed = nexus_fs.service("federation") if hasattr(nexus_fs, "service") else None
        if _fed is not None:
            from nexus.server.rpc.services.federation_rpc import FederationRPCService

            _rpc_sources.append(FederationRPCService(_fed))
        # Lock syscalls (sys_lock, sys_unlock, lock_info, lock_list, etc.)
        # are @rpc_expose on NexusFS — auto-discovered by _discover_exposed_methods.
        # LocksRPCService deleted — no separate RPC service needed.
        # --- Pay (Issue #1133) ---
        try:
            from nexus.bricks.pay import CreditsService
            from nexus.server.rpc.services.pay_rpc import PayRPCService

            _rpc_sources.append(PayRPCService(CreditsService()))
        except Exception as _exc:
            logger.debug("PayRPCService unavailable: %s", _exc)
        # --- Audit (Issue #1133) ---
        _record_store = getattr(nexus_fs, "_record_store", None)
        if _record_store is not None:
            try:
                from nexus.server.rpc.services.audit_rpc import AuditRPCService
                from nexus.storage.exchange_audit_logger import ExchangeAuditLogger

                _rpc_sources.append(
                    AuditRPCService(ExchangeAuditLogger(record_store=_record_store))
                )
            except Exception as _exc:
                logger.debug("AuditRPCService unavailable: %s", _exc)
        # --- Governance (Issue #1133) ---
        _svc_fn = getattr(nexus_fs, "service", None)
        if _svc_fn is not None:
            _anomaly = _svc_fn("governance_anomaly_service")
            _collusion = _svc_fn("governance_collusion_service")
            if _anomaly is not None or _collusion is not None:
                from nexus.server.rpc.services.governance_rpc import GovernanceRPCService

                _rpc_sources.append(GovernanceRPCService(_anomaly, _collusion))
        # --- Events (Issue #1133) ---
        if _record_store is not None:
            try:
                from nexus.server.rpc.services.events_rpc import EventsRPCService
                from nexus.services.event_log.replay import EventReplayService

                _evt_signal = None
                _rpc_sources.append(
                    EventsRPCService(EventReplayService(_record_store, event_signal=_evt_signal))
                )
            except Exception as _exc:
                logger.debug("EventsRPCService unavailable: %s", _exc)
        # --- Snapshots (Issue #1133) ---
        _snap = nexus_fs.service("snapshot_service") if hasattr(nexus_fs, "service") else None
        if _snap is not None:
            from nexus.server.rpc.services.snapshots_rpc import SnapshotsRPCService

            _rpc_sources.append(SnapshotsRPCService(_snap))
        app.state.exposed_methods = _discover_exposed_methods(nexus_fs, *_rpc_sources)
    else:
        logger.info("create_app() started without NexusFS; service discovery disabled")
        app.state.exposed_methods = {}

    # Defaults for optional services are set by init_app_state() above (Issue #2135)

    # Issue #2168: startup tracker for k8s health probes
    from nexus.server.health import StartupTracker

    app.state.startup_tracker = StartupTracker()

    # Issue #2168: startup tracker for k8s health probes
    from nexus.server.health import StartupTracker

    app.state.startup_tracker = StartupTracker()

    # Initialize subscription manager if we have a metadata store
    try:
        if nexus_fs is not None and hasattr(nexus_fs, "SessionLocal"):
            from nexus.server.subscriptions import (
                SubscriptionManager,
                set_subscription_manager,
            )

            app.state.subscription_manager = SubscriptionManager(
                nexus_fs.SessionLocal,
                webhook_timeout=app.state.profile_tuning.network.webhook_timeout,
            )
            set_subscription_manager(app.state.subscription_manager)
            # Issue #625: Forward subscription_manager to workflow dispatch service
            wds = getattr(app.state, "workflow_dispatch", None)
            if wds is not None and hasattr(wds, "set_subscription_manager"):
                wds.set_subscription_manager(app.state.subscription_manager)
            # Issue #914: Inject getter into delivery worker (fixes services→server import)
            from nexus.server.subscriptions import get_subscription_manager

            # Issue #1771: access delivery_worker via ServiceRegistry
            _dw = nexus_fs.service("delivery_worker") if nexus_fs else None
            if _dw is not None:
                _dw._subscription_manager_getter = get_subscription_manager
            logger.info("Subscription manager initialized and injected into NexusFS")
    except Exception as e:
        logger.warning(f"Failed to initialize subscription manager: {e}")

    # Add CORS middleware (Issue #1596: env-based allowlist, never wildcard + credentials)
    #
    # OAuth cookie-binding note: ``/auth/oauth/google/authorize`` sets an
    # HttpOnly ``nexus_oauth_binding`` cookie that must come back on
    # ``/auth/oauth/check`` and ``/auth/oauth/callback``. For split-origin
    # setups (frontend on :5173 or :3000, server elsewhere) the browser only
    # persists and re-sends that cookie when BOTH of these are true:
    #   (a) the fetch from the frontend uses ``credentials: 'include'``;
    #   (b) this CORS middleware echoes the origin back AND sets
    #       ``Access-Control-Allow-Credentials: true``.
    #
    # ``allow_origins`` here is always an explicit allowlist — never "*" —
    # so enabling credentials on the *server* side is safe by CORS spec
    # (the wildcard+credentials combination flagged by Issue #1596 is
    # structurally impossible below). We therefore enable credentials
    # unconditionally; otherwise the OAuth binding cookie silently drops
    # in default dev wiring and callback fails with "invalid, expired, or
    # unbound — possible CSRF attack".
    _cors_origins_raw = os.environ.get("CORS_ORIGINS", "")
    _cors_origins: list[str] = (
        [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
        if _cors_origins_raw
        else ["http://localhost:3000", "http://localhost:5173"]
    )
    if "*" in _cors_origins:
        # Defensive: the allowlist must never be wildcard with credentials=True.
        raise RuntimeError(
            "CORS_ORIGINS=* is not allowed — credentials require explicit origins. "
            "Set CORS_ORIGINS to a comma-separated list of allowed origins."
        )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Zone-ID", "X-Request-ID"],
    )

    # Add Gzip compression middleware (60-80% response size reduction)
    # Only compress responses > 1000 bytes, compression level 6 (good balance)
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

    # Initialize rate limiter (Issue #780)
    # Rate limiting is DISABLED by default for better performance
    # Set NEXUS_RATE_LIMIT_ENABLED=true to enable rate limiting
    import nexus.server.rate_limiting as _rate_limiting_mod

    global limiter
    rate_limit_enabled = os.environ.get("NEXUS_RATE_LIMIT_ENABLED", "true").lower() not in (
        "false",
        "0",
        "no",
    )
    from nexus.lib.env import get_dragonfly_url, get_redis_url

    redis_url = get_redis_url() or get_dragonfly_url()

    limiter = Limiter(
        key_func=_get_rate_limit_key,
        default_limits=[RATE_LIMIT_AUTHENTICATED] if rate_limit_enabled else [],
        headers_enabled=rate_limit_enabled,
        storage_uri=redis_url,
        strategy="fixed-window",
        enabled=rate_limit_enabled,
    )
    # Keep the canonical module in sync so any code importing from rate_limiting gets
    # the initialized Limiter instance, not the bare type annotation.
    _rate_limiting_mod.limiter = limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Add SlowAPI middleware so default_limits and rate-limit headers are applied
    # to all endpoints (not just those with explicit @limiter.limit() decorators).
    if rate_limit_enabled:
        from slowapi.middleware import SlowAPIMiddleware

        app.add_middleware(SlowAPIMiddleware)

    # Register Nexus exception handlers for error classification

    app.add_exception_handler(NexusError, _nexus_error_handler)

    if rate_limit_enabled:
        storage_type = "Redis/Dragonfly" if redis_url else "in-memory"
        logger.info(
            f"Rate limiting enabled ({storage_type}) - "
            f"Anonymous: {RATE_LIMIT_ANONYMOUS}, "
            f"Authenticated: {RATE_LIMIT_AUTHENTICATED}, "
            f"Premium: {RATE_LIMIT_PREMIUM}"
        )
    else:
        logger.warning("Rate limiting is DISABLED (set NEXUS_RATE_LIMIT_ENABLED=true to re-enable)")

    # Initialize authentication provider for user registration/login endpoints
    if auth_provider is not None:
        try:
            # Extract DatabaseLocalAuth from DiscriminatingAuthProvider if needed
            from nexus.bricks.auth.providers.base import AuthProvider
            from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth
            from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider
            from nexus.server.auth.auth_routes import set_auth_provider

            local_auth_provider: AuthProvider | None = None
            if isinstance(auth_provider, DatabaseLocalAuth):
                local_auth_provider = auth_provider
            elif isinstance(auth_provider, DiscriminatingAuthProvider):
                # Extract JWT provider from DiscriminatingAuthProvider
                local_auth_provider = auth_provider.jwt_provider

            if local_auth_provider and isinstance(local_auth_provider, DatabaseLocalAuth):
                set_auth_provider(local_auth_provider)
                logger.info("DatabaseLocalAuth provider registered for user authentication")
            else:
                logger.debug(
                    f"Auth provider is {type(auth_provider).__name__}, not DatabaseLocalAuth. "
                    "User registration/login endpoints will not be available."
                )
        except ImportError as e:
            logger.warning(f"Failed to import auth routes: {e}")
        except Exception as e:
            logger.warning(f"Failed to register auth provider: {e}")

    # Register routes
    _register_routes(app)

    # V1 API has been sunset (#2056) — all endpoints moved to v2

    # Register NexusFS instance for zone routes, migration, and user provisioning.
    # This must happen unconditionally (not only when OAuth is configured).
    try:
        if nexus_fs is not None:
            from nexus.server.auth.auth_routes import set_nexus_instance

            set_nexus_instance(nexus_fs)
            logger.info("NexusFS instance registered for zone management")
    except Exception as e:
        logger.warning(f"Failed to register NexusFS instance: {e}")

    # Initialize OAuth provider if credentials are available
    _initialize_oauth_provider(nexus_fs, auth_provider)

    # Prometheus metrics middleware and endpoint (Issue #761)
    try:
        from nexus.server.metrics import PrometheusMiddleware, metrics_endpoint

        app.add_middleware(PrometheusMiddleware)
        app.add_route("/metrics", metrics_endpoint, methods=["GET"])
    except ImportError:
        pass

    # Register QueryObserver → Prometheus collector bridge (Issue #762)
    try:
        from prometheus_client import REGISTRY

        from nexus.server.pg_metrics_collector import QueryObserverCollector

        obs_sub = app.state.observability_subsystem
        if obs_sub is not None:
            REGISTRY.register(QueryObserverCollector(obs_sub.observer))
    except ImportError:
        pass
    except Exception:
        logger.warning("Failed to register QueryObserverCollector", exc_info=True)

    # Instrument FastAPI with OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import instrument_fastapi_app

        instrument_fastapi_app(app)
    except ImportError:
        pass

    # Issue #3778: SANDBOX profile restricts HTTP surface. Gate on the
    # resolved enum (not the raw string) so invalid/unknown env values that
    # fell through to DeploymentProfile.FULL above don't accidentally enable
    # or skip the allowlist.
    if _profile == DeploymentProfile.SANDBOX:
        _filter_routes_for_sandbox(app)

    return app


def _register_routes(app: FastAPI) -> None:
    """Register all routes."""

    # Features endpoint (Issue #1389) — public, rate-limit exempt
    # ---- Core API routers (extracted from inline endpoints, #1602) ----
    from nexus.server.api.core.debug import router as debug_router
    from nexus.server.api.core.features import router as features_router
    from nexus.server.api.core.health import router as health_router
    from nexus.server.api.core.rpc import router as rpc_router
    from nexus.server.api.core.streaming import router as streaming_router

    app.include_router(health_router)

    # Issue #2168: k8s-style health probes (/healthz/live, /healthz/ready, /healthz/startup)
    from nexus.server.health.probes import router as probes_router

    app.include_router(probes_router)
    app.include_router(features_router)
    app.include_router(debug_router)
    app.include_router(streaming_router)
    app.include_router(rpc_router)

    # Authentication routes — fail fast. A server that boots without auth
    # endpoints looks healthy but silently 404s every login/OAuth request,
    # which is worse than not starting. If a deployment genuinely doesn't
    # want auth routes (rare — isolated internal service), set
    # ``NEXUS_ALLOW_MISSING_AUTH_ROUTES=true`` to preserve the old tolerant
    # behavior.
    try:
        from nexus.server.auth.auth_routes import router as auth_router

        app.include_router(auth_router)
        logger.info("Authentication routes registered")
    except ImportError as e:
        if os.environ.get("NEXUS_ALLOW_MISSING_AUTH_ROUTES", "").lower() in ("true", "1", "yes"):
            logger.warning(
                f"Failed to import auth routes: {e}. Proceeding because "
                "NEXUS_ALLOW_MISSING_AUTH_ROUTES is set. OAuth endpoints will not be available."
            )
        else:
            raise RuntimeError(
                f"Failed to import auth routes ({e}). Missing a required dependency? "
                "Set NEXUS_ALLOW_MISSING_AUTH_ROUTES=true to start without auth routes."
            ) from e

    # Zone management routes
    try:
        from nexus.server.auth.zone_routes import router as zone_router

        app.include_router(zone_router)
        logger.info("Zone management routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import zone routes: {e}. Zone management unavailable.")

    # Test hooks REST API (Issue #2) — only when NEXUS_TEST_HOOKS=true
    if os.getenv("NEXUS_TEST_HOOKS") == "true":
        try:
            from nexus.core.test_hooks import build_test_hooks_router

            app.include_router(build_test_hooks_router())
            logger.info("Test hooks routes registered (NEXUS_TEST_HOOKS=true)")
        except ImportError as e:
            logger.warning(f"Failed to import test hooks router: {e}")

    # API v2 routes — centralized registration via versioning module (#995)
    from nexus.server.api.v2.versioning import (
        DeprecationMiddleware,
        VersionHeaderMiddleware,
        build_v2_registry,
        register_v2_routers,
    )

    v2_registry = build_v2_registry(
        nexus_fs_getter=lambda: app.state.nexus_fs,
        chunked_upload_service_getter=lambda: app.state.chunked_upload_service,
    )
    register_v2_routers(app, v2_registry)
    app.add_middleware(VersionHeaderMiddleware)
    app.add_middleware(DeprecationMiddleware, registry=v2_registry)

    # Dashboard: Task Manager UI (self-contained HTML)
    from pathlib import Path

    from fastapi.responses import HTMLResponse

    @app.get("/dashboard/tasks", response_class=HTMLResponse, include_in_schema=False)
    async def task_manager_dashboard() -> HTMLResponse:
        html_path = Path(__file__).parent / "static" / "task_manager.html"
        return HTMLResponse(html_path.read_text())

    # Request correlation middleware (Issue #1002).
    # MUST be the last add_middleware call — Starlette applies in reverse order,
    # so last-added = outermost = first to execute on each request.
    # This ensures ALL other middlewares run with correlation context.
    from nexus.server.middleware.correlation import CorrelationMiddleware

    app.add_middleware(CorrelationMiddleware)  # type: ignore[arg-type]

    # Exchange Protocol error handler (Issue #1361)
    try:
        from nexus.server.api.v2.error_handler import register_exchange_error_handler

        register_exchange_error_handler(app)
        logger.info("Exchange protocol error handler registered")
    except ImportError as e:
        logger.warning(f"Failed to register Exchange error handler: {e}.")

    # Payment endpoints (Issue #3250: TUI Payments panel)
    try:
        from nexus.server.api.v2.routers.pay import audit_router as pay_audit_router
        from nexus.server.api.v2.routers.pay import router as pay_router

        app.include_router(pay_router)
        app.include_router(pay_audit_router)
        logger.info("Payment endpoints registered (/api/v2/pay/*, /api/v2/audit/*)")
    except ImportError as e:
        logger.debug(f"Pay router unavailable: {e}")

    # Catalog, Aspects, Lineage, Graph endpoints (Issue #3250: TUI Search/Knowledge)
    try:
        from nexus.server.api.v2.routers.catalog import router as catalog_router

        app.include_router(catalog_router)
        logger.info("Catalog endpoints registered (/api/v2/catalog/*)")
    except ImportError as e:
        logger.debug(f"Catalog router unavailable: {e}")

    try:
        from nexus.server.api.v2.routers.aspects import router as aspects_router

        app.include_router(aspects_router)
        logger.info("Aspects endpoints registered (/api/v2/aspects/*)")
    except ImportError as e:
        logger.debug(f"Aspects router unavailable: {e}")

    try:
        from nexus.server.api.v2.routers.lineage import router as lineage_router

        app.include_router(lineage_router)
        logger.info("Lineage endpoints registered (/api/v2/lineage/*)")
    except ImportError as e:
        logger.debug(f"Lineage router unavailable: {e}")

    try:
        from nexus.server.api.v2.routers.graph import router as graph_router

        app.include_router(graph_router)
        logger.info("Graph endpoints registered (/api/v2/graph/*)")
    except ImportError as e:
        logger.debug(f"Graph router unavailable: {e}")

    # Locks endpoints (Issue #3250: TUI Locks tab)
    try:
        from nexus.server.api.v2.routers.locks import router as locks_router

        app.include_router(locks_router)
        logger.info("Locks endpoints registered (/api/v2/locks/*)")
    except ImportError as e:
        logger.debug(f"Locks router unavailable: {e}")

    # IPC Brick endpoints (Issue #1727, LEGO §8)
    try:
        from nexus.server.api.v2.routers.ipc import router as ipc_router

        app.include_router(ipc_router)
        logger.info("IPC endpoints registered (/api/v2/ipc/*)")
    except ImportError as e:
        logger.debug(f"IPC router unavailable: {e}")

    # Secrets audit log endpoints (Issue #997)
    try:
        from nexus.server.api.v2.routers.secrets_audit import (
            get_secrets_audit_logger as _secrets_audit_dep,
        )
        from nexus.server.api.v2.routers.secrets_audit import (
            router as secrets_audit_router,
        )
        from nexus.storage.secrets_audit_logger import SecretsAuditLogger

        _secrets_audit_logger_instance: SecretsAuditLogger | None = None

        def _get_secrets_audit_logger_override(
            auth_result: dict[str, Any] = Depends(require_auth),
        ) -> tuple:
            nonlocal _secrets_audit_logger_instance
            if not auth_result.get("is_admin", False):
                raise HTTPException(
                    status_code=403,
                    detail="Secrets audit log access requires admin privileges",
                )
            if _secrets_audit_logger_instance is None:
                _sa_rs = getattr(app.state, "record_store", None)
                if _sa_rs is None:
                    raise HTTPException(status_code=500, detail="Secrets audit not configured")
                _secrets_audit_logger_instance = SecretsAuditLogger(record_store=_sa_rs)
            zone_id = auth_result.get("zone_id", ROOT_ZONE_ID)
            return _secrets_audit_logger_instance, zone_id

        app.dependency_overrides[_secrets_audit_dep] = _get_secrets_audit_logger_override
        app.include_router(secrets_audit_router)
        logger.info("Secrets audit routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import secrets audit router: {e}")

    # Secrets store endpoints (general-purpose secret storage with versioning)
    try:
        from nexus.bricks.auth.oauth.crypto import OAuthCrypto
        from nexus.bricks.secrets.service import SecretsService
        from nexus.server.api.v2.routers.secrets import (
            get_secrets_service as _secrets_service_dep,
        )
        from nexus.server.api.v2.routers.secrets import (
            router as secrets_router,
        )
        from nexus.storage.secrets_audit_logger import SecretsAuditLogger

        _secrets_service_instance: SecretsService | None = None

        def _get_secrets_service_override() -> SecretsService:
            nonlocal _secrets_service_instance
            if _secrets_service_instance is None:
                _sa_rs = getattr(app.state, "record_store", None)
                if _sa_rs is None:
                    raise HTTPException(status_code=500, detail="Secrets service not configured")

                # Build a settings_store from metastore so the encryption key
                # is persisted across restarts instead of being randomly generated.
                # Use Python RaftMetadataStore directly (not RustMetastoreProxy) to
                # avoid Rust/Python redb coherency issues with cfg: entries.
                _settings_store = None
                try:
                    from pathlib import Path

                    from nexus.storage.auth_stores.metastore_settings_store import (
                        MetastoreSettingsStore,
                    )
                    from nexus.storage.raft_metadata_store import RaftMetadataStore

                    _metadata_path = str(Path.home() / ".nexus" / "metastore")
                    _py_metastore = RaftMetadataStore.embedded(_metadata_path)
                    _settings_store = MetastoreSettingsStore(_py_metastore)
                except Exception:
                    logger.warning(
                        "MetastoreSettingsStore unavailable; using ephemeral OAuth key",
                        exc_info=True,
                    )

                _oauth_crypto = OAuthCrypto(settings_store=_settings_store)
                _audit_logger = SecretsAuditLogger(record_store=_sa_rs)
                _secrets_service_instance = SecretsService(
                    record_store=_sa_rs,
                    oauth_crypto=_oauth_crypto,
                    audit_logger=_audit_logger,
                )
            return _secrets_service_instance

        app.dependency_overrides[_secrets_service_dep] = _get_secrets_service_override
        app.include_router(secrets_router)
        logger.info("Secrets store routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import secrets router: {e}")

    # ---- /v1 (nexus-bot daemon, #3804) ----
    # Token-exchange stub — always registered; flag controls behavior
    # (route currently always returns 501, so there's no gating risk).
    try:
        from nexus.server.api.v1.routers.token_exchange import make_token_exchange_router

        _token_exchange_enabled = os.environ.get("NEXUS_TOKEN_EXCHANGE_ENABLED", "").lower() in (
            "1",
            "true",
            "yes",
        )
        app.include_router(make_token_exchange_router(enabled=_token_exchange_enabled))
        logger.info("v1 token-exchange route registered (enabled=%s)", _token_exchange_enabled)
    except ImportError as e:
        logger.warning(f"Failed to import v1 token-exchange router: {e}")

    # Daemon enroll/refresh + auth-profiles routers — gated on JWT signing key
    # + enroll-token secret. If either env var is missing, skip with a warning;
    # deployments that don't use the nexus-bot daemon are unaffected.
    _jwt_signing_key = os.environ.get("NEXUS_JWT_SIGNING_KEY")
    _enroll_token_secret = os.environ.get("NEXUS_ENROLL_TOKEN_SECRET", "")
    if _jwt_signing_key and _enroll_token_secret:
        _database_url = getattr(app.state, "database_url", None)
        if not _database_url:
            logger.warning("v1 daemon routes disabled: database_url unavailable on app.state")
        else:
            try:
                from sqlalchemy import create_engine

                from nexus.server.api.v1.jwt_signer import JwtSigner
                from nexus.server.api.v1.routers.auth_profiles import (
                    make_auth_profiles_router,
                )
                from nexus.server.api.v1.routers.daemon import make_daemon_router
                from nexus.server.api.v1.routers.jwks import make_jwks_router

                _v1_engine = create_engine(_database_url, future=True)
                _v1_signer = JwtSigner.from_path(
                    _jwt_signing_key,
                    issuer=os.environ.get("NEXUS_JWT_ISSUER", "https://nexus.local"),
                )
                app.include_router(
                    make_daemon_router(
                        engine=_v1_engine,
                        signer=_v1_signer,
                        enroll_secret=_enroll_token_secret.encode(),
                    )
                )
                app.include_router(make_auth_profiles_router(engine=_v1_engine, signer=_v1_signer))
                app.include_router(make_jwks_router(signer=_v1_signer))
                logger.info("v1 daemon + auth-profiles + jwks routes registered")

                # Dev-loop convenience: mint tenant/principal/enroll-token in
                # one call. Requires admin-bypass explicitly on AND a
                # non-empty NEXUS_ADMIN_BOOTSTRAP_TOKEN so a spoofable header
                # alone cannot mint credentials. Production deployments
                # (bypass=false or token unset) never expose this endpoint.
                _admin_bootstrap_token = os.environ.get("NEXUS_ADMIN_BOOTSTRAP_TOKEN", "")
                if (
                    os.environ.get("NEXUS_ALLOW_ADMIN_BYPASS", "").lower() in ("1", "true", "yes")
                    and _admin_bootstrap_token
                ):
                    from nexus.server.api.v1.routers.admin_bootstrap import (
                        make_admin_bootstrap_router,
                    )

                    app.include_router(
                        make_admin_bootstrap_router(
                            engine=_v1_engine,
                            enroll_secret=_enroll_token_secret.encode(),
                            admin_user=os.environ.get("NEXUS_ADMIN_USER", "admin"),
                            bootstrap_token=_admin_bootstrap_token.encode(),
                        )
                    )
                    logger.info("v1 admin daemon-bootstrap route registered (dev-only)")
                elif os.environ.get("NEXUS_ALLOW_ADMIN_BYPASS", "").lower() in (
                    "1",
                    "true",
                    "yes",
                ):
                    logger.warning(
                        "v1 admin daemon-bootstrap route NOT registered: "
                        "NEXUS_ALLOW_ADMIN_BYPASS=true but "
                        "NEXUS_ADMIN_BOOTSTRAP_TOKEN is unset — set it to a "
                        "random 32+ byte secret to enable dev bootstrap."
                    )
            except ImportError as e:
                logger.warning(f"Failed to import v1 daemon routers: {e}")
    else:
        logger.warning(
            "v1 daemon routes disabled: NEXUS_JWT_SIGNING_KEY and/or "
            "NEXUS_ENROLL_TOKEN_SECRET unset"
        )

    # Asyncio debug endpoint (Python 3.14+) — gated behind env flag + admin auth (Issue #1596)
    if os.environ.get("NEXUS_DEBUG_ENABLED", "").lower() in ("1", "true", "yes"):
        from nexus.server.dependencies import require_admin as _require_admin_dep

        @app.get("/debug/asyncio", tags=["debug"], dependencies=[Depends(_require_admin_dep)])
        async def debug_asyncio() -> dict[str, Any]:
            """Debug endpoint for asyncio task introspection.

            Requires NEXUS_DEBUG_ENABLED=true and admin privileges.

            Returns information about running async tasks, including:
            - Total task count
            - Current task info
            - Call graph (Python 3.14+ only)
            """
            result: dict[str, Any] = {
                "python_version": f"{__import__('sys').version_info.major}.{__import__('sys').version_info.minor}",
            }

            # Get all running tasks
            try:
                all_tasks = asyncio.all_tasks()
                current = asyncio.current_task()
                result["task_count"] = len(all_tasks)
                result["current_task"] = current.get_name() if current else None
                result["tasks"] = [
                    {
                        "name": task.get_name(),
                        "done": task.done(),
                        "cancelled": task.cancelled(),
                    }
                    for task in list(all_tasks)[:50]  # Limit to 50 tasks
                ]
            except Exception as e:
                result["tasks_error"] = str(e)

            # Python 3.14+ call graph introspection
            try:
                from asyncio import format_call_graph  # type: ignore[attr-defined]

                # Format call graph for current task (no args needed)
                result["call_graph_available"] = True
                result["call_graph"] = format_call_graph()
            except ImportError:
                result["call_graph_available"] = False
                result["call_graph_note"] = "Requires Python 3.14+"
            except Exception as e:
                result["call_graph_error"] = str(e)

            return result


# ============================================================================
# Server Runner
# ============================================================================


def run_server(
    app: FastAPI | str,
    host: str = "0.0.0.0",
    port: int = 2026,
    log_level: str = "info",
    workers: int | None = None,
) -> None:
    """Run the FastAPI server with uvicorn.

    Args:
        app: FastAPI application instance or import string (e.g., "nexus.server:app")
        host: Host to bind to
        port: Port to bind to
        log_level: Logging level
        workers: Number of worker processes (default: 1, or NEXUS_WORKERS env var)
            - For multi-worker mode, pass app as string import path
            - Set to 0 or None for single worker (recommended for development)
            - Set to CPU count for production (e.g., 4 for 4-core machine)

    Production deployment for multi-worker:
        # Option 1: Use uvicorn CLI with workers
        uvicorn nexus.server.fastapi_server:app --host 0.0.0.0 --port 2026 --workers 4

        # Option 2: Use gunicorn with uvicorn workers (recommended)
        gunicorn nexus.server.fastapi_server:app -w 4 -k uvicorn.workers.UvicornWorker

    Environment variables:
        NEXUS_WORKERS: Number of workers (default: 1)
        NEXUS_HOST: Host to bind (default: 0.0.0.0)
        NEXUS_PORT: Port to bind (default: 2026)
    """
    import os

    import uvicorn

    from nexus.core import setup_uvloop

    # Install uvloop for better async performance (2-4x faster)
    # This must be called before uvicorn creates its event loop
    if setup_uvloop():
        logger.info("uvloop installed as default event loop policy")

    # Get workers from parameter or environment variable
    if workers is None:
        workers = int(os.environ.get("NEXUS_WORKERS", "1"))

    # Multi-worker mode requires app to be a string import path
    if workers > 1 and not isinstance(app, str):
        logger.warning(
            f"Multi-worker mode (workers={workers}) requires app to be a string import path. "
            "Falling back to single worker. For production, use: "
            "uvicorn nexus.server.fastapi_server:app --workers N"
        )
        workers = 1

    logger.info(f"Starting Nexus server on {host}:{port} with {workers} worker(s)")

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        workers=workers if workers > 1 else None,
    )


def run_server_from_config(
    nexus_fs: "NexusFS",
    host: str = "0.0.0.0",
    port: int = 2026,
    api_key: str | None = None,
    auth_provider: Any = None,
    database_url: str | None = None,
    log_level: str = "info",
) -> None:
    """Create and run server from configuration.

    Args:
        nexus_fs: NexusFS instance
        host: Host to bind to
        port: Port to bind to
        api_key: Static API key
        auth_provider: Auth provider
        database_url: Database URL for async operations
        log_level: Logging level
    """
    app = create_app(
        nexus_fs=nexus_fs,
        api_key=api_key,
        auth_provider=auth_provider,
        database_url=database_url,
    )
    run_server(app, host=host, port=port, log_level=log_level)
