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
import dataclasses
import hashlib
import logging
import os
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from anyio import to_thread
from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from starlette.middleware.gzip import GZipMiddleware

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)

# --- Extracted modules (re-exported for backward compatibility) ---
from nexus.server.dependencies import (  # noqa: E402
    get_auth_result,
    get_operation_context,
    require_auth,
)
from nexus.server.error_handlers import (  # noqa: E402
    nexus_error_handler as _nexus_error_handler,
)
from nexus.server.path_utils import (
    unscope_internal_dict,
    unscope_internal_path,
    unscope_result,
)
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)
from nexus.server.rate_limiting import (  # noqa: E402
    RATE_LIMIT_ANONYMOUS,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_PREMIUM,
    _get_rate_limit_key,
    _rate_limit_exceeded_handler,
)
from nexus.server.streaming import (  # noqa: E402
    _sign_stream_token,
    _verify_stream_token,
)

if TYPE_CHECKING:
    from nexus.core.nexus_fs import NexusFS

logger = logging.getLogger(__name__)


# ============================================================================
# Pydantic Models for Request/Response
# ============================================================================


class RPCRequestModel(BaseModel):
    """JSON-RPC 2.0 request model."""

    jsonrpc: str = "2.0"
    method: str | None = None
    params: dict[str, Any] | None = None
    id: str | int | None = None


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
from nexus.server.rate_limiting import limiter  # noqa: F811, E402 — re-import for module-level use

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
# Lifespan Management
# ============================================================================


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    """Application lifespan manager.

    Handles startup and shutdown of async resources.
    """
    # Configure structured logging (Issue #1002).
    # This is the single canonical call site — reads NEXUS_ENV env var.
    try:
        from nexus.server.logging_config import configure_logging

        env = os.environ.get("NEXUS_ENV", "dev")
        configure_logging(env=env)
    except ImportError:
        pass  # structlog not installed — fall back to stdlib

    logger.info("Starting FastAPI Nexus server...")

    # Initialize Sentry error tracking (Issue #759)
    try:
        from nexus.server.sentry import setup_sentry

        setup_sentry()
    except ImportError:
        logger.debug("Sentry not available")

    # Initialize OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import setup_telemetry

        setup_telemetry()
    except ImportError:
        logger.debug("OpenTelemetry not available")

    # Initialize Prometheus metrics (Issue #761)
    try:
        from nexus.server.metrics import setup_prometheus

        setup_prometheus()
    except ImportError:
        logger.debug("prometheus_client not available")

    # Configure thread pool size (Issue #932)
    # Increase from default 40 to prevent thread pool exhaustion under load
    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = _app.state.thread_pool_size
    logger.info(f"Thread pool size set to {limiter.total_tokens}")

    # Initialize async ReBAC manager if database URL provided
    if _app.state.database_url:
        try:
            from nexus.services.permissions.async_rebac_manager import (
                AsyncReBACManager,
                create_async_engine_from_url,
            )

            engine = create_async_engine_from_url(_app.state.database_url)
            _app.state.async_rebac_manager = AsyncReBACManager(engine)
            logger.info("Async ReBAC manager initialized")

            # Issue #940: Initialize AsyncNexusFS with permission enforcement
            try:
                from nexus.core.async_nexus_fs import AsyncNexusFS
                from nexus.services.permissions.async_permissions import AsyncPermissionEnforcer

                backend_root = os.getenv("NEXUS_BACKEND_ROOT", ".nexus-data/backend")
                tenant_id = os.getenv("NEXUS_TENANT_ID", "default")
                enforce_permissions = os.getenv("NEXUS_ENFORCE_PERMISSIONS", "true").lower() in (
                    "true",
                    "1",
                    "yes",
                )

                # Issue #1239: Create namespace manager for per-subject visibility
                # Issue #1265: Factory function handles L3 persistent store wiring
                namespace_manager = None
                if enforce_permissions and hasattr(_app.state, "nexus_fs"):
                    sync_rebac = getattr(_app.state.nexus_fs, "_rebac_manager", None)
                    if sync_rebac:
                        from nexus.services.permissions.namespace_factory import (
                            create_namespace_manager,
                        )

                        ns_record_store = getattr(_app.state.nexus_fs, "_record_store", None)
                        namespace_manager = create_namespace_manager(
                            rebac_manager=sync_rebac,
                            record_store=ns_record_store,
                        )
                        logger.info(
                            "[NAMESPACE] NamespaceManager initialized for AsyncPermissionEnforcer "
                            "(using sync rebac_manager, L3=%s)",
                            "enabled" if ns_record_store else "disabled",
                        )

                # Create permission enforcer with async ReBAC
                # Note: agent_registry may not be initialized yet (it's set later
                # in the lifespan), so use getattr with None default.
                permission_enforcer = AsyncPermissionEnforcer(
                    rebac_manager=_app.state.async_rebac_manager,
                    namespace_manager=namespace_manager,
                    agent_registry=getattr(_app.state, "agent_registry", None),
                )

                # Create AsyncNexusFS using the same RaftMetadataStore as sync NexusFS
                _app.state.async_nexus_fs = AsyncNexusFS(
                    backend_root=backend_root,
                    metadata_store=_app.state.nexus_fs.metadata,
                    tenant_id=tenant_id,
                    enforce_permissions=enforce_permissions,
                    permission_enforcer=permission_enforcer,
                )
                await _app.state.async_nexus_fs.initialize()
                logger.info(
                    f"AsyncNexusFS initialized (backend={backend_root}, "
                    f"tenant={tenant_id}, enforce_permissions={enforce_permissions})"
                )
            except Exception as e:
                logger.warning(f"Failed to initialize AsyncNexusFS: {e}")

        except Exception as e:
            logger.warning(f"Failed to initialize async ReBAC manager: {e}")

    # Initialize cache factory for Dragonfly/Redis or PostgreSQL fallback (Issue #1075, #1251)
    # Provides optimized connection pooling for permission/tiger caches
    try:
        from nexus.cache.factory import init_cache_factory
        from nexus.cache.settings import CacheSettings

        cache_settings = CacheSettings.from_env()

        # Pass RecordStore for SQL-backed cache fallback when CacheStoreABC is not available
        record_store = getattr(_app.state.nexus_fs, "_record_store", None)

        _app.state.cache_factory = await init_cache_factory(
            cache_settings, record_store=record_store
        )
        logger.info(
            f"Cache factory initialized with {_app.state.cache_factory.backend_name} backend"
        )

        # Wire up CacheStoreABC L2 cache to TigerCache (Issue #1106)
        # This enables L1 (memory) -> L2 (CacheStore) -> L3 (RecordStore) caching
        if _app.state.cache_factory.has_cache_store:
            tiger_cache = getattr(
                getattr(_app.state.nexus_fs, "_rebac_manager", None),
                "_tiger_cache",
                None,
            )
            if tiger_cache:
                dragonfly_tiger = _app.state.cache_factory.get_tiger_cache()
                tiger_cache.set_dragonfly_cache(dragonfly_tiger)
                logger.info(
                    "[TIGER] Dragonfly L2 cache wired up - "
                    "L1 (memory) -> L2 (Dragonfly) -> L3 (PostgreSQL)"
                )
    except Exception as e:
        logger.warning(f"Failed to initialize cache factory: {e}")

    # Event Log WAL for durable event persistence (Issue #1397)
    # Service-layer concern: WAL-first durability for EventBus, not a kernel pillar.
    # Rust WAL preferred; falls back to PG; None if neither available (graceful degrade).
    _app.state.event_log = None
    try:
        from nexus.services.event_log import EventLogConfig, create_event_log

        wal_dir = os.getenv("NEXUS_WAL_DIR", ".nexus-data/wal")
        sync_mode = os.getenv("NEXUS_WAL_SYNC_MODE", "every")
        segment_size = int(os.getenv("NEXUS_WAL_SEGMENT_SIZE", str(4 * 1024 * 1024)))

        event_log_config = EventLogConfig(
            wal_dir=Path(wal_dir),
            segment_size_bytes=segment_size,
            sync_mode=sync_mode,  # type: ignore[arg-type]
        )
        _app.state.event_log = create_event_log(
            event_log_config,
            session_factory=getattr(_app.state, "session_factory", None),
        )
        if _app.state.event_log:
            logger.info(f"Event log initialized (wal_dir={wal_dir}, sync_mode={sync_mode})")
    except Exception as e:
        logger.warning(f"Failed to initialize event log: {e}")

    # Issue #1397: Start event bus and wire event log for WAL-first persistence
    if _app.state.nexus_fs:
        event_bus_ref = getattr(_app.state.nexus_fs, "_event_bus", None)
        if event_bus_ref is not None:
            # Connect the underlying DragonflyClient (async init required)
            redis_client = getattr(event_bus_ref, "_redis", None)
            if redis_client and hasattr(redis_client, "connect"):
                try:
                    await redis_client.connect()
                except Exception as e:
                    logger.warning(f"Failed to connect event bus Redis client: {e}")

            # Start the event bus (sets _started=True, enables publish())
            if hasattr(event_bus_ref, "start") and not getattr(event_bus_ref, "_started", True):
                try:
                    await event_bus_ref.start()
                    logger.info("Event bus started for event publishing")
                except Exception as e:
                    logger.warning(f"Failed to start event bus: {e}")

            # Issue #1331: Store main event loop ref for cross-thread event publishing
            _app.state.nexus_fs._main_event_loop = asyncio.get_running_loop()

            # Wire event_log into EventBus for WAL-first durability (Issue #1397).
            # EventBus.publish() handles: WAL append (if available) → Dragonfly fan-out.
            if _app.state.event_log is not None:
                event_bus_ref._event_log = _app.state.event_log
                logger.info("Event log wired into EventBus (WAL-first before pub/sub)")

    # WebSocket Manager for real-time events (Issue #1116)
    # Bridges Redis Pub/Sub to WebSocket clients for push notifications
    try:
        from nexus.server.websocket import WebSocketManager

        # Get event bus from NexusFS if available
        event_bus = None
        if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "_event_bus"):
            event_bus = _app.state.nexus_fs._event_bus

        _app.state.websocket_manager = WebSocketManager(
            event_bus=event_bus,
            reactive_manager=_app.state.reactive_subscription_manager,
        )
        await _app.state.websocket_manager.start()
        logger.info("WebSocket manager started for real-time events")
    except Exception as e:
        logger.warning(f"Failed to start WebSocket manager: {e}")

    # Issue #1129/#1130: WriteBack Service for bidirectional sync (Nexus -> Backend)
    # Enable with NEXUS_WRITE_BACK=true (default: disabled)
    write_back_enabled = os.getenv("NEXUS_WRITE_BACK", "").lower() in ("true", "1", "yes")
    if write_back_enabled and _app.state.nexus_fs:
        try:
            from nexus.services.change_log_store import ChangeLogStore
            from nexus.services.conflict_log_store import ConflictLogStore
            from nexus.services.conflict_resolution import ConflictStrategy
            from nexus.services.gateway import NexusFSGateway
            from nexus.services.sync_backlog_store import SyncBacklogStore
            from nexus.services.write_back_service import WriteBackService

            gw = NexusFSGateway(_app.state.nexus_fs)

            # ConflictLogStore is always available for the REST API,
            # even if the full write-back pipeline can't start (no event bus).
            conflict_log_store = ConflictLogStore(gw)
            _app.state.conflict_log_store = conflict_log_store

            wb_event_bus = None
            if hasattr(_app.state.nexus_fs, "_event_bus"):
                wb_event_bus = _app.state.nexus_fs._event_bus
            if wb_event_bus:
                backlog_store = SyncBacklogStore(gw)
                change_log_store = ChangeLogStore(gw)

                # Map env var to ConflictStrategy (backward compat)
                _policy_map = {
                    "lww": ConflictStrategy.KEEP_NEWER,
                    "fork": ConflictStrategy.RENAME_CONFLICT,
                }
                raw_policy = os.getenv("NEXUS_CONFLICT_POLICY", "keep_newer")
                try:
                    default_strategy = ConflictStrategy(raw_policy)
                except ValueError:
                    default_strategy = _policy_map.get(raw_policy, ConflictStrategy.KEEP_NEWER)

                _app.state.write_back_service = WriteBackService(
                    gateway=gw,
                    event_bus=wb_event_bus,
                    backlog_store=backlog_store,
                    change_log_store=change_log_store,
                    default_strategy=default_strategy,
                    conflict_log_store=conflict_log_store,
                )
                await _app.state.write_back_service.start()
                logger.info("WriteBack service started for bidirectional sync")
            else:
                logger.debug("WriteBack service skipped: no event bus available")
        except Exception as e:
            logger.warning(f"Failed to start WriteBack service: {e}")

    # Connect Lock Manager coordination client (Issue #1186)
    # Required for distributed lock REST API endpoints
    if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "_coordination_client"):
        coord_client = _app.state.nexus_fs._coordination_client
        if coord_client is not None:
            try:
                await coord_client.connect()
                logger.info("Lock manager coordination client connected")
            except Exception as e:
                logger.warning(f"Failed to connect lock manager coordination client: {e}")

    # Hot Search Daemon (Issue #951)
    # Pre-warm search indexes for sub-50ms query response
    # Enable with NEXUS_SEARCH_DAEMON=true (default: enabled if database URL provided)
    search_daemon_enabled = os.getenv("NEXUS_SEARCH_DAEMON", "").lower() in (
        "true",
        "1",
        "yes",
    ) or (
        # Auto-enable if not explicitly disabled and database URL is set
        os.getenv("NEXUS_SEARCH_DAEMON", "").lower() not in ("false", "0", "no")
        and _app.state.database_url
    )

    if search_daemon_enabled:
        try:
            from nexus.search.daemon import DaemonConfig, SearchDaemon, set_search_daemon

            config = DaemonConfig(
                database_url=_app.state.database_url,
                bm25s_index_dir=os.getenv("NEXUS_BM25S_INDEX_DIR", ".nexus-data/bm25s"),
                db_pool_min_size=int(os.getenv("NEXUS_SEARCH_POOL_MIN", "10")),
                db_pool_max_size=int(os.getenv("NEXUS_SEARCH_POOL_MAX", "50")),
                refresh_enabled=os.getenv("NEXUS_SEARCH_REFRESH", "true").lower()
                in (
                    "true",
                    "1",
                    "yes",
                ),
                # Issue #1024: Entropy-aware filtering for redundant content
                entropy_filtering=os.getenv("NEXUS_ENTROPY_FILTERING", "false").lower()
                in ("true", "1", "yes"),
                entropy_threshold=float(os.getenv("NEXUS_ENTROPY_THRESHOLD", "0.35")),
                entropy_alpha=float(os.getenv("NEXUS_ENTROPY_ALPHA", "0.5")),
            )

            _app.state.search_daemon = SearchDaemon(config)
            await _app.state.search_daemon.startup()
            _app.state.search_daemon_enabled = True
            set_search_daemon(_app.state.search_daemon)

            # Set NexusFS reference for index refresh (Issue #1024)
            _app.state.search_daemon._nexus_fs = _app.state.nexus_fs

            stats = _app.state.search_daemon.get_stats()
            logger.info(
                f"Search Daemon started: {stats['bm25_documents']} docs indexed, "
                f"startup={stats['startup_time_ms']:.1f}ms"
            )
        except Exception as e:
            logger.warning(f"Failed to start Search Daemon: {e}")
            _app.state.search_daemon_enabled = False
    else:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")

    # Tiger Cache queue processor (Issue #935)
    # NOTE: Disabled by default - write-through handles grants/revokes immediately
    # Enable with NEXUS_ENABLE_TIGER_WORKER=true for cache warming scenarios
    tiger_task: asyncio.Task[Any] | None = None
    # Issue #913: Track startup tasks to prevent memory leaks on shutdown
    warm_task: asyncio.Task[Any] | None = None
    backfill_task: asyncio.Task[Any] | None = None
    if _app.state.nexus_fs and os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() in (
        "true",
        "1",
        "yes",
    ):
        try:
            from nexus.server.background_tasks import tiger_cache_queue_task

            tiger_task = asyncio.create_task(
                tiger_cache_queue_task(_app.state.nexus_fs, interval_seconds=60, batch_size=1)
            )
            logger.info("Tiger Cache queue processor started (explicit enable)")
        except Exception as e:
            logger.warning(f"Failed to start Tiger Cache queue processor: {e}")
    else:
        logger.debug("Tiger Cache queue processor disabled (write-through handles grants)")

    # Tiger Cache warm-up on startup (Issue #979)
    # Pre-load recently used permission bitmaps to avoid cold-start penalties
    # Non-blocking: runs in background thread, server starts immediately
    if _app.state.nexus_fs:
        try:
            tiger_cache = getattr(_app.state.nexus_fs._rebac_manager, "_tiger_cache", None)
            if tiger_cache:
                warm_limit = int(os.getenv("NEXUS_TIGER_CACHE_WARM_LIMIT", "500"))

                async def _warm_tiger_cache() -> None:
                    import asyncio

                    loaded = await asyncio.to_thread(tiger_cache.warm_from_db, warm_limit)
                    logger.info(f"Tiger Cache warmed with {loaded} entries from database")

                # Issue #913: Store task reference for proper shutdown
                warm_task = asyncio.create_task(_warm_tiger_cache())
                logger.debug(f"Tiger Cache warm-up started (limit={warm_limit})")

                # Start DirectoryGrantExpander worker for large directory grants (Leopard-style)
                # This processes pending directory grants asynchronously in background
                try:
                    from nexus.services.permissions.tiger_cache import DirectoryGrantExpander

                    expander = DirectoryGrantExpander(
                        engine=_app.state.nexus_fs._rebac_manager.engine,
                        tiger_cache=tiger_cache,
                        metadata_store=_app.state.nexus_fs.metadata,
                    )
                    _app.state.directory_grant_expander = expander

                    async def _run_grant_expander() -> None:
                        await expander.run_worker()

                    asyncio.create_task(_run_grant_expander())
                    logger.info("DirectoryGrantExpander worker started for large folder grants")
                except Exception as e:
                    logger.debug(f"DirectoryGrantExpander startup skipped: {e}")

        except Exception as e:
            logger.debug(f"Tiger Cache warm-up skipped: {e}")

    # Auto-backfill sparse directory index for system paths (Issue #perf19)
    # This ensures /skills and other shared paths have index entries for O(1) lookups
    if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "metadata"):
        try:
            _nexus_fs = _app.state.nexus_fs  # Capture for closure

            async def _backfill_system_paths() -> None:
                import asyncio

                for prefix in ["/skills", "/sessions"]:
                    try:
                        # Backfill without zone filter to include NULL zone files
                        created = await asyncio.to_thread(
                            _nexus_fs.metadata.backfill_directory_index,
                            prefix=prefix,
                            zone_id=None,
                        )
                        if created > 0:
                            logger.info(f"Sparse index backfill: {created} entries for {prefix}")
                    except Exception as e:
                        logger.debug(f"Sparse index backfill skipped for {prefix}: {e}")

            # Issue #913: Store task reference for proper shutdown
            backfill_task = asyncio.create_task(_backfill_system_paths())
            logger.info("Sparse directory index backfill started for system paths")
        except Exception as e:
            logger.warning(f"Sparse index backfill skipped: {e}")

    # Issue #1076: File cache warmup on server startup
    # Pre-load metadata for commonly accessed paths to reduce cold-start latency
    # Always enabled - runs in background, lightweight (metadata only), no downside
    if _app.state.nexus_fs:
        try:
            warmup_max_files = int(os.getenv("NEXUS_CACHE_WARMUP_MAX_FILES", "1000"))
            warmup_depth = int(os.getenv("NEXUS_CACHE_WARMUP_DEPTH", "2"))
            _nexus_fs_warmup = _app.state.nexus_fs  # Capture for closure

            async def _warmup_file_cache() -> None:
                from nexus.cache.warmer import CacheWarmer, WarmupConfig

                config = WarmupConfig(
                    max_files=warmup_max_files,
                    depth=warmup_depth,
                    include_content=False,  # Metadata only for fast startup
                )
                warmer = CacheWarmer(nexus_fs=_nexus_fs_warmup, config=config)
                stats = await warmer.warmup_directory(
                    path="/",
                    depth=warmup_depth,
                    include_content=False,
                    max_files=warmup_max_files,
                )
                logger.info(
                    f"[WARMUP] Server startup warmup complete: "
                    f"{stats.files_warmed} files, {stats.metadata_warmed} metadata entries"
                )

            asyncio.create_task(_warmup_file_cache())
            logger.info(
                f"[WARMUP] Server startup warmup started (max_files={warmup_max_files}, depth={warmup_depth})"
            )
        except Exception as e:
            logger.debug(f"[WARMUP] Server startup warmup skipped: {e}")

    # Issue #1240: Initialize AgentRegistry for agent lifecycle tracking
    if _app.state.nexus_fs and getattr(_app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.core.agent_registry import AgentRegistry

            _app.state.agent_registry = AgentRegistry(
                session_factory=_app.state.nexus_fs.SessionLocal,
                entity_registry=getattr(_app.state.nexus_fs, "_entity_registry", None),
                flush_interval=60,
            )
            # Inject into NexusFS for RPC methods
            _app.state.nexus_fs._agent_registry = _app.state.agent_registry

            # Wire into sync PermissionEnforcer
            perm_enforcer = getattr(_app.state.nexus_fs, "_permission_enforcer", None)
            if perm_enforcer is not None:
                perm_enforcer.agent_registry = _app.state.agent_registry

            # Issue #1440: Create async wrapper for protocol conformance
            from nexus.services.agents.async_agent_registry import AsyncAgentRegistry

            _app.state.async_agent_registry = AsyncAgentRegistry(_app.state.agent_registry)

            logger.info("[AGENT-REG] AgentRegistry initialized and wired")
        except Exception as e:
            logger.warning(f"[AGENT-REG] Failed to initialize AgentRegistry: {e}")
            _app.state.agent_registry = None
            _app.state.async_agent_registry = None
    else:
        _app.state.agent_registry = None
        _app.state.async_agent_registry = None

    # Issue #1355: Initialize KeyService for agent identity
    if _app.state.nexus_fs and getattr(_app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.identity.crypto import IdentityCrypto
            from nexus.identity.key_service import KeyService
            from nexus.identity.models import AgentKeyModel  # noqa: F401 — register with Base
            from nexus.server.auth.oauth_crypto import OAuthCrypto

            # Ensure agent_keys table exists (AgentKeyModel is imported lazily,
            # after SQLAlchemyRecordStore.create_all already ran)
            _nx_engine = getattr(_app.state.nexus_fs, "_sql_engine", None)
            if _nx_engine is not None:
                AgentKeyModel.__table__.create(_nx_engine, checkfirst=True)

            # Reuse OAuthCrypto for Fernet encryption of private keys
            _db_url = _app.state.database_url or "sqlite:///nexus.db"
            _identity_oauth_crypto = OAuthCrypto(db_url=_db_url)
            _identity_crypto = IdentityCrypto(oauth_crypto=_identity_oauth_crypto)

            _app.state.key_service = KeyService(
                session_factory=_app.state.nexus_fs.SessionLocal,
                crypto=_identity_crypto,
            )
            # Inject into NexusFS for register_agent integration
            _app.state.nexus_fs._key_service = _app.state.key_service

            logger.info("[KYA] KeyService initialized and wired")
        except Exception as e:
            logger.warning(f"[KYA] Failed to initialize KeyService: {e}")
            _app.state.key_service = None
    else:
        _app.state.key_service = None

    # Issue #788: Initialize ChunkedUploadService for tus.io resumable uploads
    # Prefer the pre-configured service from factory (reads NEXUS_UPLOAD_* env vars).
    # Fall back to creating one here if the factory didn't provide it.
    _upload_cleanup_task = None
    _factory_upload_svc = (
        _app.state.nexus_fs._service_extras.get("chunked_upload_service")
        if _app.state.nexus_fs
        else None
    )
    if _factory_upload_svc is not None:
        _app.state.chunked_upload_service = _factory_upload_svc
        _upload_cleanup_task = asyncio.create_task(
            _app.state.chunked_upload_service.start_cleanup_loop()
        )
        logger.info("[TUS] ChunkedUploadService initialized from factory with background cleanup")
    elif _app.state.nexus_fs and getattr(_app.state.nexus_fs, "SessionLocal", None):
        try:
            from nexus.services.chunked_upload_service import (
                ChunkedUploadConfig,
                ChunkedUploadService,
            )

            _backend = getattr(_app.state.nexus_fs, "backend", None)
            _session_factory = _app.state.nexus_fs.SessionLocal
            if _backend and _session_factory:
                # Build config from env vars for fallback path
                import os as _os

                _upload_kwargs: dict = {}
                for _env, _key in {
                    "NEXUS_UPLOAD_MIN_CHUNK_SIZE": "min_chunk_size",
                    "NEXUS_UPLOAD_MAX_CHUNK_SIZE": "max_chunk_size",
                    "NEXUS_UPLOAD_MAX_CONCURRENT": "max_concurrent_uploads",
                    "NEXUS_UPLOAD_SESSION_TTL_HOURS": "session_ttl_hours",
                    "NEXUS_UPLOAD_CLEANUP_INTERVAL": "cleanup_interval_seconds",
                    "NEXUS_UPLOAD_MAX_SIZE": "max_upload_size",
                }.items():
                    _v = _os.getenv(_env)
                    if _v is not None:
                        _upload_kwargs[_key] = int(_v)

                _app.state.chunked_upload_service = ChunkedUploadService(
                    session_factory=_session_factory,
                    backend=_backend,
                    metadata_store=getattr(_app.state.nexus_fs, "metadata", None),
                    config=ChunkedUploadConfig(**_upload_kwargs),
                )
                _upload_cleanup_task = asyncio.create_task(
                    _app.state.chunked_upload_service.start_cleanup_loop()
                )
                logger.info("[TUS] ChunkedUploadService initialized with background cleanup")
        except Exception as e:
            logger.warning(f"[TUS] Failed to initialize ChunkedUploadService: {e}")
            _app.state.chunked_upload_service = None
    else:
        _app.state.chunked_upload_service = None

    # Issue #726: Wire circuit breaker from factory for health endpoint access
    if _app.state.nexus_fs:
        _app.state.rebac_circuit_breaker = _app.state.nexus_fs._service_extras.get(
            "rebac_circuit_breaker"
        )

    # Issue #1240: Start agent heartbeat and stale detection background tasks
    _heartbeat_task = None
    _stale_detection_task = None
    if _app.state.agent_registry:
        from nexus.server.background_tasks import (
            heartbeat_flush_task,
            stale_agent_detection_task,
        )

        _heartbeat_task = asyncio.create_task(
            heartbeat_flush_task(_app.state.agent_registry, interval_seconds=60)
        )
        _stale_detection_task = asyncio.create_task(
            stale_agent_detection_task(_app.state.agent_registry, interval_seconds=300)
        )
        logger.info("[AGENT-REG] Background heartbeat flush and stale detection tasks started")

    # Issue #1307: Initialize SandboxAuthService for authenticated sandbox creation
    if _app.state.nexus_fs and not _app.state.agent_registry:
        logger.info(
            "[SANDBOX-AUTH] AgentRegistry not available, SandboxAuthService will not be initialized"
        )
    if _app.state.nexus_fs and _app.state.agent_registry:
        try:
            from nexus.sandbox.auth_service import SandboxAuthService
            from nexus.sandbox.events import AgentEventLog
            from nexus.sandbox.sandbox_manager import SandboxManager

            session_factory = getattr(_app.state.nexus_fs, "SessionLocal", None)
            if session_factory and callable(session_factory):
                # Create AgentEventLog for sandbox lifecycle audit
                _app.state.agent_event_log = AgentEventLog(session_factory=session_factory)

                # Create SandboxManager for SandboxAuthService
                # (separate from NexusFS's lazy sandbox manager — different layers)
                sandbox_config = getattr(_app.state.nexus_fs, "_config", None)
                sandbox_mgr = SandboxManager(
                    session_factory=session_factory,
                    e2b_api_key=os.getenv("E2B_API_KEY"),
                    e2b_team_id=os.getenv("E2B_TEAM_ID"),
                    e2b_template_id=os.getenv("E2B_TEMPLATE_ID"),
                    config=sandbox_config,
                )

                # Get NamespaceManager if available (best-effort)
                # Issue #1265: Factory function handles L3 persistent store wiring
                namespace_manager = None
                sync_rebac = getattr(_app.state.nexus_fs, "_rebac_manager", None)
                if sync_rebac:
                    try:
                        from nexus.services.permissions.namespace_factory import (
                            create_namespace_manager,
                        )

                        ns_record_store = getattr(_app.state.nexus_fs, "_record_store", None)
                        namespace_manager = create_namespace_manager(
                            rebac_manager=sync_rebac,
                            record_store=ns_record_store,
                        )
                    except Exception as e:
                        logger.info(
                            "[SANDBOX-AUTH] NamespaceManager not available (%s), "
                            "sandbox mount tables will be empty",
                            e,
                        )

                _app.state.sandbox_auth_service = SandboxAuthService(
                    agent_registry=_app.state.agent_registry,
                    sandbox_manager=sandbox_mgr,
                    namespace_manager=namespace_manager,
                    event_log=_app.state.agent_event_log,
                    budget_enforcement=False,
                )
                logger.info("[SANDBOX-AUTH] SandboxAuthService initialized")
        except Exception as e:
            logger.warning(f"[SANDBOX-AUTH] Failed to initialize SandboxAuthService: {e}")

    # Issue #1212: Initialize SchedulerService if PostgreSQL database is available
    _scheduler_pool = None
    if _app.state.database_url and "postgresql" in _app.state.database_url:
        try:
            import asyncpg

            from nexus.pay.credits import CreditsService
            from nexus.scheduler.queue import TaskQueue
            from nexus.scheduler.service import SchedulerService

            # Convert SQLAlchemy URL to asyncpg DSN
            pg_dsn = _app.state.database_url.replace("+asyncpg", "").replace("+psycopg2", "")
            _scheduler_pool = await asyncpg.create_pool(pg_dsn, min_size=2, max_size=5)

            # Create scheduled_tasks table if it doesn't exist
            async with _scheduler_pool.acquire() as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS scheduled_tasks (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        agent_id TEXT NOT NULL,
                        executor_id TEXT NOT NULL,
                        task_type TEXT NOT NULL,
                        payload JSONB NOT NULL DEFAULT '{}',
                        priority_tier SMALLINT NOT NULL DEFAULT 2,
                        deadline TIMESTAMPTZ,
                        boost_amount NUMERIC(12,6) NOT NULL DEFAULT 0,
                        boost_tiers SMALLINT NOT NULL DEFAULT 0,
                        effective_tier SMALLINT NOT NULL DEFAULT 2,
                        enqueued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        started_at TIMESTAMPTZ,
                        completed_at TIMESTAMPTZ,
                        status TEXT NOT NULL DEFAULT 'queued',
                        boost_reservation_id TEXT,
                        idempotency_key TEXT UNIQUE,
                        zone_id TEXT NOT NULL DEFAULT 'default',
                        error_message TEXT
                    )
                """)
                await conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_dequeue
                    ON scheduled_tasks (effective_tier, enqueued_at)
                    WHERE status = 'queued'
                """)

            scheduler_service = SchedulerService(
                queue=TaskQueue(),
                db_pool=_scheduler_pool,
                credits_service=CreditsService(enabled=False),
            )
            _app.state.scheduler_service = scheduler_service
            logger.info("Scheduler service initialized (PostgreSQL)")
        except ImportError as e:
            logger.debug(f"Scheduler service not available: {e}")
        except Exception as e:
            logger.warning(f"Failed to initialize Scheduler service: {e}")

    # Issue #574: Task Queue Engine - Start background worker
    task_runner_task: asyncio.Task[Any] | None = None
    if _app.state.nexus_fs:
        try:
            from nexus.tasks import is_available

            if is_available():
                service = _app.state.nexus_fs.task_queue_service
                engine = service.get_engine()

                from nexus.tasks.runner import AsyncTaskRunner

                runner = AsyncTaskRunner(engine=engine, max_workers=4)
                service.set_runner(runner)
                _app.state.task_runner = runner
                task_runner_task = asyncio.create_task(runner.run())
                logger.info("Task Queue runner started (4 workers)")
            else:
                logger.debug("Task Queue: nexus_tasks Rust extension not available")
        except Exception as e:
            logger.warning(f"Task Queue runner not started: {e}")

    yield

    # Cleanup
    logger.info("Shutting down FastAPI Nexus server...")

    # Issue #1331: Stop event bus
    _ebus_stop = getattr(_app.state.nexus_fs, "_event_bus", None) if _app.state.nexus_fs else None
    if _ebus_stop is not None:
        try:
            await _ebus_stop.stop()
            logger.info("Event bus stopped")
        except Exception as e:
            logger.warning(f"Error shutting down event bus: {e}")

    # Issue #788: Stop upload cleanup task
    if _upload_cleanup_task:
        _upload_cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await _upload_cleanup_task
        logger.info("[TUS] Upload cleanup task stopped")

    # Issue #574: Stop Task Queue runner
    if task_runner_task:
        try:
            if _app.state.task_runner:
                await _app.state.task_runner.shutdown()
            task_runner_task.cancel()
            with suppress(asyncio.CancelledError):
                await task_runner_task
            logger.info("Task Queue runner stopped")
        except Exception as e:
            logger.warning(f"Error shutting down Task Queue runner: {e}")

    # Issue #1212: Shutdown scheduler pool
    if _scheduler_pool:
        try:
            await _scheduler_pool.close()
            logger.info("Scheduler pool closed")
        except Exception as e:
            logger.warning(f"Error closing scheduler pool: {e}")

    # Issue #940: Shutdown AsyncNexusFS
    if _app.state.async_nexus_fs:
        try:
            await _app.state.async_nexus_fs.close()
            logger.info("AsyncNexusFS stopped")
        except Exception as e:
            logger.warning(f"Error shutting down AsyncNexusFS: {e}")

    # Shutdown Search Daemon (Issue #951)
    if _app.state.search_daemon:
        try:
            await _app.state.search_daemon.shutdown()
            logger.info("Search Daemon stopped")
        except Exception as e:
            logger.warning(f"Error shutting down Search Daemon: {e}")

    # Issue #1129: Stop WriteBack Service
    if _app.state.write_back_service:
        try:
            await _app.state.write_back_service.stop()
            logger.info("WriteBack service stopped")
        except Exception as e:
            logger.warning(f"Error shutting down WriteBack service: {e}")

    # Stop DirectoryGrantExpander worker
    if hasattr(_app.state, "directory_grant_expander") and _app.state.directory_grant_expander:
        try:
            _app.state.directory_grant_expander.stop()
            logger.info("DirectoryGrantExpander worker stopped")
        except Exception as e:
            logger.debug(f"Error stopping DirectoryGrantExpander: {e}")

    # Issue #1240: Cancel agent background tasks and final flush
    for task_ref in (_heartbeat_task, _stale_detection_task):
        if task_ref and not task_ref.done():
            task_ref.cancel()
            with suppress(asyncio.CancelledError):
                await task_ref
    if _app.state.agent_registry:
        try:
            _app.state.agent_registry.flush_heartbeats()
            logger.info("[AGENT-REG] Final heartbeat flush completed")
        except Exception:
            logger.warning("[AGENT-REG] Final heartbeat flush failed", exc_info=True)

    # Issue #1397: Close Event Log WAL
    if _app.state.event_log:
        try:
            await _app.state.event_log.close()
            logger.info("Event log closed")
        except Exception as e:
            logger.warning(f"Error closing event log: {e}")

    # SandboxManager now uses session-per-operation — no persistent session to close
    if _app.state.sandbox_auth_service:
        logger.info(
            "[SANDBOX-AUTH] SandboxAuthService cleaned up (session-per-op, no persistent session)"
        )

    # Cancel Tiger Cache task
    if tiger_task:
        tiger_task.cancel()
        with suppress(asyncio.CancelledError):
            await tiger_task
        logger.info("Tiger Cache queue processor stopped")

    # Issue #913: Cancel startup tasks to prevent leaks
    if warm_task:
        warm_task.cancel()
        with suppress(asyncio.CancelledError):
            await warm_task
        logger.debug("Tiger Cache warm-up task cancelled")
    if backfill_task:
        backfill_task.cancel()
        with suppress(asyncio.CancelledError):
            await backfill_task
        logger.debug("Sparse index backfill task cancelled")

    # Issue #913: Cancel any pending event tasks in NexusFS
    if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "_event_tasks"):
        event_tasks = _app.state.nexus_fs._event_tasks.copy()
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*event_tasks, return_exceptions=True)
            logger.info(f"Cancelled {len(event_tasks)} pending event tasks")

    # Shutdown WebSocket manager (Issue #1116)
    if _app.state.websocket_manager:
        try:
            await _app.state.websocket_manager.stop()
            logger.info("WebSocket manager stopped")
        except Exception as e:
            logger.warning(f"Error shutting down WebSocket manager: {e}")

    # Disconnect Lock Manager coordination client (Issue #1186)
    if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "_coordination_client"):
        coord_client = _app.state.nexus_fs._coordination_client
        if coord_client is not None:
            try:
                await coord_client.disconnect()
                logger.info("Lock manager coordination client disconnected")
            except Exception as e:
                logger.debug(f"Error disconnecting coordination client: {e}")

    if _app.state.subscription_manager:
        await _app.state.subscription_manager.close()
        # Clear global singleton (Issue #1115)
        from nexus.server.subscriptions import set_subscription_manager

        set_subscription_manager(None)
    if _app.state.nexus_fs and hasattr(_app.state.nexus_fs, "close"):
        _app.state.nexus_fs.close()

    # Shutdown cache factory (Issue #1075)
    if hasattr(_app.state, "cache_factory") and _app.state.cache_factory:
        try:
            await _app.state.cache_factory.shutdown()
            logger.info("Cache factory stopped")
        except Exception as e:
            logger.warning(f"Error shutting down cache factory: {e}")

    # Shutdown OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import shutdown_telemetry

        shutdown_telemetry()
    except ImportError:
        pass

    # Shutdown Sentry (Issue #759) — flush pending events
    try:
        from nexus.server.sentry import shutdown_sentry

        shutdown_sentry()
    except ImportError:
        pass


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

        obs_sub = nexus_fs._service_extras.get("observability_subsystem")
        if obs_sub is not None:
            REGISTRY.register(QueryObserverCollector(obs_sub.observer))
    except Exception:
        pass

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
        google_redirect_uri = os.getenv(
            "GOOGLE_REDIRECT_URI", "http://localhost:5173/oauth/callback"
        )
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
    """Register all routes."""

    # Health check (exempt from rate limiting - must always be accessible)
    @app.get("/health", response_model=HealthResponse)
    @limiter.exempt
    async def health_check() -> HealthResponse:
        # Include configuration status for debugging
        enforce_permissions = None
        enforce_zone_isolation = None
        has_auth = None

        if _fastapi_app.state.nexus_fs:
            enforce_permissions = getattr(_fastapi_app.state.nexus_fs, "_enforce_permissions", None)
            enforce_zone_isolation = getattr(
                _fastapi_app.state.nexus_fs, "_enforce_zone_isolation", None
            )

        # Check if authentication is configured
        has_auth = bool(_fastapi_app.state.api_key or _fastapi_app.state.auth_provider)

        return HealthResponse(
            status="healthy",
            service="nexus-rpc",
            enforce_permissions=enforce_permissions,
            enforce_zone_isolation=enforce_zone_isolation,
            has_auth=has_auth,
        )

    # Extended health check with component status (Issue #951)
    @app.get("/health/detailed", tags=["health"])
    @limiter.exempt
    async def health_check_detailed() -> dict[str, Any]:
        """Detailed health check including all components.

        Returns status of:
        - Core service
        - Search daemon (if enabled)
        - Database connection
        - Background tasks
        - Mounted backends (Issue #708)
        """
        health: dict[str, Any] = {
            "status": "healthy",
            "service": "nexus-rpc",
            "components": {},
        }

        # Check search daemon (Issue #951)
        if _fastapi_app.state.search_daemon:
            daemon_health = _fastapi_app.state.search_daemon.get_health()
            health["components"]["search_daemon"] = daemon_health
        else:
            health["components"]["search_daemon"] = {
                "status": "disabled",
                "message": "Set NEXUS_SEARCH_DAEMON=true to enable",
            }

        # Check ReBAC + circuit breaker (Issue #726)
        rebac_health: dict[str, Any] = {"status": "disabled"}
        has_rebac = _fastapi_app.state.async_rebac_manager or getattr(
            _fastapi_app.state.nexus_fs, "_rebac_manager", None
        )
        if has_rebac:
            cb = getattr(_fastapi_app.state, "rebac_circuit_breaker", None)
            if cb:
                from nexus.services.permissions.circuit_breaker import CircuitState

                cb_state = cb.state
                if cb_state == CircuitState.CLOSED:
                    rebac_status = "healthy"
                elif cb_state == CircuitState.HALF_OPEN:
                    rebac_status = "degraded"
                else:
                    rebac_status = "unhealthy"
                rebac_health = {
                    "status": rebac_status,
                    "circuit_state": cb_state.value,
                    "failure_count": cb.failure_count,
                    "open_count": cb.open_count,
                    "last_failure_time": cb.last_failure_time,
                }
            else:
                rebac_health = {"status": "healthy"}
        health["components"]["rebac"] = rebac_health

        # Check subscription manager
        health["components"]["subscriptions"] = {
            "status": "healthy" if _fastapi_app.state.subscription_manager else "disabled",
        }

        # Check WebSocket manager (Issue #1116)
        if _fastapi_app.state.websocket_manager:
            ws_stats = _fastapi_app.state.websocket_manager.get_stats()
            health["components"]["websocket"] = {
                "status": "healthy",
                "current_connections": ws_stats["current_connections"],
                "total_connections": ws_stats["total_connections"],
                "total_messages_sent": ws_stats["total_messages_sent"],
                "connections_by_zone": ws_stats["connections_by_zone"],
            }
        else:
            health["components"]["websocket"] = {"status": "disabled"}

        # Check Reactive Subscription Manager (Issue #1167)
        if _fastapi_app.state.reactive_subscription_manager:
            try:
                reactive_stats = _fastapi_app.state.reactive_subscription_manager.get_stats()
                health["components"]["reactive_subscriptions"] = {
                    "status": "healthy",
                    "total_subscriptions": reactive_stats["total_subscriptions"],
                    "read_set_subscriptions": reactive_stats["read_set_subscriptions"],
                    "pattern_subscriptions": reactive_stats["pattern_subscriptions"],
                    "avg_lookup_ms": reactive_stats["avg_lookup_ms"],
                    "registry": reactive_stats["registry"],
                }
            except Exception as e:
                health["components"]["reactive_subscriptions"] = {
                    "status": "error",
                    "error": str(e),
                }
        else:
            health["components"]["reactive_subscriptions"] = {"status": "disabled"}

        # Check mounted backends (Issue #708)
        backends_health: dict[str, Any] = {}
        if _fastapi_app.state.nexus_fs and hasattr(_fastapi_app.state.nexus_fs, "path_router"):
            mounts = _fastapi_app.state.nexus_fs.path_router.list_mounts()
            for mount in mounts:
                backend = mount.backend
                mount_point = mount.mount_point

                # Call check_connection on backend
                try:
                    # Note: For user-scoped backends, health check without context
                    # will return limited info. Full per-user health requires context.
                    status = backend.check_connection()
                    backends_health[mount_point] = {
                        "backend": backend.name,
                        "healthy": status.success,
                        "latency_ms": status.latency_ms,
                        "user_scoped": backend.user_scoped,
                        "thread_safe": backend.thread_safe,
                    }
                    if status.error_message:
                        backends_health[mount_point]["error"] = status.error_message
                    if status.details:
                        backends_health[mount_point]["details"] = status.details
                except Exception as e:
                    backends_health[mount_point] = {
                        "backend": backend.name,
                        "healthy": False,
                        "error": str(e),
                    }

        health["components"]["backends"] = backends_health

        # Update overall status if any backend is unhealthy
        unhealthy_backends = [k for k, v in backends_health.items() if not v.get("healthy", True)]
        if unhealthy_backends:
            health["status"] = "degraded"
            health["unhealthy_backends"] = unhealthy_backends

        # Circuit breaker health (Issue #1366)
        _resiliency_mgr = (
            _fastapi_app.state.nexus_fs._service_extras.get("resiliency_manager")
            if _fastapi_app.state.nexus_fs
            and hasattr(_fastapi_app.state.nexus_fs, "_service_extras")
            else None
        )
        if _resiliency_mgr is not None:
            health["components"]["resiliency"] = _resiliency_mgr.health_check()
            if health["components"]["resiliency"]["status"] == "degraded":
                health["status"] = "degraded"

        return health

    # Connection pool metrics endpoint (Issue #1075)
    @app.get("/metrics/pool", tags=["health"])
    @limiter.exempt
    async def pool_metrics() -> dict[str, Any]:
        """Get database connection pool metrics.

        Returns metrics for PostgreSQL and Redis/Dragonfly connection pools:
        - pool_size: Base number of connections
        - checked_out: Connections currently in use
        - overflow: Connections beyond pool_size
        - available: Connections ready to use

        Useful for monitoring pool utilization and identifying
        connection exhaustion issues.
        """
        metrics: dict[str, Any] = {}

        # PostgreSQL pool stats from metadata store
        if _fastapi_app.state.nexus_fs and hasattr(_fastapi_app.state.nexus_fs, "metadata"):
            try:
                pg_stats = _fastapi_app.state.nexus_fs.metadata.get_pool_stats()
                metrics["postgres"] = pg_stats
            except Exception as e:
                metrics["postgres"] = {"error": str(e)}
        else:
            metrics["postgres"] = {"status": "not_available"}

        # Redis/Dragonfly pool stats from cache factory
        try:
            from nexus.cache.factory import get_cache_factory

            cache_factory = get_cache_factory()
            if cache_factory.has_cache_store and cache_factory._cache_client:
                dragonfly_stats = cache_factory._cache_client.get_pool_stats()
                dragonfly_info = await cache_factory._cache_client.get_info()
                metrics["dragonfly"] = {
                    **dragonfly_stats,
                    "server_info": dragonfly_info,
                }
            else:
                metrics["dragonfly"] = {"status": "not_configured"}
        except RuntimeError:
            # Cache factory not initialized
            metrics["dragonfly"] = {"status": "not_initialized"}
        except Exception as e:
            metrics["dragonfly"] = {"error": str(e)}

        return metrics

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

    # A2A Protocol Endpoint (Issue #1256)
    try:
        from nexus.a2a import create_a2a_router

        a2a_base_url = os.environ.get("NEXUS_A2A_BASE_URL", "http://localhost:2026")
        a2a_auth_required = bool(
            getattr(_fastapi_app.state, "api_key", None)
            or getattr(_fastapi_app.state, "auth_provider", None)
        )
        a2a_router = create_a2a_router(
            nexus_fs=_fastapi_app.state.nexus_fs,
            config=None,  # Will use defaults; config can be passed when available
            base_url=a2a_base_url,
            auth_required=a2a_auth_required,
            data_dir=getattr(_fastapi_app.state, "data_dir", None),
        )
        app.include_router(a2a_router)
        logger.info("A2A protocol endpoint registered (/.well-known/agent.json + /a2a)")
    except ImportError as e:
        logger.warning(f"Failed to import A2A router: {e}. A2A endpoint will not be available.")

    # Asyncio debug endpoint (Python 3.14+)
    @app.get("/debug/asyncio", tags=["debug"])
    async def debug_asyncio() -> dict[str, Any]:
        """Debug endpoint for asyncio task introspection.

        Returns information about running async tasks, including:
        - Total task count
        - Current task info
        - Call graph (Python 3.14+ only)

        This is useful for debugging stuck or slow async operations.
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

    # Auth whoami
    @app.get("/api/auth/whoami", response_model=WhoamiResponse)
    async def whoami(
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> WhoamiResponse:
        if auth_result is None or not auth_result.get("authenticated"):
            return WhoamiResponse(authenticated=False)

        return WhoamiResponse(
            authenticated=True,
            subject_type=auth_result.get("subject_type"),
            subject_id=auth_result.get("subject_id"),
            zone_id=auth_result.get("zone_id"),
            is_admin=auth_result.get("is_admin", False),
            inherit_permissions=auth_result.get("inherit_permissions", True),
            user=auth_result.get("subject_id"),
        )

    # Status endpoint
    @app.get("/api/nfs/status")
    async def status() -> dict[str, Any]:
        return {
            "status": "running",
            "service": "nexus-rpc",
            "version": "1.0",
            "async": True,
            "methods": list(_fastapi_app.state.exposed_methods.keys()),
        }

    # Domain endpoints extracted to api/v1/routers/ (#1288):
    # search, memory, graph, admin, cache, events, share, locks, subscriptions, identity

    # ========================================================================
    # Streaming Endpoint for Local Backend
    # ========================================================================

    @app.get("/api/stream/{path:path}", tags=["streaming"], response_model=None)
    async def stream_file(
        request: Request,
        path: str,
        token: str = Query(..., description="Signed stream token"),
        zone_id: str = Query("default", description="Zone ID"),
    ) -> Response | StreamingResponse:
        """Stream file content with HTTP Range support (RFC 9110).

        Used by the local backend when return_url=True is requested.
        The token is generated by _generate_download_url() and contains a signed
        expiration timestamp for security. Supports partial content (206),
        full content (200), and range not satisfiable (416) responses.
        """
        from nexus.server.range_utils import build_range_response

        # Verify token
        if not _verify_stream_token(token, f"/{path}", zone_id):
            raise HTTPException(status_code=403, detail="Invalid or expired stream token")

        nexus_fs = _fastapi_app.state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        try:
            full_path = f"/{path}"

            from nexus.core.permissions import OperationContext

            context = OperationContext(
                user="system",
                groups=[],
                zone_id=zone_id,
                subject_type="system",
                subject_id="stream",
            )

            meta = await to_thread_with_timeout(nexus_fs.stat, full_path, context=context)
            content_hash = meta.get("etag") or meta.get("content_hash")
            if not content_hash:
                raise HTTPException(status_code=500, detail="File has no content hash")

            total_size = meta.get("size", 0)

            # Use kernel stream methods — never reach through to the backend
            # directly (ObjectStoreABC abstraction boundary).
            return build_range_response(
                request_headers=request.headers,
                content_generator=lambda s, e, cs: nexus_fs.stream_range(
                    full_path, s, e, chunk_size=cs, context=context
                ),
                full_generator=lambda: nexus_fs.stream(full_path, context=context),
                total_size=total_size,
                etag=content_hash,
                content_type="application/octet-stream",
                filename=path.split("/")[-1],
                extra_headers={"X-Content-Hash": content_hash},
            )

        except NexusFileNotFoundError:
            raise HTTPException(status_code=404, detail=f"File not found: /{path}") from None
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from None
        except Exception as e:
            logger.error(f"Stream error for /{path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Stream error: {e}") from e

    # Main RPC endpoint (authenticated users get RATE_LIMIT_AUTHENTICATED)
    # Rate limiting key is extracted from Bearer token to identify users
    @app.post("/api/nfs/{method}")
    @limiter.limit(RATE_LIMIT_AUTHENTICATED)
    async def rpc_endpoint(
        method: str,
        request: Request,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> Response:
        """Handle RPC method calls."""
        import time as _time

        _rpc_start = _time.time()

        try:
            # Parse request body using decode_rpc_message to handle bytes encoding
            _parse_start = _time.time()
            body_bytes = await request.body()
            body = decode_rpc_message(body_bytes) if body_bytes else {}
            rpc_request = RPCRequest.from_dict(body)
            _parse_elapsed = (_time.time() - _parse_start) * 1000

            # Validate method matches URL
            if rpc_request.method and rpc_request.method != method:
                return _error_response(
                    rpc_request.id,
                    RPCErrorCode.INVALID_REQUEST,
                    f"Method mismatch: URL={method}, body={rpc_request.method}",
                )

            # Set method from URL if not in body
            if not rpc_request.method:
                rpc_request.method = method

            # Parse parameters
            params = parse_method_params(method, rpc_request.params)

            # Get operation context
            context = get_operation_context(auth_result)

            _setup_elapsed = (_time.time() - _rpc_start) * 1000 - _parse_elapsed

            # Early 304 check for read operations - check ETag BEFORE reading content
            # This avoids downloading/reading content if client already has it cached
            if_none_match = request.headers.get("If-None-Match")
            if (
                method == "read"
                and if_none_match
                and hasattr(params, "path")
                and _fastapi_app.state.nexus_fs
            ):
                try:
                    # Get ETag from metadata without reading content (fast!)
                    cached_etag = _fastapi_app.state.nexus_fs.get_etag(params.path, context=context)
                    if cached_etag:
                        client_etag = if_none_match.strip('"')
                        if client_etag == cached_etag:
                            # ETag matches - return 304 without reading content
                            logger.debug(f"Early 304: {params.path} (ETag match, no content read)")
                            return Response(
                                status_code=304,
                                headers={
                                    "ETag": f'"{cached_etag}"',
                                    "Cache-Control": "private, max-age=60",
                                },
                            )
                except Exception as e:
                    # If ETag check fails, fall through to normal read
                    logger.debug(f"Early ETag check failed for {params.path}: {e}")

            # Dispatch method
            _dispatch_start = _time.time()
            result = await _dispatch_method(method, params, context)
            _dispatch_elapsed = (_time.time() - _dispatch_start) * 1000

            # Build response with cache headers (includes ETag for read operations)
            headers = _get_cache_headers(method, result)

            # Late 304 check - fallback for cases where early check didn't apply
            # (e.g., ETag computed from response content)
            if if_none_match and "ETag" in headers:
                # Strip quotes and compare
                client_etag = if_none_match.strip('"')
                server_etag = headers["ETag"].strip('"')
                if client_etag == server_etag:
                    # Return 304 Not Modified - no body needed
                    return Response(
                        status_code=304,
                        headers={
                            "ETag": headers["ETag"],
                            "Cache-Control": headers.get("Cache-Control", ""),
                        },
                    )

            # Success response - use encode_rpc_message for proper serialization
            _encode_start = _time.time()
            success_response = {
                "jsonrpc": "2.0",
                "id": rpc_request.id,
                "result": result,
            }
            # encode_rpc_message handles bytes, datetime, etc.
            encoded = encode_rpc_message(success_response)
            _encode_elapsed = (_time.time() - _encode_start) * 1000
            _total_rpc = (_time.time() - _rpc_start) * 1000

            # Log API timing (auth time not included in total - happens in Depends before this)
            _auth_time = auth_result.get("_auth_time_ms", 0) if auth_result else 0
            _full_server_time = _auth_time + _total_rpc
            if _full_server_time > 20:  # Log if server time >20ms
                logger.info(
                    f"[RPC-TIMING] method={method}, auth={_auth_time:.1f}ms, parse={_parse_elapsed:.1f}ms, "
                    f"setup={_setup_elapsed:.1f}ms, dispatch={_dispatch_elapsed:.1f}ms, "
                    f"encode={_encode_elapsed:.1f}ms, rpc={_total_rpc:.1f}ms, server_total={_full_server_time:.1f}ms"
                )

            # Using Response directly with pre-encoded JSON for performance
            return Response(content=encoded, media_type="application/json", headers=headers)

        except ValueError as e:
            return _error_response(None, RPCErrorCode.INVALID_PARAMS, f"Invalid parameters: {e}")
        except NexusFileNotFoundError as e:
            return _error_response(None, RPCErrorCode.FILE_NOT_FOUND, str(e))
        except InvalidPathError as e:
            return _error_response(None, RPCErrorCode.INVALID_PATH, str(e))
        except NexusPermissionError as e:
            return _error_response(None, RPCErrorCode.PERMISSION_ERROR, str(e))
        except ValidationError as e:
            return _error_response(None, RPCErrorCode.VALIDATION_ERROR, str(e))
        except ConflictError as e:
            return _error_response(
                None,
                RPCErrorCode.CONFLICT,
                str(e),
                data={
                    "path": e.path,
                    "expected_etag": e.expected_etag,
                    "current_etag": e.current_etag,
                },
            )
        except NexusError as e:
            logger.warning(f"NexusError in method {method}: {e}")
            return _error_response(None, RPCErrorCode.INTERNAL_ERROR, f"Nexus error: {e}")
        except Exception as e:
            logger.exception(f"Error executing method {method}")
            return _error_response(None, RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}")


def _get_cache_headers(method: str, result: Any) -> dict[str, str]:
    """Generate appropriate cache headers based on method and result.

    Cache strategy:
    - Read operations: Cache with ETag for validation
    - List/glob operations: Short cache with private scope
    - Write/delete operations: No cache
    - Metadata operations: Short cache

    Args:
        method: RPC method name
        result: Response result

    Returns:
        Dict of HTTP cache headers
    """

    headers: dict[str, str] = {}

    # Read operations - cache with ETag
    if method == "read":
        # Generate ETag from content or etag in result
        if isinstance(result, bytes):
            etag = hashlib.md5(result).hexdigest()
            headers["ETag"] = f'"{etag}"'
            headers["Cache-Control"] = "private, max-age=60"
        elif isinstance(result, dict):
            if "etag" in result:
                headers["ETag"] = f'"{result["etag"]}"'
            elif "content" in result and isinstance(result["content"], bytes):
                etag = hashlib.md5(result["content"]).hexdigest()
                headers["ETag"] = f'"{etag}"'
            # If returning download_url, allow caching the URL itself
            if "download_url" in result:
                headers["Cache-Control"] = "private, max-age=300"
            else:
                headers["Cache-Control"] = "private, max-age=60"

    # List and glob operations - short cache
    elif method in ("list", "glob", "search"):
        headers["Cache-Control"] = "private, max-age=30"

    # Metadata operations - short cache
    elif method in ("get_metadata", "exists", "is_directory"):
        headers["Cache-Control"] = "private, max-age=60"

    # Write/delete operations - no cache
    elif method in ("write", "delete", "rename", "copy", "mkdir", "rmdir", "delta_write", "edit"):
        headers["Cache-Control"] = "no-store"

    # Delta read - cache like regular read
    elif method == "delta_read":
        headers["Cache-Control"] = "private, max-age=60"

    # Default for other methods - no cache
    else:
        headers["Cache-Control"] = "private, no-cache"

    return headers


def _error_response(
    request_id: Any,
    code: RPCErrorCode,
    message: str,
    data: dict[str, Any] | None = None,
) -> JSONResponse:
    """Create JSON-RPC error response."""
    # Build error response directly since RPCResponse.error is a classmethod
    error_dict = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code.value if hasattr(code, "value") else code,
            "message": message,
        },
    }
    if data:
        error_dict["error"]["data"] = data
    return JSONResponse(content=error_dict)


# Issue #1115: Event firing helper for RPC handlers
async def _fire_rpc_event(
    event_type: str,
    path: str,
    context: Any,
    old_path: str | None = None,
    size: int | None = None,
) -> None:
    """Fire an event after RPC mutation operation (non-blocking).

    This broadcasts events to webhook subscriptions for file operations
    performed via the RPC API (not FUSE).

    Args:
        event_type: Event type (file_write, file_delete, etc.)
        path: File/directory path
        context: Request context with zone info
        old_path: Old path for rename operations
        size: File size for write operations
    """
    if not _fastapi_app.state.subscription_manager:
        return

    try:
        zone_id = getattr(context, "zone_id", None) or "default"
        data: dict[str, Any] = {"file_path": path}
        if old_path:
            data["old_path"] = old_path
        if size is not None:
            data["size"] = size

        # Await broadcast to ensure webhook delivery before response
        # This adds slight latency but ensures reliable event delivery
        await _fastapi_app.state.subscription_manager.broadcast(event_type, data, zone_id)
    except Exception as e:
        logger.warning(f"[RPC] Failed to fire event {event_type} for {path}: {e}")


@dataclasses.dataclass(frozen=True, slots=True)
class _DispatchEntry:
    """Dispatch table entry for an RPC method.

    Attributes:
        handler: The handler callable (sync or async).
        is_async: If True, handler is awaited directly; otherwise wrapped
            with ``to_thread_with_timeout``.
        event_type: If set, fire this event type after handler completes.
        event_path_attr: Attribute name on ``params`` for the event path.
        event_old_path_attr: Attribute name on ``params`` for old_path (rename).
        event_size_key: Key in the result dict to extract size (write).
    """

    handler: Any  # Callable[[params, context], result]
    is_async: bool = False
    event_type: str | None = None
    event_path_attr: str = "path"
    event_old_path_attr: str | None = None
    event_size_key: str | None = None


# Lazily initialized — handler functions are defined later in this module.
_DISPATCH_TABLE: dict[str, _DispatchEntry] = {}


def _build_dispatch_table() -> dict[str, _DispatchEntry]:
    """Build the RPC dispatch table (Issue #1288).

    Called once on first RPC request. Entries with ``event_type`` fire
    subscription events after the handler completes.
    """
    return {
        # Core filesystem operations
        "read": _DispatchEntry(_handle_read_async, is_async=True),
        "write": _DispatchEntry(
            _handle_write, event_type="file_write", event_size_key="bytes_written"
        ),
        "exists": _DispatchEntry(_handle_exists),
        "list": _DispatchEntry(_handle_list),
        "delete": _DispatchEntry(_handle_delete, event_type="file_delete"),
        "rename": _DispatchEntry(
            _handle_rename,
            event_type="file_rename",
            event_path_attr="new_path",
            event_old_path_attr="old_path",
        ),
        "copy": _DispatchEntry(_handle_copy),
        "mkdir": _DispatchEntry(_handle_mkdir, event_type="dir_create"),
        "rmdir": _DispatchEntry(_handle_rmdir, event_type="dir_delete"),
        "get_metadata": _DispatchEntry(_handle_get_metadata),
        "glob": _DispatchEntry(_handle_glob),
        "grep": _DispatchEntry(_handle_grep),
        "search": _DispatchEntry(_handle_search),
        "is_directory": _DispatchEntry(_handle_is_directory),
        # Delta sync (Issue #869)
        "delta_read": _DispatchEntry(_handle_delta_read),
        "delta_write": _DispatchEntry(_handle_delta_write),
        # Semantic search (Issue #947)
        "semantic_search_index": _DispatchEntry(_handle_semantic_search_index, is_async=True),
        # Memory API (Issue #4)
        "store_memory": _DispatchEntry(_handle_store_memory),
        "list_memories": _DispatchEntry(_handle_list_memories),
        "query_memories": _DispatchEntry(_handle_query_memories),
        "retrieve_memory": _DispatchEntry(_handle_retrieve_memory),
        "delete_memory": _DispatchEntry(_handle_delete_memory),
        "approve_memory": _DispatchEntry(_handle_approve_memory),
        "deactivate_memory": _DispatchEntry(_handle_deactivate_memory),
        "approve_memory_batch": _DispatchEntry(_handle_approve_memory_batch),
        "deactivate_memory_batch": _DispatchEntry(_handle_deactivate_memory_batch),
        "delete_memory_batch": _DispatchEntry(_handle_delete_memory_batch),
        # Admin API (v0.5.1)
        "admin_create_key": _DispatchEntry(_handle_admin_create_key),
        "admin_list_keys": _DispatchEntry(_handle_admin_list_keys),
        "admin_get_key": _DispatchEntry(_handle_admin_get_key),
        "admin_revoke_key": _DispatchEntry(_handle_admin_revoke_key),
        "admin_update_key": _DispatchEntry(_handle_admin_update_key),
    }


async def _dispatch_method(method: str, params: Any, context: Any) -> Any:
    """Dispatch RPC method call.

    Looks up the method in ``_DISPATCH_TABLE`` first, then falls back to
    ``_auto_dispatch`` for dynamically exposed methods.
    """
    global _DISPATCH_TABLE  # noqa: PLW0603

    nexus_fs = _fastapi_app.state.nexus_fs
    if nexus_fs is None:
        raise RuntimeError("NexusFS not initialized")

    # Lazy-init on first call (handler functions defined later in module)
    if not _DISPATCH_TABLE:
        _DISPATCH_TABLE = _build_dispatch_table()

    # Issue #1457: Enforce admin_only for ALL dispatch paths (auto + manual)
    func = _fastapi_app.state.exposed_methods.get(method)
    if func and getattr(func, "_rpc_admin_only", False):
        _require_admin(context)

    # Auto-dispatch takes priority for dynamically exposed methods
    # that are NOT in the static dispatch table
    if method in _fastapi_app.state.exposed_methods and method not in _DISPATCH_TABLE:
        return await _auto_dispatch(method, params, context)

    entry = _DISPATCH_TABLE.get(method)
    if entry is not None:
        # Execute handler
        if entry.is_async:
            result = await entry.handler(params, context)
        else:
            result = await to_thread_with_timeout(entry.handler, params, context)

        # Fire subscription event for mutations (Issue #1115)
        if entry.event_type is not None:
            path = getattr(params, entry.event_path_attr, None)
            old_path = (
                getattr(params, entry.event_old_path_attr, None)
                if entry.event_old_path_attr
                else None
            )
            size = (
                result.get(entry.event_size_key)
                if entry.event_size_key and isinstance(result, dict)
                else None
            )
            await _fire_rpc_event(
                entry.event_type, path or "", context, old_path=old_path, size=size
            )

        return result

    # Fallback: try auto-dispatch for exposed methods
    if method in _fastapi_app.state.exposed_methods:
        return await _auto_dispatch(method, params, context)

    raise ValueError(f"Unknown method: {method}")


async def _auto_dispatch(method: str, params: Any, context: Any) -> Any:
    """Auto-dispatch to exposed method."""
    import inspect

    func = _fastapi_app.state.exposed_methods[method]

    # Build kwargs
    kwargs: dict[str, Any] = {}
    sig = inspect.signature(func)

    for param_name, _param in sig.parameters.items():
        if param_name == "self":
            continue
        # Support both "context" and "_context" parameter names.
        # Skills methods intentionally use "_context" to avoid shadowing/conflicts.
        elif param_name in ("context", "_context"):
            kwargs[param_name] = context
        elif hasattr(params, param_name):
            kwargs[param_name] = getattr(params, param_name)

    # Call function (handle both sync and async)
    if asyncio.iscoroutinefunction(func):
        return await func(**kwargs)
    else:
        # Run sync function in thread pool with timeout (Issue #932)
        # Use longer timeout for sync operations (5 minutes)
        timeout = 300.0 if method == "sync_mount" else None
        return await to_thread_with_timeout(func, timeout=timeout, **kwargs)


# ============================================================================
# Memory API Helper
# ============================================================================


def _get_memory_api_with_context(context: Any) -> Any:
    """Get Memory API instance with authenticated context.

    Args:
        context: Operation context with zone/user/agent info

    Returns:
        Memory API instance with user/agent/zone from context
    """
    nexus_fs = _fastapi_app.state.nexus_fs
    if nexus_fs is None:
        raise RuntimeError("NexusFS not initialized")

    # Convert context to dict format needed by _get_memory_api
    context_dict: dict[str, Any] = {}
    if context:
        if hasattr(context, "zone_id") and context.zone_id:
            context_dict["zone_id"] = context.zone_id
        if hasattr(context, "user_id") and context.user_id:
            context_dict["user_id"] = context.user_id
        elif hasattr(context, "user") and context.user:
            context_dict["user_id"] = context.user
        if hasattr(context, "agent_id") and context.agent_id:
            context_dict["agent_id"] = context.agent_id

    # _get_memory_api is available on NexusFS
    return nexus_fs._get_memory_api(context_dict if context_dict else None)


# ============================================================================
# Memory Method Handlers (Issue #4)
# ============================================================================


def _handle_store_memory(params: Any, context: Any) -> dict[str, Any]:
    """Handle store_memory RPC method."""
    memory_api = _get_memory_api_with_context(context)
    memory_id = memory_api.store(
        content=params.content,
        memory_type=params.memory_type,
        scope=params.scope,
        importance=params.importance,
        namespace=params.namespace,
        path_key=params.path_key,
        state=params.state,
    )
    return {"memory_id": memory_id}


def _handle_list_memories(params: Any, context: Any) -> dict[str, Any]:
    """Handle list_memories RPC method."""
    memory_api = _get_memory_api_with_context(context)
    memories = memory_api.list(
        scope=params.scope,
        memory_type=params.memory_type,
        namespace=params.namespace,
        namespace_prefix=params.namespace_prefix,
        state=params.state,
        limit=params.limit,
    )
    return {"memories": memories}


def _handle_query_memories(params: Any, context: Any) -> dict[str, Any]:
    """Handle query_memories RPC method."""
    memory_api = _get_memory_api_with_context(context)

    # Support semantic search if query is provided (#406)
    if params.query:
        # Create embedding provider if specified
        embedding_provider_obj = None
        if params.embedding_provider:
            try:
                from nexus.search.embeddings import create_embedding_provider

                embedding_provider_obj = create_embedding_provider(
                    provider=params.embedding_provider
                )
            except Exception:
                # Failed to create provider, will use default or fallback
                pass

        # Use search method with semantic search
        search_mode = params.search_mode or "hybrid"
        memories = memory_api.search(
            query=params.query,
            memory_type=params.memory_type,
            scope=params.scope,
            limit=params.limit,
            search_mode=search_mode,
            embedding_provider=embedding_provider_obj,
        )
    else:
        # Use regular query method
        memories = memory_api.query(
            memory_type=params.memory_type,
            scope=params.scope,
            state=params.state,
            limit=params.limit,
        )
    return {"memories": memories}


def _handle_retrieve_memory(params: Any, context: Any) -> dict[str, Any]:
    """Handle retrieve_memory RPC method."""
    memory_api = _get_memory_api_with_context(context)
    memory = memory_api.retrieve(
        namespace=params.namespace,
        path_key=params.path_key,
        path=params.path,
    )
    return {"memory": memory}


def _handle_delete_memory(params: Any, context: Any) -> dict[str, Any]:
    """Handle delete_memory RPC method."""
    memory_api = _get_memory_api_with_context(context)
    deleted = memory_api.delete(params.memory_id)
    return {"deleted": deleted}


def _handle_approve_memory(params: Any, context: Any) -> dict[str, Any]:
    """Handle approve_memory RPC method (#368)."""
    memory_api = _get_memory_api_with_context(context)
    approved = memory_api.approve(params.memory_id)
    return {"approved": approved}


def _handle_deactivate_memory(params: Any, context: Any) -> dict[str, Any]:
    """Handle deactivate_memory RPC method (#368)."""
    memory_api = _get_memory_api_with_context(context)
    deactivated = memory_api.deactivate(params.memory_id)
    return {"deactivated": deactivated}


def _handle_approve_memory_batch(params: Any, context: Any) -> dict[str, Any]:
    """Handle approve_memory_batch RPC method (#368)."""
    memory_api = _get_memory_api_with_context(context)
    result: dict[str, Any] = memory_api.approve_batch(params.memory_ids)
    return result


def _handle_deactivate_memory_batch(params: Any, context: Any) -> dict[str, Any]:
    """Handle deactivate_memory_batch RPC method (#368)."""
    memory_api = _get_memory_api_with_context(context)
    result: dict[str, Any] = memory_api.deactivate_batch(params.memory_ids)
    return result


def _handle_delete_memory_batch(params: Any, context: Any) -> dict[str, Any]:
    """Handle delete_memory_batch RPC method (#368)."""
    memory_api = _get_memory_api_with_context(context)
    result: dict[str, Any] = memory_api.delete_batch(params.memory_ids)
    return result


# ============================================================================
# Manual Method Handlers
# ============================================================================


def _generate_download_url(
    path: str, context: Any, expires_in: int = 3600
) -> dict[str, Any] | None:
    """Generate presigned/signed URL for direct download if backend supports it.

    This enables clients to download files directly from S3/GCS or via streaming
    from local backend, bypassing the Nexus server for improved performance on
    large files.

    Supported backends:
    - S3: Returns presigned URL for direct download from S3
    - GCS: Returns signed URL for direct download from GCS
    - Local: Returns streaming endpoint URL with signed token (Issue #853)

    Args:
        path: Virtual file path
        context: Operation context
        expires_in: URL expiration time in seconds

    Returns:
        Dict with download_url, expires_in, method, backend if supported, None otherwise
    """
    nexus_fs = _fastapi_app.state.nexus_fs
    if nexus_fs is None:
        return None

    try:
        # Get the backend for this path via router
        route = nexus_fs.router.route(path)
        backend = route.backend
        backend_path = route.backend_path

        # Check if backend supports presigned URLs
        # S3 connector
        if hasattr(backend, "generate_presigned_url"):
            # Update context with backend_path
            from dataclasses import replace

            if context and hasattr(context, "backend_path"):
                context = replace(context, backend_path=backend_path)
            result = backend.generate_presigned_url(backend_path, expires_in, context)
            return {
                "download_url": result["url"],
                "expires_in": result["expires_in"],
                "method": result["method"],
                "backend": "s3",
            }

        # GCS connector
        if hasattr(backend, "generate_signed_url"):
            # Update context with backend_path
            from dataclasses import replace

            if context and hasattr(context, "backend_path"):
                context = replace(context, backend_path=backend_path)
            result = backend.generate_signed_url(backend_path, expires_in, context)
            return {
                "download_url": result["url"],
                "expires_in": result["expires_in"],
                "method": result["method"],
                "backend": "gcs",
            }

        # Local backend - use streaming endpoint with signed token
        from nexus.backends.local import LocalBackend

        if isinstance(backend, LocalBackend) and hasattr(backend, "stream_content"):
            # Get zone_id from context
            zone_id = "default"
            if context and hasattr(context, "zone_id"):
                zone_id = context.zone_id or "default"

            # Generate signed token for streaming access
            token = _sign_stream_token(path, expires_in, zone_id)

            # URL-encode the path (remove leading slash for URL construction)
            from urllib.parse import quote

            encoded_path = quote(path.lstrip("/"), safe="")

            return {
                "download_url": f"/api/stream/{encoded_path}?token={token}&zone_id={zone_id}",
                "expires_in": expires_in,
                "method": "GET",
                "backend": "local",
            }

        # Backend doesn't support presigned URLs or streaming
        return None

    except Exception as e:
        logger.warning(f"Failed to generate download URL for {path}: {e}")
        return None


async def _handle_read_async(params: Any, context: Any) -> bytes | dict[str, Any]:
    """Handle read method (async version for parsed reads).

    Returns raw bytes which will be encoded by encode_rpc_message using
    the standard {__type__: 'bytes', data: ...} format.

    If return_url=True and the backend supports it (S3/GCS connectors),
    returns a presigned URL instead of file content for direct download.
    """
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    # Handle optional parameters
    return_metadata = getattr(params, "return_metadata", False) or False
    parsed = getattr(params, "parsed", False) or False
    return_url = getattr(params, "return_url", False) or False
    expires_in = getattr(params, "expires_in", 3600) or 3600

    # Handle return_url - generate presigned URL for direct download
    if return_url:
        result = await to_thread_with_timeout(
            _generate_download_url, params.path, context, expires_in
        )
        if result:
            return result
        # Fall through to normal read if URL generation not supported

    # If not parsed, use sync read in thread with timeout (Issue #932)
    if not parsed:
        read_result: bytes | dict[str, Any] = await to_thread_with_timeout(
            nexus_fs.read,
            params.path,
            context,
            return_metadata,
            False,
        )
        # Issue #1202: Strip internal prefixes from metadata path
        if isinstance(read_result, dict):
            read_result = unscope_internal_dict(read_result, ["path", "virtual_path"])
        return read_result

    # For parsed reads, we need to handle async parsing
    # First, read the raw content with timeout (Issue #932)
    raw_result = await to_thread_with_timeout(
        nexus_fs.read,
        params.path,
        context,
        True,
        False,  # return_metadata=True, parsed=False
    )

    content = raw_result.get("content", b"") if isinstance(raw_result, dict) else raw_result

    # Now parse the content asynchronously
    if hasattr(nexus_fs, "_get_parsed_content_async"):
        parsed_content, parse_info = await nexus_fs._get_parsed_content_async(params.path, content)
    else:
        # Fallback to sync method in thread with timeout (Issue #932)
        parsed_content, parse_info = await to_thread_with_timeout(
            nexus_fs._get_parsed_content, params.path, content
        )

    if return_metadata:
        result = {
            "content": parsed_content,
            "parsed": parse_info.get("parsed", False),
            "provider": parse_info.get("provider"),
            "cached": parse_info.get("cached", False),
        }
        if isinstance(raw_result, dict):
            result["etag"] = raw_result.get("etag")
            result["version"] = raw_result.get("version")
            result["modified_at"] = raw_result.get("modified_at")
            result["size"] = len(parsed_content)
        return result

    return parsed_content


def _handle_read(params: Any, context: Any) -> bytes | dict[str, Any]:
    """Handle read method (sync version - kept for compatibility).

    Returns raw bytes which will be encoded by encode_rpc_message using
    the standard {__type__: 'bytes', data: ...} format.
    """
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    # Handle optional parameters
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "return_metadata") and params.return_metadata is not None:
        kwargs["return_metadata"] = params.return_metadata
    if hasattr(params, "parsed") and params.parsed is not None:
        kwargs["parsed"] = params.parsed

    result = nexus_fs.read(params.path, **kwargs)

    # Return raw bytes - encode_rpc_message will convert to {__type__: 'bytes', data: ...}
    if isinstance(result, bytes):
        return result
    # Issue #1202: Strip internal prefixes from metadata path
    if isinstance(result, dict):
        result = unscope_internal_dict(result, ["path", "virtual_path"])
    return result


def _handle_write(params: Any, context: Any) -> dict[str, Any]:
    """Handle write method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    # Content should already be bytes after decode_rpc_message
    content = params.content
    if isinstance(content, str):
        content = content.encode("utf-8")

    # Handle optional parameters
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "if_match") and params.if_match:
        kwargs["if_match"] = params.if_match
    if hasattr(params, "if_none_match") and params.if_none_match:
        kwargs["if_none_match"] = params.if_none_match
    if hasattr(params, "force") and params.force:
        kwargs["force"] = params.force
    # Lock params (Issue #1143) — only forward when non-default
    lock_val = getattr(params, "lock", None)
    if lock_val:
        kwargs["lock"] = lock_val
    lock_timeout_val = getattr(params, "lock_timeout", None)
    if lock_timeout_val is not None and lock_timeout_val != 30.0:
        kwargs["lock_timeout"] = lock_timeout_val

    bytes_written = nexus_fs.write(params.path, content, **kwargs)
    return {"bytes_written": bytes_written}


def _handle_exists(params: Any, context: Any) -> dict[str, Any]:
    """Handle exists method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    return {"exists": nexus_fs.exists(params.path, context=context)}


def _handle_list(params: Any, context: Any) -> dict[str, Any]:
    """Handle list method with optional pagination support (Issue #937).

    Backward Compatibility:
    - If limit not provided: returns {"files": [...]} (legacy format)
    - If limit provided: returns {"files": [...], "next_cursor": ..., "has_more": ...}
    """
    import time as _time

    _handle_start = _time.time()

    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "show_parsed") and params.show_parsed is not None:
        kwargs["show_parsed"] = params.show_parsed
    if hasattr(params, "recursive") and params.recursive is not None:
        kwargs["recursive"] = params.recursive
    if hasattr(params, "details") and params.details is not None:
        kwargs["details"] = params.details

    # Check for pagination mode (Issue #937)
    # Only use pagination if client explicitly requests it
    limit = getattr(params, "limit", None)
    cursor = getattr(params, "cursor", None)

    if limit is not None:
        kwargs["limit"] = limit
    if cursor:
        kwargs["cursor"] = cursor

    _list_start = _time.time()
    result = nexus_fs.list(params.path, **kwargs)
    _list_elapsed = (_time.time() - _list_start) * 1000

    # Result is PaginatedResult when limit is provided
    if hasattr(result, "to_dict"):
        _build_start = _time.time()
        paginated = result.to_dict()
        # Issue #1202: Strip internal zone/tenant/user prefixes from paths
        items = [
            unscope_internal_dict(f, ["path", "virtual_path"])
            if isinstance(f, dict)
            else unscope_internal_path(f)
            for f in paginated["items"]
        ]
        response = {
            "files": items,
            "next_cursor": paginated["next_cursor"],
            "has_more": paginated["has_more"],
            "total_count": paginated.get("total_count"),
        }
        _build_elapsed = (_time.time() - _build_start) * 1000
        _total_elapsed = (_time.time() - _handle_start) * 1000
        logger.info(
            f"[HANDLE-LIST] path={params.path}, list={_list_elapsed:.1f}ms, "
            f"build={_build_elapsed:.1f}ms, total={_total_elapsed:.1f}ms, "
            f"files={len(items)}, has_more={paginated['has_more']}"
        )
        return response

    # Fallback for non-paginated result (shouldn't happen)
    _build_start = _time.time()
    raw_entries = result if isinstance(result, list) else []
    # Issue #1202: Strip internal zone/tenant/user prefixes from paths
    entries = [
        unscope_internal_dict(f, ["path", "virtual_path"])
        if isinstance(f, dict)
        else unscope_internal_path(f)
        for f in raw_entries
    ]
    response = {"files": entries, "has_more": False, "next_cursor": None}
    _build_elapsed = (_time.time() - _build_start) * 1000
    _total_elapsed = (_time.time() - _handle_start) * 1000
    logger.info(
        f"[HANDLE-LIST] path={params.path}, list={_list_elapsed:.1f}ms, "
        f"build={_build_elapsed:.1f}ms, total={_total_elapsed:.1f}ms, files={len(entries)}"
    )
    return response


def _handle_delete(params: Any, context: Any) -> dict[str, Any]:
    """Handle delete method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.delete supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        nexus_fs.delete(params.path, context=context)
    except TypeError:
        nexus_fs.delete(params.path)

    response: dict[str, Any] = {"deleted": True}
    return response


def _handle_rename(params: Any, context: Any) -> dict[str, Any]:
    """Handle rename method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.rename supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        nexus_fs.rename(params.old_path, params.new_path, context=context)
    except TypeError:
        nexus_fs.rename(params.old_path, params.new_path)

    response: dict[str, Any] = {"renamed": True}
    return response


def _handle_copy(params: Any, context: Any) -> dict[str, Any]:
    """Handle copy method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    nexus_fs.copy(params.src_path, params.dst_path, context=context)  # type: ignore[attr-defined]
    return {"copied": True}


def _handle_mkdir(params: Any, context: Any) -> dict[str, Any]:
    """Handle mkdir method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "parents") and params.parents is not None:
        kwargs["parents"] = params.parents
    if hasattr(params, "exist_ok") and params.exist_ok is not None:
        kwargs["exist_ok"] = params.exist_ok

    nexus_fs.mkdir(params.path, **kwargs)
    return {"created": True}


def _handle_rmdir(params: Any, context: Any) -> dict[str, Any]:
    """Handle rmdir method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "recursive") and params.recursive is not None:
        kwargs["recursive"] = params.recursive
    if hasattr(params, "force") and params.force is not None:
        kwargs["force"] = params.force

    nexus_fs.rmdir(params.path, **kwargs)
    return {"removed": True}


def _handle_get_metadata(params: Any, context: Any) -> dict[str, Any]:
    """Handle get_metadata method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    metadata = nexus_fs.get_metadata(params.path, context=context)
    # Issue #1202: Strip internal zone/tenant/user prefixes from metadata path
    if isinstance(metadata, dict):
        metadata = unscope_internal_dict(metadata, ["path"])
    return {"metadata": metadata}


def _handle_glob(params: Any, context: Any) -> dict[str, Any]:
    """Handle glob method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path

    matches = nexus_fs.glob(params.pattern, **kwargs)
    # Issue #1202: Strip internal zone/tenant/user prefixes from paths
    matches = [unscope_internal_path(m) if isinstance(m, str) else m for m in matches]
    return {"matches": matches}


def _handle_grep(params: Any, context: Any) -> dict[str, Any]:
    """Handle grep method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "ignore_case") and params.ignore_case is not None:
        kwargs["ignore_case"] = params.ignore_case
    if hasattr(params, "max_results") and params.max_results is not None:
        kwargs["max_results"] = params.max_results
    if hasattr(params, "file_pattern") and params.file_pattern is not None:
        kwargs["file_pattern"] = params.file_pattern
    if hasattr(params, "search_mode") and params.search_mode is not None:
        kwargs["search_mode"] = params.search_mode

    results = nexus_fs.grep(params.pattern, **kwargs)
    # Issue #1202: Strip internal zone/tenant/user prefixes from paths
    results = [unscope_result(r) for r in results]
    # Return "results" key to match RemoteNexusFS.grep() expectations
    return {"results": results}


def _handle_search(params: Any, context: Any) -> dict[str, Any]:
    """Handle search method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path
    if hasattr(params, "limit") and params.limit is not None:
        kwargs["limit"] = params.limit
    if hasattr(params, "search_type") and params.search_type:
        kwargs["search_type"] = params.search_type

    results = nexus_fs.search(params.query, **kwargs)  # type: ignore[attr-defined]
    return {"results": results}


async def _handle_semantic_search_index(params: Any, _context: Any) -> dict[str, Any]:
    """Handle semantic_search_index method (Issue #947).

    Index documents for semantic search with embeddings.

    Args:
        params.path: Path to index (file or directory, default: "/")
        params.recursive: If True, index directory recursively (default: True)
        context: Operation context

    Returns:
        Dictionary mapping file paths to number of chunks indexed
    """
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    path = getattr(params, "path", "/")
    recursive = getattr(params, "recursive", True)

    # Check if semantic search is initialized
    if not hasattr(nexus_fs, "_semantic_search") or nexus_fs._semantic_search is None:
        # Try to initialize semantic search
        try:
            await nexus_fs.initialize_semantic_search()
        except Exception as e:
            raise ValueError(
                f"Semantic search is not initialized and could not be auto-initialized: {e}"
            ) from e

    # Call the async indexing method
    results = await nexus_fs.semantic_search_index(path=path, recursive=recursive)

    # Calculate total chunks (handle case where values might be dicts or errors)
    total_chunks = 0
    for v in results.values():
        if isinstance(v, int):
            total_chunks += v
        elif isinstance(v, dict) and "chunks" in v:
            total_chunks += v["chunks"]

    return {"indexed": results, "total_files": len(results), "total_chunks": total_chunks}


def _handle_is_directory(params: Any, context: Any) -> dict[str, Any]:
    """Handle is_directory method."""
    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None
    return {"is_directory": nexus_fs.is_directory(params.path, context=context)}


# ============================================================================
# Delta Sync Handlers (Issue #869)
# ============================================================================


def _handle_delta_read(params: Any, context: Any) -> dict[str, Any]:
    """Handle delta_read method for rsync-style incremental updates.

    If client provides a content hash matching their cached version,
    returns only the delta (binary diff) instead of full file content.
    This reduces bandwidth by 50-90% for files with small changes.

    Args:
        params.path: File path to read
        params.client_hash: Client's current content hash (optional)
        params.max_delta_ratio: Max delta/original size ratio before falling back (default: 0.8)

    Returns:
        - If client_hash matches server: {"unchanged": True, "server_hash": ...}
        - If delta is smaller than threshold: {"delta": bytes, "server_hash": ..., "is_full": False}
        - If delta is larger or no client_hash: {"content": bytes, "server_hash": ..., "is_full": True}
    """
    import bsdiff4

    from nexus.core.hash_fast import hash_content

    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    # Read current file content
    content = nexus_fs.read(params.path, context=context)
    if isinstance(content, dict):
        content = content.get("content", b"")
    if isinstance(content, str):
        content = content.encode("utf-8")
    assert isinstance(content, bytes)

    # Compute server's content hash
    server_hash = hash_content(content)

    # Get client's hash if provided
    client_hash = getattr(params, "client_hash", None)
    max_delta_ratio = getattr(params, "max_delta_ratio", 0.8)

    # If no client hash or client hash matches, handle appropriately
    if client_hash is None:
        # No client cache - return full content
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
        }

    if client_hash == server_hash:
        # Content unchanged - client can use cached version
        return {
            "unchanged": True,
            "server_hash": server_hash,
        }

    # Client has different version - need to compute delta
    # Get client's cached content from their provided hash
    # Note: Client must send their cached content for delta computation
    client_content = getattr(params, "client_content", None)

    if client_content is None:
        # Client didn't send their content - can't compute delta, return full
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "client_content_required",
        }

    if isinstance(client_content, str):
        client_content = client_content.encode("utf-8")

    # Verify client's content matches their claimed hash
    if hash_content(client_content) != client_hash:
        # Hash mismatch - return full content
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "client_hash_mismatch",
        }

    # Compute binary delta using bsdiff4
    delta = bsdiff4.diff(client_content, content)

    # Check if delta is worth sending (smaller than threshold)
    delta_ratio = len(delta) / len(content) if len(content) > 0 else 1.0

    if delta_ratio > max_delta_ratio:
        # Delta too large - send full content instead
        return {
            "content": content,
            "server_hash": server_hash,
            "is_full": True,
            "size": len(content),
            "reason": "delta_too_large",
            "delta_ratio": delta_ratio,
        }

    # Delta is efficient - return it
    return {
        "delta": delta,
        "server_hash": server_hash,
        "is_full": False,
        "delta_size": len(delta),
        "original_size": len(content),
        "compression_ratio": 1.0 - delta_ratio,
    }


def _handle_delta_write(params: Any, context: Any) -> dict[str, Any]:
    """Handle delta_write method for rsync-style incremental updates.

    Client sends a binary delta patch instead of full file content.
    Server applies the patch to the current file version.

    Args:
        params.path: File path to write
        params.delta: Binary delta patch (bsdiff4 format)
        params.base_hash: Expected hash of current server content
        params.if_match: Optional ETag for optimistic concurrency

    Returns:
        {"bytes_written": int, "new_hash": str} on success
        {"error": str, "reason": str} on conflict
    """
    import bsdiff4

    from nexus.core.hash_fast import hash_content

    nexus_fs = _fastapi_app.state.nexus_fs
    assert nexus_fs is not None

    # Get the delta and base hash
    delta = params.delta
    if isinstance(delta, str):
        delta = delta.encode("latin-1")  # Binary data might be encoded

    base_hash = getattr(params, "base_hash", None)
    if base_hash is None:
        raise ValueError("base_hash is required for delta_write")

    # Read current file content
    try:
        current_content = nexus_fs.read(params.path, context=context)
        if isinstance(current_content, dict):
            current_content = current_content.get("content", b"")
        if isinstance(current_content, str):
            current_content = current_content.encode("utf-8")
        assert isinstance(current_content, bytes)
    except Exception as e:
        # File doesn't exist - can't apply delta
        raise ValueError("Cannot apply delta to non-existent file. Use write() instead.") from e

    # Verify current content matches expected base
    current_hash = hash_content(current_content)
    if current_hash != base_hash:
        return {
            "error": "conflict",
            "reason": "base_hash_mismatch",
            "expected_hash": base_hash,
            "actual_hash": current_hash,
        }

    # Apply the delta patch
    new_content = bsdiff4.patch(current_content, delta)

    # Write the patched content
    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "if_match") and params.if_match:
        kwargs["if_match"] = params.if_match

    bytes_written = nexus_fs.write(params.path, new_content, **kwargs)
    new_hash = hash_content(new_content)

    return {
        "bytes_written": bytes_written,
        "new_hash": new_hash,
        "patch_applied": True,
    }


# ============================================================================
# Admin API Handlers (v0.5.1)
# ============================================================================


def _require_admin(context: Any) -> None:
    """Require admin privileges for admin operations."""
    from nexus.core.exceptions import NexusPermissionError

    if not context or not getattr(context, "is_admin", False):
        raise NexusPermissionError("Admin privileges required for this operation")


def _handle_admin_create_key(params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_create_key method."""
    import uuid
    from datetime import timedelta

    from nexus.server.auth.database_key import DatabaseAPIKeyAuth
    from nexus.services.permissions.entity_registry import EntityRegistry

    _require_admin(context)

    auth_provider = _fastapi_app.state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    # Auto-generate user_id if not provided
    user_id = params.user_id
    if not user_id:
        user_id = f"user_{uuid.uuid4().hex[:12]}"

    # Register user in entity registry (for agent permission inheritance)
    if params.subject_type == "user" or not params.subject_type:
        entity_registry = EntityRegistry(auth_provider.session_factory)
        entity_registry.register_entity(
            entity_type="user",
            entity_id=user_id,
            parent_type="zone",
            parent_id=params.zone_id,
        )

    # Calculate expiry if specified
    expires_at = None
    if params.expires_days:
        expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

    # Create API key
    with auth_provider.session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id=user_id,
            name=params.name,
            subject_type=params.subject_type,
            subject_id=params.subject_id,
            zone_id=params.zone_id,
            is_admin=params.is_admin,
            expires_at=expires_at,
        )
        session.commit()

        return {
            "key_id": key_id,
            "api_key": raw_key,
            "user_id": user_id,
            "name": params.name,
            "subject_type": params.subject_type,
            "subject_id": params.subject_id or user_id,
            "zone_id": params.zone_id,
            "is_admin": params.is_admin,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }


def _handle_admin_list_keys(params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_list_keys method.

    Performance optimized: All filtering happens in SQL instead of Python.
    """

    from sqlalchemy import func, or_, select

    from nexus.storage.models import APIKeyModel

    _require_admin(context)

    auth_provider = _fastapi_app.state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel)

        # Apply all filters in SQL for performance
        if params.user_id:
            stmt = stmt.where(APIKeyModel.user_id == params.user_id)
        if params.zone_id:
            stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)
        if params.is_admin is not None:
            stmt = stmt.where(APIKeyModel.is_admin == int(params.is_admin))
        if not params.include_revoked:
            stmt = stmt.where(APIKeyModel.revoked == 0)

        # Filter expired keys in SQL (not Python) for correct pagination
        if not params.include_expired:
            now = datetime.now(UTC)
            stmt = stmt.where(
                or_(
                    APIKeyModel.expires_at.is_(None),
                    APIKeyModel.expires_at > now,
                )
            )

        # Get total count before pagination (for accurate total)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = session.scalar(count_stmt) or 0

        # Apply pagination
        stmt = stmt.order_by(APIKeyModel.created_at.desc())
        stmt = stmt.limit(params.limit).offset(params.offset)
        api_keys = list(session.scalars(stmt).all())

        keys = []
        for key in api_keys:
            keys.append(
                {
                    "key_id": key.key_id,
                    "user_id": key.user_id,
                    "subject_type": key.subject_type,
                    "subject_id": key.subject_id,
                    "name": key.name,
                    "zone_id": key.zone_id,
                    "is_admin": bool(key.is_admin),
                    "created_at": key.created_at.isoformat() if key.created_at else None,
                    "expires_at": key.expires_at.isoformat() if key.expires_at else None,
                    "revoked": bool(key.revoked),
                    "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
                    "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
                }
            )

        return {"keys": keys, "total": total}


def _handle_admin_get_key(params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_get_key method."""
    from sqlalchemy import select

    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    _require_admin(context)

    auth_provider = _fastapi_app.state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        return {
            "key_id": api_key.key_id,
            "user_id": api_key.user_id,
            "subject_type": api_key.subject_type,
            "subject_id": api_key.subject_id,
            "name": api_key.name,
            "zone_id": api_key.zone_id,
            "is_admin": bool(api_key.is_admin),
            "created_at": api_key.created_at.isoformat() if api_key.created_at else None,
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
            "revoked": bool(api_key.revoked),
            "revoked_at": api_key.revoked_at.isoformat() if api_key.revoked_at else None,
            "last_used_at": api_key.last_used_at.isoformat() if api_key.last_used_at else None,
        }


def _handle_admin_revoke_key(params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_revoke_key method."""
    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    _require_admin(context)

    auth_provider = _fastapi_app.state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        success = DatabaseAPIKeyAuth.revoke_key(session, params.key_id)
        if not success:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        session.commit()
        return {"success": True, "key_id": params.key_id}


def _handle_admin_update_key(params: Any, context: Any) -> dict[str, Any]:
    """Handle admin_update_key method."""
    from datetime import timedelta

    from sqlalchemy import select

    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    _require_admin(context)

    auth_provider = _fastapi_app.state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    with auth_provider.session_factory() as session:
        stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
        api_key = session.scalar(stmt)

        if not api_key:
            raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

        # Update fields if provided
        if params.name is not None:
            api_key.name = params.name
        if params.is_admin is not None:
            api_key.is_admin = int(params.is_admin)
        if params.expires_days is not None:
            api_key.expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

        session.commit()

        return {
            "success": True,
            "key_id": api_key.key_id,
            "name": api_key.name,
            "is_admin": bool(api_key.is_admin),
            "expires_at": api_key.expires_at.isoformat() if api_key.expires_at else None,
        }


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
