"""FastAPI server for Nexus filesystem.

This module implements an async HTTP server using FastAPI that exposes all
NexusFileSystem operations through a JSON-RPC API. This provides significantly
better performance under concurrent load compared to the ThreadingHTTPServer.

Performance improvements:
- Async database operations (asyncpg/aiosqlite)
- Connection pooling
- Non-blocking I/O
- 10-50x throughput improvement under concurrent load

The server maintains the same API contract as rpc_server.py:
- POST /api/nfs/{method} - JSON-RPC endpoints
- GET /health - Health check
- GET /api/auth/whoami - Authentication info

Example:
    from nexus.server.fastapi_server import create_app, run_server

    app = create_app(nexus_fs, database_url="postgresql://...")
    run_server(app, host="0.0.0.0", port=2026)
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.gzip import GZipMiddleware

from nexus.constants import DEFAULT_GOOGLE_REDIRECT_URI, DEFAULT_NEXUS_URL

# --- Extracted modules (re-exported for backward compatibility) ---
from nexus.server.dependencies import (  # noqa: F401, E402
    get_auth_result,
    get_operation_context,
    require_auth,
)
from nexus.server.error_handlers import (  # noqa: E402
    nexus_error_handler as _nexus_error_handler,
)
from nexus.server.path_utils import (  # noqa: F401
    unscope_internal_dict,
    unscope_internal_path,
    unscope_result,
)
from nexus.server.rate_limiting import (  # noqa: F401, E402
    RATE_LIMIT_ANONYMOUS,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_PREMIUM,
    _get_rate_limit_key,
    _rate_limit_exceeded_handler,
)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models for Request/Response
# ============================================================================


class HealthResponse(BaseModel):
    """Health check response."""

    status: str
    service: str
    enforce_permissions: bool | None = None
    enforce_zone_isolation: bool | None = None
    has_auth: bool | None = None


class WhoamiResponse(BaseModel):
    """Authentication info response."""

    authenticated: bool
    subject_type: str | None = None
    subject_id: str | None = None
    zone_id: str | None = None
    is_admin: bool = False
    inherit_permissions: bool = True  # v0.5.1: Whether agent inherits owner's permissions
    user: str | None = None


# ============================================================================
# Lock API Models — extracted to api/v1/models/locks.py (Issue #1288)
# Re-exported for backward compatibility with tests/consumers.
# ============================================================================
from nexus.server.api.v1.models.locks import LOCK_MAX_TTL as LOCK_MAX_TTL  # noqa: E402
from nexus.server.api.v1.models.locks import LockAcquireRequest as LockAcquireRequest  # noqa: E402
from nexus.server.api.v1.models.locks import LockExtendRequest as LockExtendRequest  # noqa: E402
from nexus.server.api.v1.models.locks import LockHolderResponse as LockHolderResponse  # noqa: E402
from nexus.server.api.v1.models.locks import LockInfoMutex as LockInfoMutex  # noqa: E402
from nexus.server.api.v1.models.locks import LockInfoSemaphore as LockInfoSemaphore  # noqa: E402
from nexus.server.api.v1.models.locks import LockListResponse as LockListResponse  # noqa: E402
from nexus.server.api.v1.models.locks import LockResponse as LockResponse  # noqa: E402
from nexus.server.api.v1.models.locks import LockStatusResponse as LockStatusResponse  # noqa: E402

# Rate limiting and error handlers are now in rate_limiting.py and error_handlers.py.
# The `limiter` global, rate limit constants, and handler functions are imported above.
from nexus.server.rate_limiting import limiter  # noqa: F401, F811, E402

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


# Stream token signing/verification is now in streaming.py.
# Auth dependencies (get_auth_result, require_auth, get_operation_context) are in dependencies.py.
# All are imported above for backward compatibility.


# ============================================================================
# Lifespan Management (extracted to lifespan/ package, Issue #1602)
# ============================================================================
from nexus.server.lifespan import lifespan  # noqa: E402

# ============================================================================
# Application Factory
# ============================================================================


def create_app(
    nexus_fs: NexusFS,
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
        data_dir: Server data directory for persistent storage (A2A tasks, etc.)

    Returns:
        Configured FastAPI application
    """
    global _fastapi_app

    # Create app first so we can store state on it
    app = FastAPI(
        title="Nexus RPC Server",
        description="AI-Native Distributed Filesystem API",
        version="1.0.0",
        lifespan=lifespan,
    )

    # Set module-level reference for kernel code access
    _fastapi_app = app

    # Store application state on app.state (replaces AppState class)
    app.state.nexus_fs = nexus_fs
    app.state.api_key = api_key
    app.state.auth_provider = auth_provider
    app.state.database_url = database_url
    app.state.data_dir = data_dir  # Issue #1412: A2A task persistence

    # Expose async_session_factory from RecordStoreABC (if available).
    # This is the canonical way for async endpoints to get database sessions
    # without bypassing the RecordStore abstraction with raw URLs.
    _record_store = getattr(nexus_fs, "_record_store", None)
    if _record_store is not None:
        try:
            app.state.async_session_factory = _record_store.async_session_factory
        except NotImplementedError:
            app.state.async_session_factory = None
    else:
        app.state.async_session_factory = None

    # Expose sync session_factory from RecordStoreABC (Issue #1519).
    # This is the canonical way for sync endpoints to get database sessions
    # without reaching into NexusFS.SessionLocal internals.
    if _record_store is not None:
        app.state.session_factory = _record_store.session_factory
    elif hasattr(nexus_fs, "SessionLocal") and nexus_fs.SessionLocal is not None:
        app.state.session_factory = nexus_fs.SessionLocal
    else:
        app.state.session_factory = None

    # Thread pool and timeout settings (Issue #932)
    app.state.thread_pool_size = thread_pool_size or int(
        os.environ.get("NEXUS_THREAD_POOL_SIZE", "200")
    )
    app.state.operation_timeout = operation_timeout or float(
        os.environ.get("NEXUS_OPERATION_TIMEOUT", "30.0")
    )

    # Discover exposed methods
    app.state.exposed_methods = _discover_exposed_methods(nexus_fs)

    # Initialize defaults for optional services (set during lifespan)
    app.state.async_nexus_fs = None
    app.state.async_rebac_manager = None
    app.state.subscription_manager = None
    app.state.search_daemon = None
    app.state.search_daemon_enabled = False
    app.state.directory_grant_expander = None
    app.state.cache_factory = None
    app.state.websocket_manager = None
    app.state.reactive_subscription_manager = None
    app.state.agent_registry = None
    app.state.async_agent_registry = None
    app.state.agent_event_log = None
    app.state.sandbox_auth_service = None
    app.state.write_back_service = None
    app.state.task_runner = None
    app.state.event_log = None
    app.state.key_service = None
    app.state.rebac_circuit_breaker = None
    app.state.chunked_upload_service = None

    # Initialize subscription manager if we have a metadata store
    try:
        if hasattr(nexus_fs, "SessionLocal"):
            from nexus.server.subscriptions import (
                SubscriptionManager,
                set_subscription_manager,
            )

            app.state.subscription_manager = SubscriptionManager(nexus_fs.SessionLocal)
            nexus_fs.subscription_manager = app.state.subscription_manager
            set_subscription_manager(app.state.subscription_manager)
            logger.info("Subscription manager initialized and injected into NexusFS")
    except Exception as e:
        logger.warning(f"Failed to initialize subscription manager: {e}")

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add Gzip compression middleware (60-80% response size reduction)
    # Only compress responses > 1000 bytes, compression level 6 (good balance)
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)

    # Initialize rate limiter (Issue #780)
    # Rate limiting is DISABLED by default for better performance
    # Set NEXUS_RATE_LIMIT_ENABLED=true to enable rate limiting
    import nexus.server.rate_limiting as _rate_limiting_mod

    global limiter
    rate_limit_enabled = os.environ.get("NEXUS_RATE_LIMIT_ENABLED", "").lower() in (
        "true",
        "1",
        "yes",
    )
    redis_url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")

    limiter = Limiter(
        key_func=_get_rate_limit_key,
        default_limits=[RATE_LIMIT_AUTHENTICATED] if rate_limit_enabled else [],
        storage_uri=redis_url,
        strategy="fixed-window",
        enabled=rate_limit_enabled,
    )
    # Keep the canonical module in sync so any code importing from rate_limiting gets
    # the initialized Limiter instance, not the bare type annotation.
    _rate_limiting_mod.limiter = limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Register Nexus exception handlers for error classification
    from nexus.core.exceptions import NexusError

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
        logger.info(
            "Rate limiting is DISABLED (default, set NEXUS_RATE_LIMIT_ENABLED=true to enable)"
        )

    # Initialize authentication provider for user registration/login endpoints
    if auth_provider is not None:
        try:
            from nexus.server.auth.auth_routes import set_auth_provider

            # Extract DatabaseLocalAuth from DiscriminatingAuthProvider if needed
            from nexus.server.auth.base import AuthProvider
            from nexus.server.auth.database_local import DatabaseLocalAuth
            from nexus.server.auth.factory import DiscriminatingAuthProvider

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

    # Register extracted v1 domain routers (#1288)
    try:
        from nexus.server.api.v1.versioning import build_v1_registry, register_v1_routers

        v1_registry = build_v1_registry()
        register_v1_routers(app, v1_registry)
    except Exception as e:
        logger.warning("Failed to register v1 routers: %s", e)

    # Register NexusFS instance for zone routes, migration, and user provisioning.
    # This must happen unconditionally (not only when OAuth is configured).
    try:
        from nexus.server.auth.auth_routes import set_nexus_instance

        set_nexus_instance(nexus_fs)
        logger.info("NexusFS instance registered for zone management")
    except Exception as e:
        logger.warning(f"Failed to register NexusFS instance: {e}")

    # Initialize OAuth provider if credentials are available
    _initialize_oauth_provider(nexus_fs, auth_provider, database_url)

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

        obs_sub = getattr(nexus_fs, "_service_extras", {}).get("observability_subsystem")
        if obs_sub is not None:
            REGISTRY.register(QueryObserverCollector(obs_sub.observer))
    except Exception:
        pass

    # Register WriteBuffer → Prometheus collector bridge (Issue #1370)
    try:
        from prometheus_client import REGISTRY

        from nexus.server.wb_metrics_collector import WriteBufferCollector

        _wo = nexus_fs._write_observer
        if _wo is not None and hasattr(_wo, "metrics"):
            REGISTRY.register(WriteBufferCollector(_wo))
    except Exception as e:
        logger.debug("WriteBuffer metrics collector not registered: %s", e)

    # Instrument FastAPI with OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import instrument_fastapi_app

        instrument_fastapi_app(app)
    except ImportError:
        pass

    return app


def _initialize_oauth_provider(
    nexus_fs: NexusFS, auth_provider: Any, database_url: str | None
) -> None:
    """Initialize OAuth provider if Google OAuth credentials are available.

    Args:
        nexus_fs: NexusFS instance
        auth_provider: Authentication provider (for session factory)
        database_url: Database URL
    """
    try:
        google_client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv(
            "NEXUS_OAUTH_GOOGLE_CLIENT_ID"
        )
        google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv(
            "NEXUS_OAUTH_GOOGLE_CLIENT_SECRET"
        )
        google_redirect_uri = os.getenv("GOOGLE_REDIRECT_URI", DEFAULT_GOOGLE_REDIRECT_URI)
        jwt_secret = os.getenv("NEXUS_JWT_SECRET")

        if not google_client_id or not google_client_secret:
            logger.debug(
                "Google OAuth credentials not found. OAuth endpoints will return 500 errors."
            )
            return

        # Get session factory from auth_provider or nexus_fs
        session_factory = None
        if auth_provider and hasattr(auth_provider, "session_factory"):
            session_factory = auth_provider.session_factory
        elif hasattr(nexus_fs, "SessionLocal") and nexus_fs.SessionLocal is not None:
            session_factory = nexus_fs.SessionLocal
        else:
            logger.warning("Cannot initialize OAuth provider: no session factory available")
            return

        if not session_factory:
            logger.warning("Cannot initialize OAuth provider: session factory is None")
            return

        # Initialize OAuth provider
        from nexus.server.auth.auth_routes import set_oauth_provider
        from nexus.server.auth.oauth_crypto import OAuthCrypto
        from nexus.server.auth.oauth_user_auth import OAuthUserAuth

        oauth_crypto = OAuthCrypto(db_url=database_url)
        oauth_provider = OAuthUserAuth(
            session_factory=session_factory,
            google_client_id=google_client_id,
            google_client_secret=google_client_secret,
            google_redirect_uri=google_redirect_uri,
            jwt_secret=jwt_secret,
            oauth_crypto=oauth_crypto,
        )

        set_oauth_provider(oauth_provider)
        logger.info("Google OAuth provider initialized successfully")
    except Exception as e:
        logger.warning(
            f"Failed to initialize OAuth provider: {e}. OAuth endpoints will not be available."
        )

    # NexusFS instance is now registered unconditionally in create_app()
    # (moved from here to avoid being gated on OAuth credentials)


def _discover_exposed_methods(nexus_fs: NexusFS) -> dict[str, Any]:
    """Discover all methods marked with @rpc_expose decorator."""
    exposed = {}

    for name in dir(nexus_fs):
        if name.startswith("_"):
            continue

        try:
            attr = getattr(nexus_fs, name)
            if callable(attr) and hasattr(attr, "_rpc_exposed"):
                method_name = getattr(attr, "_rpc_name", name)
                exposed[method_name] = attr
                logger.debug(f"Discovered RPC method: {method_name}")
        except Exception:
            continue

    logger.info(f"Auto-discovered {len(exposed)} RPC methods")
    return exposed


def _register_routes(app: FastAPI) -> None:
    """Register all routes.

    Core endpoints (health, debug, streaming, RPC) are extracted to
    ``api/core/`` routers (#1602).  Domain routers (auth, zone, v2, A2A,
    secrets audit) are registered here.
    """

    # ---- Core API routers (extracted from inline endpoints, #1602) ----
    from nexus.server.api.core.debug import router as debug_router
    from nexus.server.api.core.health import router as health_router
    from nexus.server.api.core.rpc import router as rpc_router
    from nexus.server.api.core.streaming import router as streaming_router

    app.include_router(health_router)
    app.include_router(debug_router)
    app.include_router(streaming_router)
    app.include_router(rpc_router)

    # ---- Domain routers ----

    # Authentication routes
    try:
        from nexus.server.auth.auth_routes import router as auth_router

        app.include_router(auth_router)
        logger.info("Authentication routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import auth routes: {e}. OAuth endpoints will not be available.")

    # Zone management routes
    try:
        from nexus.server.auth.zone_routes import router as zone_router

        app.include_router(zone_router)
        logger.info("Zone management routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import zone routes: {e}. Zone management unavailable.")

    # API v2 routes — centralized registration via versioning module (#995)
    from nexus.server.api.v2.versioning import (
        DeprecationMiddleware,
        VersionHeaderMiddleware,
        build_v2_registry,
        register_v2_routers,
    )

    v2_registry = build_v2_registry(
        async_nexus_fs_getter=lambda: _fastapi_app.state.async_nexus_fs,
        chunked_upload_service_getter=lambda: _fastapi_app.state.chunked_upload_service,
    )
    register_v2_routers(app, v2_registry)
    app.add_middleware(VersionHeaderMiddleware)
    app.add_middleware(DeprecationMiddleware, registry=v2_registry)

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

    # A2A Protocol Endpoint (Issue #1256, brick-extracted #1401)
    try:
        from nexus.a2a import create_a2a_router

        a2a_base_url = os.environ.get("NEXUS_A2A_BASE_URL", DEFAULT_NEXUS_URL)
        a2a_auth_required = bool(
            getattr(_fastapi_app.state, "api_key", None)
            or getattr(_fastapi_app.state, "auth_provider", None)
        )

        async def _a2a_auth_adapter(request: Request) -> dict[str, Any] | None:
            try:
                return await get_auth_result(
                    request=request,
                    authorization=request.headers.get("Authorization"),
                    x_agent_id=request.headers.get("X-Agent-ID"),
                    x_nexus_subject=request.headers.get("X-Nexus-Subject"),
                    x_nexus_zone_id=request.headers.get("X-Nexus-Zone-ID"),
                )
            except Exception:
                return None

        a2a_router = create_a2a_router(
            nexus_fs=_fastapi_app.state.nexus_fs,
            config=None,
            base_url=a2a_base_url,
            auth_required=a2a_auth_required,
            auth_fn=_a2a_auth_adapter,
            data_dir=getattr(_fastapi_app.state, "data_dir", None),
        )
        app.include_router(a2a_router)
        logger.info("A2A protocol endpoint registered (/.well-known/agent.json + /a2a)")
    except ImportError as e:
        logger.warning(f"Failed to import A2A router: {e}. A2A endpoint will not be available.")

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
                session_factory = getattr(_fastapi_app.state.nexus_fs, "SessionLocal", None)
                if session_factory is None:
                    raise HTTPException(status_code=500, detail="Secrets audit not configured")
                engine = session_factory.kw.get("bind") if hasattr(session_factory, "kw") else None
                if engine is not None:
                    from nexus.storage.models.secrets_audit_log import SecretsAuditLogModel

                    SecretsAuditLogModel.__table__.create(engine, checkfirst=True)
                _secrets_audit_logger_instance = SecretsAuditLogger(session_factory=session_factory)
            zone_id = auth_result.get("zone_id", "default")
            return _secrets_audit_logger_instance, zone_id

        app.dependency_overrides[_secrets_audit_dep] = _get_secrets_audit_logger_override
        app.include_router(secrets_audit_router)
        logger.info("Secrets audit routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import secrets audit router: {e}")


# ============================================================================
# RPC Dispatch (extracted to rpc/ package, Issue #1602)
# ============================================================================
# Handler functions and dispatch infrastructure have been extracted to:
#   - nexus.server.rpc.dispatch      (dispatch table, dispatch_method, fire_rpc_event)
#   - nexus.server.rpc.handlers.filesystem  (16 filesystem handlers)
#   - nexus.server.rpc.handlers.memory      (10 memory handlers)
#   - nexus.server.rpc.handlers.delta       (2 delta sync handlers)
#   - nexus.server.rpc.handlers.admin       (5 admin key handlers)
#
# The dispatch function is imported and used by rpc_endpoint() above.
from nexus.server.rpc.dispatch import dispatch_method as _dispatch_method  # noqa: F401, E402, I001
from nexus.server.rpc.handlers.admin import require_admin as _require_admin  # noqa: F401, E402, I001


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
    nexus_fs: NexusFS,
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
