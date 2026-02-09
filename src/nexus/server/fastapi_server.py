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
import hashlib
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from anyio import to_thread
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi import (
    status as http_status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.gzip import GZipMiddleware

from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)

if TYPE_CHECKING:
    from nexus.core.async_nexus_fs import AsyncNexusFS
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
# Lock API Models (Issue #1186)
# ============================================================================


class LockAcquireRequest(BaseModel):
    """Request model for acquiring a lock."""

    path: str
    timeout: float = 30.0  # Max time to wait for lock acquisition
    ttl: float = 30.0  # Lock TTL (auto-expires after this)
    max_holders: int = 1  # 1 = mutex, >1 = semaphore
    blocking: bool = True  # If false, return immediately without waiting


class LockResponse(BaseModel):
    """Response model for lock operations."""

    lock_id: str
    path: str
    mode: Literal["mutex", "semaphore"]
    max_holders: int
    ttl: int
    expires_at: str  # ISO 8601 timestamp


class LockStatusResponse(BaseModel):
    """Response model for lock status queries."""

    path: str
    locked: bool
    lock_info: dict[str, Any] | None = None


class LockExtendRequest(BaseModel):
    """Request model for extending a lock."""

    lock_id: str
    ttl: float = 30.0


class LockReleaseRequest(BaseModel):
    """Request model for releasing a lock."""

    lock_id: str
    force: bool = False  # Admin-only: force release regardless of owner


class LockListResponse(BaseModel):
    """Response model for listing locks."""

    locks: list[dict[str, Any]]
    count: int


# ============================================================================
# Rate Limiting Configuration (Issue #780)
# ============================================================================

# Rate limit tiers (configurable via environment variables)
RATE_LIMIT_ANONYMOUS = os.environ.get("NEXUS_RATE_LIMIT_ANONYMOUS", "60/minute")
RATE_LIMIT_AUTHENTICATED = os.environ.get("NEXUS_RATE_LIMIT_AUTHENTICATED", "300/minute")
RATE_LIMIT_PREMIUM = os.environ.get("NEXUS_RATE_LIMIT_PREMIUM", "1000/minute")


def _get_rate_limit_key(request: Request) -> str:
    """Extract rate limit key from request.

    Priority:
    1. Authenticated user from Bearer token (parsed from sk- format)
    2. Agent ID from header
    3. IP address for anonymous requests
    """
    # Try to extract identity from Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        # Parse sk-<zone>_<user>_<id>_<random> format
        if token.startswith("sk-"):
            parts = token[3:].split("_")
            if len(parts) >= 2:
                zone = parts[0] or "default"
                user = parts[1] or "unknown"
                return f"user:{zone}:{user}"
        # For other tokens, use hash as key
        return f"token:{hashlib.sha256(token.encode()).hexdigest()[:16]}"

    # Check for agent ID header
    agent_id = request.headers.get("X-Agent-ID")
    if agent_id:
        return f"agent:{agent_id}"

    # Fall back to IP address
    return str(get_remote_address(request))


def _rate_limit_exceeded_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Custom handler for rate limit exceeded errors."""
    detail = getattr(exc, "detail", str(exc))
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": str(detail),
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


def _nexus_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Custom handler for Nexus exceptions.

    Includes is_expected flag for error classification:
    - Expected errors: User errors (validation, not found, permission denied)
    - Unexpected errors: System errors (backend failures, bugs)
    """
    from nexus.core.exceptions import (
        AuthenticationError,
        BackendError,
        ConflictError,
        InvalidPathError,
        NexusError,
        NexusFileNotFoundError,
        NexusPermissionError,
        ParserError,
        PermissionDeniedError,
        ValidationError,
    )

    # Determine HTTP status code and error type based on exception
    if isinstance(exc, NexusFileNotFoundError):
        status_code = 404
        error_type = "Not Found"
    elif isinstance(exc, (NexusPermissionError, PermissionDeniedError)):
        status_code = 403
        error_type = "Forbidden"
    elif isinstance(exc, AuthenticationError):
        status_code = 401
        error_type = "Unauthorized"
    elif isinstance(exc, (InvalidPathError, ValidationError)):
        status_code = 400
        error_type = "Bad Request"
    elif isinstance(exc, ConflictError):
        status_code = 409
        error_type = "Conflict"
    elif isinstance(exc, ParserError):
        status_code = 422
        error_type = "Unprocessable Entity"
    elif isinstance(exc, BackendError):
        status_code = 502
        error_type = "Bad Gateway"
    elif isinstance(exc, NexusError):
        status_code = 500
        error_type = "Internal Server Error"
    else:
        status_code = 500
        error_type = "Internal Server Error"

    is_expected = getattr(exc, "is_expected", False)
    path = getattr(exc, "path", None)

    content: dict[str, Any] = {
        "error": error_type,
        "detail": str(exc),
        "is_expected": is_expected,
    }
    if path:
        content["path"] = path

    # Add conflict-specific data
    if isinstance(exc, ConflictError):
        content["expected_etag"] = exc.expected_etag
        content["current_etag"] = exc.current_etag

    return JSONResponse(status_code=status_code, content=content)


# Global limiter instance (initialized in create_app)
# Note: This is set before routes are registered, so it's never None when decorators run
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
        timeout: Timeout in seconds (uses _app_state.operation_timeout if None)
        **kwargs: Keyword arguments for func

    Returns:
        Result from func

    Raises:
        TimeoutError: If operation exceeds timeout
    """
    effective_timeout = timeout if timeout is not None else _app_state.operation_timeout
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(func, *args, **kwargs),
            timeout=effective_timeout,
        )
    except TimeoutError:
        raise TimeoutError(f"Operation timed out after {effective_timeout}s") from None


# ============================================================================
# Application State
# ============================================================================


class AppState:
    """Application state container."""

    def __init__(self) -> None:
        self.nexus_fs: NexusFS | None = None
        self.async_nexus_fs: AsyncNexusFS | None = None  # Issue #940: Native async fs
        self.auth_provider: Any = None
        self.api_key: str | None = None
        self.exposed_methods: dict[str, Any] = {}
        self.async_rebac_manager: Any = None
        self.database_url: str | None = None
        self.subscription_manager: Any = None  # SubscriptionManager for webhooks
        # Thread pool and timeout settings (Issue #932)
        self.thread_pool_size: int = 200
        self.operation_timeout: float = 30.0
        # Hot Search Daemon (Issue #951)
        self.search_daemon: Any = None
        self.search_daemon_enabled: bool = False
        # Directory Grant Expander for large folder grants (Leopard-style)
        self.directory_grant_expander: Any = None
        # Cache factory for Dragonfly/Redis (Issue #1075)
        self.cache_factory: Any = None
        # WebSocket Manager for real-time events (Issue #1116)
        self.websocket_manager: Any = None


# Global state (set during app creation)
_app_state = AppState()


# ============================================================================
# Stream Token Signing (for local backend streaming URLs)
# ============================================================================

# Secret key for signing stream tokens (persistent across restarts if set via env)
_STREAM_SECRET: bytes | None = None


def _get_stream_secret() -> bytes:
    """Get or generate the stream token signing secret."""
    global _STREAM_SECRET
    if _STREAM_SECRET is None:
        env_secret = os.environ.get("NEXUS_STREAM_SECRET")
        # Use env var if set, otherwise generate random secret (changes on restart)
        _STREAM_SECRET = env_secret.encode() if env_secret else secrets.token_bytes(32)
    return _STREAM_SECRET


def _sign_stream_token(path: str, expires_in: int, zone_id: str = "default") -> str:
    """Generate a signed token for streaming access to a file.

    Token format: {expires_at}.{signature}
    Where signature = HMAC-SHA256(path:expires_at:zone_id)[:16]

    Args:
        path: Virtual file path
        expires_in: Token validity in seconds
        zone_id: Zone ID for isolation

    Returns:
        Signed token string
    """
    expires_at = int(time.time()) + expires_in
    payload = f"{path}:{expires_at}:{zone_id}"
    signature = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]
    return f"{expires_at}.{signature}"


def _verify_stream_token(token: str, path: str, zone_id: str = "default") -> bool:
    """Verify a stream token is valid and not expired.

    Args:
        token: Token string from _sign_stream_token
        path: Virtual file path (must match token)
        zone_id: Zone ID (must match token)

    Returns:
        True if token is valid, False otherwise
    """
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return False

        expires_at_str, signature = parts
        expires_at = int(expires_at_str)

        # Check expiration
        if expires_at < time.time():
            return False

        # Verify signature
        payload = f"{path}:{expires_at}:{zone_id}"
        expected_sig = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]

        return hmac.compare_digest(signature, expected_sig)
    except (ValueError, TypeError):
        return False


# ============================================================================
# Dependencies
# ============================================================================

# Auth cache: token_hash -> (result, expiry_time)
# TTL: 15 minutes (900 seconds) - balances performance vs permission freshness
_AUTH_CACHE: dict[str, tuple[dict[str, Any], float]] = {}
_AUTH_CACHE_TTL = 900  # 15 minutes in seconds
_AUTH_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth


def _get_cached_auth(token: str) -> dict[str, Any] | None:
    """Get cached auth result if valid."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
    cached = _AUTH_CACHE.get(token_hash)
    if cached:
        result, expiry = cached
        if time.time() < expiry:
            return result
        # Expired, remove from cache
        _AUTH_CACHE.pop(token_hash, None)
    return None


def _set_cached_auth(token: str, result: dict[str, Any]) -> None:
    """Cache auth result with TTL."""
    # Simple size limit: remove oldest if too large
    if len(_AUTH_CACHE) >= _AUTH_CACHE_MAX_SIZE:
        # Remove ~10% of entries (oldest first by expiry)
        to_remove = sorted(_AUTH_CACHE.items(), key=lambda x: x[1][1])[: _AUTH_CACHE_MAX_SIZE // 10]
        for key, _ in to_remove:
            _AUTH_CACHE.pop(key, None)

    token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
    _AUTH_CACHE[token_hash] = (result, time.time() + _AUTH_CACHE_TTL)


async def get_auth_result(
    authorization: str | None = Header(None, alias="Authorization"),
    x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
    x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
) -> dict[str, Any] | None:
    """Validate authentication and return auth result.

    Note: Timing added for performance debugging (Issue #perf19).

    Args:
        authorization: Bearer token from Authorization header
        x_agent_id: Optional agent ID header
        x_nexus_subject: Optional identity hint header (e.g., "user:alice")
        x_nexus_zone_id: Optional zone hint header

    Returns:
        Auth result dict or None if not authenticated
    """

    def _parse_subject_header(value: str) -> tuple[str | None, str | None]:
        parts = value.split(":", 1)
        if len(parts) != 2:
            return (None, None)
        subject_type, subject_id = parts[0].strip(), parts[1].strip()
        if not subject_type or not subject_id:
            return (None, None)
        return (subject_type, subject_id)

    # No auth configured = open access
    if not _app_state.api_key and not _app_state.auth_provider:
        # In open access mode, we still want a stable identity for permission checks.
        # Prefer explicit identity headers; otherwise, best-effort infer from sk- style keys.
        subject_type: str | None = None
        subject_id: str | None = None
        zone_id: str | None = x_nexus_zone_id

        if x_nexus_subject:
            st, sid = _parse_subject_header(x_nexus_subject)
            subject_type, subject_id = st, sid
        elif authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            # Best-effort: infer zone/user from DatabaseAPIKeyAuth format
            # Format: sk-<zone>_<user>_<id>_<random-hex>
            if token.startswith("sk-"):
                remainder = token[len("sk-") :]
                parts = remainder.split("_")
                if len(parts) >= 2:
                    inferred_zone = parts[0] or None
                    inferred_user = parts[1] or None
                    zone_id = zone_id or inferred_zone
                    subject_type = "user"
                    subject_id = inferred_user

        return {
            "authenticated": True,
            "is_admin": False,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "zone_id": zone_id,
            "inherit_permissions": True,  # Open access mode always inherits
            "metadata": {"open_access": True},
            "x_agent_id": x_agent_id,
        }

    if not authorization:
        return None

    if not authorization.startswith("Bearer "):
        return None

    token = authorization[7:]

    # Try auth provider first
    if _app_state.auth_provider:
        import time as _time

        # Check cache first (15 min TTL)
        cached_result = _get_cached_auth(token)
        if cached_result:
            # Update x_agent_id and timing for this request
            cached_result["x_agent_id"] = x_agent_id
            cached_result["_auth_time_ms"] = 0.0  # Cache hit = no auth time
            cached_result["_auth_cached"] = True
            return cached_result

        # Cache miss - call provider
        _auth_start = _time.time()
        result = await _app_state.auth_provider.authenticate(token)
        _auth_elapsed = (_time.time() - _auth_start) * 1000
        if _auth_elapsed > 10:  # Log if auth takes >10ms
            logger.info(f"[AUTH-TIMING] provider auth took {_auth_elapsed:.1f}ms (cache miss)")
        if result is None:
            return None
        auth_result = {
            "authenticated": result.authenticated,
            "is_admin": result.is_admin,
            "subject_type": result.subject_type,
            "subject_id": result.subject_id,
            "zone_id": result.zone_id,
            "inherit_permissions": result.inherit_permissions
            if hasattr(result, "inherit_permissions")
            else True,
            "metadata": result.metadata if hasattr(result, "metadata") else {},
            "x_agent_id": x_agent_id,
            "_auth_time_ms": _auth_elapsed,  # Pass to RPC for logging
            "_auth_cached": False,
        }
        # Cache successful auth result
        _set_cached_auth(token, auth_result.copy())
        return auth_result

    # Fall back to static API key
    if _app_state.api_key:
        if token == _app_state.api_key:
            return {
                "authenticated": True,
                "is_admin": True,
                "subject_type": "user",
                "subject_id": "admin",
                "inherit_permissions": True,  # Static admin key always inherits
            }
        return None

    return None


async def require_auth(
    auth_result: dict[str, Any] | None = Depends(get_auth_result),
) -> dict[str, Any]:
    """Require authentication for endpoint.

    Raises:
        HTTPException: If not authenticated
    """
    if auth_result is None or not auth_result.get("authenticated"):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")
    return auth_result


def get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Create OperationContext from auth result.

    Args:
        auth_result: Authentication result dict

    Returns:
        OperationContext for filesystem operations
    """
    from nexus.core.permissions import OperationContext

    subject_type = auth_result.get("subject_type") or "user"
    subject_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or "default"
    is_admin = auth_result.get("is_admin", False)
    agent_id = auth_result.get("x_agent_id")
    user_id = subject_id

    # Handle agent authentication
    if subject_type == "agent":
        agent_id = subject_id
        metadata = auth_result.get("metadata", {})
        user_id = metadata.get("legacy_user_id", subject_id)

    # Handle X-Agent-ID header
    if agent_id and subject_type == "user":
        subject_type = "agent"
        subject_id = agent_id

    # Admin capabilities
    admin_capabilities = set()
    if is_admin:
        from nexus.core.permissions_enhanced import AdminCapability

        admin_capabilities = {
            AdminCapability.READ_ALL,
            AdminCapability.WRITE_ALL,
            AdminCapability.DELETE_ANY,
            AdminCapability.MANAGE_REBAC,
        }

    return OperationContext(
        user=user_id,
        agent_id=agent_id,
        subject_type=subject_type,
        subject_id=subject_id,
        zone_id=zone_id,
        is_admin=is_admin,
        groups=[],
        admin_capabilities=admin_capabilities,
    )


# ============================================================================
# Lifespan Management
# ============================================================================


@asynccontextmanager
async def lifespan(_app: FastAPI) -> Any:
    """Application lifespan manager.

    Handles startup and shutdown of async resources.
    """
    logger.info("Starting FastAPI Nexus server...")

    # Initialize OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import setup_telemetry

        setup_telemetry()
    except ImportError:
        logger.debug("OpenTelemetry not available")

    # Configure thread pool size (Issue #932)
    # Increase from default 40 to prevent thread pool exhaustion under load
    limiter = to_thread.current_default_thread_limiter()
    limiter.total_tokens = _app_state.thread_pool_size
    logger.info(f"Thread pool size set to {limiter.total_tokens}")

    # Initialize async ReBAC manager if database URL provided
    if _app_state.database_url:
        try:
            from nexus.core.async_rebac_manager import (
                AsyncReBACManager,
                create_async_engine_from_url,
            )

            engine = create_async_engine_from_url(_app_state.database_url)
            _app_state.async_rebac_manager = AsyncReBACManager(engine)
            logger.info("Async ReBAC manager initialized")

            # Issue #940: Initialize AsyncNexusFS with permission enforcement
            try:
                from nexus.core.async_nexus_fs import AsyncNexusFS
                from nexus.core.async_permissions import AsyncPermissionEnforcer

                backend_root = os.getenv("NEXUS_BACKEND_ROOT", ".nexus-data/backend")
                tenant_id = os.getenv("NEXUS_TENANT_ID", "default")
                enforce_permissions = os.getenv("NEXUS_ENFORCE_PERMISSIONS", "true").lower() in (
                    "true",
                    "1",
                    "yes",
                )

                # Issue #1239: Create namespace manager for per-subject visibility
                # NamespaceManager uses sync rebac_manager from nexus_fs for mount table queries
                namespace_manager = None
                if enforce_permissions and hasattr(_app_state, "nexus_fs"):
                    sync_rebac = getattr(_app_state.nexus_fs, "_rebac_manager", None)
                    if sync_rebac:
                        from nexus.core.namespace_manager import NamespaceManager

                        namespace_manager = NamespaceManager(
                            rebac_manager=sync_rebac,
                            cache_maxsize=10_000,
                            cache_ttl=300,
                            revision_window=10,
                        )
                        logger.info(
                            "[NAMESPACE] NamespaceManager initialized for AsyncPermissionEnforcer "
                            "(using sync rebac_manager for mount table queries)"
                        )

                # Create permission enforcer with async ReBAC
                permission_enforcer = AsyncPermissionEnforcer(
                    rebac_manager=_app_state.async_rebac_manager,
                    namespace_manager=namespace_manager,
                )

                # Create AsyncNexusFS
                _app_state.async_nexus_fs = AsyncNexusFS(
                    backend_root=backend_root,
                    engine=engine,
                    tenant_id=tenant_id,
                    enforce_permissions=enforce_permissions,
                    permission_enforcer=permission_enforcer,
                )
                await _app_state.async_nexus_fs.initialize()
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

        # Get PostgreSQL engine for cache fallback when Dragonfly is not available
        pg_engine = getattr(
            getattr(_app_state.nexus_fs, "_rebac_manager", None),
            "engine",
            None,
        )

        _app_state.cache_factory = await init_cache_factory(
            cache_settings, postgres_engine=pg_engine
        )
        logger.info(
            f"Cache factory initialized with {_app_state.cache_factory.backend_name} backend"
        )

        # Wire up Dragonfly L2 cache to TigerCache (Issue #1106)
        # This enables L1 (memory) -> L2 (Dragonfly) -> L3 (PostgreSQL) caching
        if _app_state.cache_factory.is_using_dragonfly:
            tiger_cache = getattr(
                getattr(_app_state.nexus_fs, "_rebac_manager", None),
                "_tiger_cache",
                None,
            )
            if tiger_cache:
                dragonfly_tiger = _app_state.cache_factory.get_tiger_cache()
                tiger_cache.set_dragonfly_cache(dragonfly_tiger)
                logger.info(
                    "[TIGER] Dragonfly L2 cache wired up - "
                    "L1 (memory) -> L2 (Dragonfly) -> L3 (PostgreSQL)"
                )
    except Exception as e:
        logger.warning(f"Failed to initialize cache factory: {e}")

    # WebSocket Manager for real-time events (Issue #1116)
    # Bridges Redis Pub/Sub to WebSocket clients for push notifications
    try:
        from nexus.server.websocket import WebSocketManager

        # Get event bus from NexusFS if available
        event_bus = None
        if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "_event_bus"):
            event_bus = _app_state.nexus_fs._event_bus

        _app_state.websocket_manager = WebSocketManager(event_bus=event_bus)
        await _app_state.websocket_manager.start()
        logger.info("WebSocket manager started for real-time events")
    except Exception as e:
        logger.warning(f"Failed to start WebSocket manager: {e}")

    # Connect Lock Manager coordination client (Issue #1186)
    # Required for distributed lock REST API endpoints
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "_coordination_client"):
        coord_client = _app_state.nexus_fs._coordination_client
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
        and _app_state.database_url
    )

    if search_daemon_enabled:
        try:
            from nexus.search.daemon import DaemonConfig, SearchDaemon, set_search_daemon

            config = DaemonConfig(
                database_url=_app_state.database_url,
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

            _app_state.search_daemon = SearchDaemon(config)
            await _app_state.search_daemon.startup()
            _app_state.search_daemon_enabled = True
            set_search_daemon(_app_state.search_daemon)

            # Set NexusFS reference for index refresh (Issue #1024)
            _app_state.search_daemon._nexus_fs = _app_state.nexus_fs

            stats = _app_state.search_daemon.get_stats()
            logger.info(
                f"Search Daemon started: {stats['bm25_documents']} docs indexed, "
                f"startup={stats['startup_time_ms']:.1f}ms"
            )
        except Exception as e:
            logger.warning(f"Failed to start Search Daemon: {e}")
            _app_state.search_daemon_enabled = False
    else:
        logger.debug("Search Daemon disabled (set NEXUS_SEARCH_DAEMON=true to enable)")

    # Tiger Cache queue processor (Issue #935)
    # NOTE: Disabled by default - write-through handles grants/revokes immediately
    # Enable with NEXUS_ENABLE_TIGER_WORKER=true for cache warming scenarios
    tiger_task: asyncio.Task[Any] | None = None
    # Issue #913: Track startup tasks to prevent memory leaks on shutdown
    warm_task: asyncio.Task[Any] | None = None
    backfill_task: asyncio.Task[Any] | None = None
    if _app_state.nexus_fs and os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() in (
        "true",
        "1",
        "yes",
    ):
        try:
            from nexus.server.background_tasks import tiger_cache_queue_task

            tiger_task = asyncio.create_task(
                tiger_cache_queue_task(_app_state.nexus_fs, interval_seconds=60, batch_size=1)
            )
            logger.info("Tiger Cache queue processor started (explicit enable)")
        except Exception as e:
            logger.warning(f"Failed to start Tiger Cache queue processor: {e}")
    else:
        logger.debug("Tiger Cache queue processor disabled (write-through handles grants)")

    # Tiger Cache warm-up on startup (Issue #979)
    # Pre-load recently used permission bitmaps to avoid cold-start penalties
    # Non-blocking: runs in background thread, server starts immediately
    if _app_state.nexus_fs:
        try:
            tiger_cache = getattr(_app_state.nexus_fs._rebac_manager, "_tiger_cache", None)
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
                    from nexus.core.tiger_cache import DirectoryGrantExpander

                    expander = DirectoryGrantExpander(
                        engine=_app_state.nexus_fs._rebac_manager.engine,
                        tiger_cache=tiger_cache,
                        metadata_store=_app_state.nexus_fs.metadata,
                    )
                    _app_state.directory_grant_expander = expander

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
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "metadata"):
        try:
            _nexus_fs = _app_state.nexus_fs  # Capture for closure

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
    if _app_state.nexus_fs:
        try:
            warmup_max_files = int(os.getenv("NEXUS_CACHE_WARMUP_MAX_FILES", "1000"))
            warmup_depth = int(os.getenv("NEXUS_CACHE_WARMUP_DEPTH", "2"))
            _nexus_fs_warmup = _app_state.nexus_fs  # Capture for closure

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

    yield

    # Cleanup
    logger.info("Shutting down FastAPI Nexus server...")

    # Issue #940: Shutdown AsyncNexusFS
    if _app_state.async_nexus_fs:
        try:
            await _app_state.async_nexus_fs.close()
            logger.info("AsyncNexusFS stopped")
        except Exception as e:
            logger.warning(f"Error shutting down AsyncNexusFS: {e}")

    # Shutdown Search Daemon (Issue #951)
    if _app_state.search_daemon:
        try:
            await _app_state.search_daemon.shutdown()
            logger.info("Search Daemon stopped")
        except Exception as e:
            logger.warning(f"Error shutting down Search Daemon: {e}")

    # Stop DirectoryGrantExpander worker
    if hasattr(_app_state, "directory_grant_expander") and _app_state.directory_grant_expander:
        try:
            _app_state.directory_grant_expander.stop()
            logger.info("DirectoryGrantExpander worker stopped")
        except Exception as e:
            logger.debug(f"Error stopping DirectoryGrantExpander: {e}")

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
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "_event_tasks"):
        event_tasks = _app_state.nexus_fs._event_tasks.copy()
        for task in event_tasks:
            task.cancel()
        if event_tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.gather(*event_tasks, return_exceptions=True)
            logger.info(f"Cancelled {len(event_tasks)} pending event tasks")

    # Shutdown WebSocket manager (Issue #1116)
    if _app_state.websocket_manager:
        try:
            await _app_state.websocket_manager.stop()
            logger.info("WebSocket manager stopped")
        except Exception as e:
            logger.warning(f"Error shutting down WebSocket manager: {e}")

    # Disconnect Lock Manager coordination client (Issue #1186)
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "_coordination_client"):
        coord_client = _app_state.nexus_fs._coordination_client
        if coord_client is not None:
            try:
                await coord_client.disconnect()
                logger.info("Lock manager coordination client disconnected")
            except Exception as e:
                logger.debug(f"Error disconnecting coordination client: {e}")

    if _app_state.subscription_manager:
        await _app_state.subscription_manager.close()
        # Clear global singleton (Issue #1115)
        from nexus.server.subscriptions import set_subscription_manager

        set_subscription_manager(None)
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "close"):
        _app_state.nexus_fs.close()

    # Shutdown cache factory (Issue #1075)
    if hasattr(_app_state, "cache_factory") and _app_state.cache_factory:
        try:
            await _app_state.cache_factory.shutdown()
            logger.info("Cache factory stopped")
        except Exception as e:
            logger.warning(f"Error shutting down cache factory: {e}")

    # Shutdown OpenTelemetry (Issue #764)
    try:
        from nexus.server.telemetry import shutdown_telemetry

        shutdown_telemetry()
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
) -> FastAPI:
    """Create FastAPI application.

    Args:
        nexus_fs: NexusFS instance
        api_key: Static API key for authentication
        auth_provider: Auth provider instance
        database_url: Database URL for async operations
        thread_pool_size: Thread pool size for sync operations (default: 200)
        operation_timeout: Timeout for sync operations in seconds (default: 30.0)

    Returns:
        Configured FastAPI application
    """
    # Store in global state
    _app_state.nexus_fs = nexus_fs
    _app_state.api_key = api_key
    _app_state.auth_provider = auth_provider
    _app_state.database_url = database_url

    # Thread pool and timeout settings (Issue #932)
    # Read from parameter, environment variable, or use default
    _app_state.thread_pool_size = thread_pool_size or int(
        os.environ.get("NEXUS_THREAD_POOL_SIZE", "200")
    )
    _app_state.operation_timeout = operation_timeout or float(
        os.environ.get("NEXUS_OPERATION_TIMEOUT", "30.0")
    )

    # Discover exposed methods
    _app_state.exposed_methods = _discover_exposed_methods(nexus_fs)

    # Initialize subscription manager if we have a metadata store
    try:
        if hasattr(nexus_fs, "SessionLocal"):
            from nexus.server.subscriptions import (
                SubscriptionManager,
                set_subscription_manager,
            )

            _app_state.subscription_manager = SubscriptionManager(nexus_fs.SessionLocal)
            # Inject into NexusFS for automatic event broadcasting
            nexus_fs.subscription_manager = _app_state.subscription_manager
            # Set global singleton for FUSE event firing (Issue #1115)
            set_subscription_manager(_app_state.subscription_manager)
            logger.info("Subscription manager initialized and injected into NexusFS")
    except Exception as e:
        logger.warning(f"Failed to initialize subscription manager: {e}")

    # Create app
    app = FastAPI(
        title="Nexus RPC Server",
        description="AI-Native Distributed Filesystem API",
        version="1.0.0",
        lifespan=lifespan,
    )

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

    # Initialize OAuth provider if credentials are available
    _initialize_oauth_provider(nexus_fs, auth_provider, database_url)

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

    # Set NexusFS instance for user provisioning in OAuth flow
    try:
        from nexus.server.auth.auth_routes import set_nexus_instance

        set_nexus_instance(_app_state.nexus_fs)
        logger.info("NexusFS instance registered for OAuth provisioning")
    except Exception as e:
        logger.warning(f"Failed to register NexusFS instance: {e}")


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


async def _graph_enhanced_search(
    query: str,
    search_type: str,
    limit: int,
    path_filter: str | None,
    alpha: float,
    graph_mode: str,
) -> list:
    """Execute graph-enhanced search using GraphEnhancedRetriever (Issue #1040).

    Creates a GraphEnhancedRetriever on-the-fly and executes the search.
    This helper is called when graph_mode is not "none".

    Args:
        query: Search query text
        search_type: Base search type (keyword, semantic, hybrid)
        limit: Maximum results
        path_filter: Optional path prefix filter
        alpha: Semantic vs keyword weight
        graph_mode: Graph enhancement mode (low, high, dual)

    Returns:
        List of GraphEnhancedSearchResult
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from nexus.search.graph_retrieval import (
        GraphEnhancedRetriever,
        GraphRetrievalConfig,
    )
    from nexus.search.graph_store import GraphStore
    from nexus.search.semantic import SemanticSearchResult

    if not _app_state.nexus_fs:
        raise RuntimeError("NexusFS not initialized")

    # Get database URL
    db_url = _app_state.database_url
    if not db_url:
        db_url = (
            _app_state.nexus_fs._record_store.database_url
            if _app_state.nexus_fs._record_store
            else None
        )

    # Convert to async URL
    if not db_url:
        raise RuntimeError("No database URL available for graph search endpoint")
    async_url = db_url
    if async_url.startswith("postgresql://"):
        async_url = async_url.replace("postgresql://", "postgresql+asyncpg://")
    elif async_url.startswith("sqlite:///"):
        async_url = async_url.replace("sqlite:///", "sqlite+aiosqlite:///")

    # Create async engine and session
    engine = create_async_engine(async_url, echo=False)
    async_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with async_session_factory() as session:
            # Initialize components
            graph_store = GraphStore(session, zone_id="default")

            # Create a wrapper for SemanticSearch that uses the search daemon
            class DaemonSemanticSearchWrapper:
                """Wraps search daemon as SemanticSearch interface."""

                def __init__(self, daemon: Any) -> None:
                    self.daemon = daemon
                    self.embedding_provider = getattr(daemon, "_embedding_provider", None)

                async def search(
                    self,
                    query: str,
                    path: str = "/",
                    limit: int = 10,
                    search_mode: str = "hybrid",
                    alpha: float = 0.5,
                ) -> list[SemanticSearchResult]:
                    # Map search_mode to daemon's search_type
                    results = await self.daemon.search(
                        query=query,
                        search_type=search_mode,
                        limit=limit,
                        path_filter=path if path != "/" else None,
                        alpha=alpha,
                    )
                    # Convert daemon results to SemanticSearchResult
                    return [
                        SemanticSearchResult(
                            path=r.path,
                            chunk_index=r.chunk_index,
                            chunk_text=r.chunk_text,
                            score=r.score,
                            start_offset=r.start_offset,
                            end_offset=r.end_offset,
                            line_start=r.line_start,
                            line_end=r.line_end,
                            keyword_score=r.keyword_score,
                            vector_score=r.vector_score,
                        )
                        for r in results
                    ]

            # Create wrapper and retriever
            semantic_wrapper = DaemonSemanticSearchWrapper(_app_state.search_daemon)
            embedding_provider = getattr(_app_state.search_daemon, "_embedding_provider", None)

            config = GraphRetrievalConfig(
                graph_mode=graph_mode,
                entity_similarity_threshold=0.75,
                neighbor_hops=2,
            )

            retriever = GraphEnhancedRetriever(
                semantic_search=semantic_wrapper,  # type: ignore
                graph_store=graph_store,
                embedding_provider=embedding_provider,
                config=config,
            )

            # Execute search
            results = await retriever.search(
                query=query,
                path=path_filter or "/",
                limit=limit,
                graph_mode=graph_mode,
                search_mode=search_type,
                alpha=alpha,
                include_graph_context=True,
            )

            return results
    finally:
        await engine.dispose()


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

        if _app_state.nexus_fs:
            enforce_permissions = getattr(_app_state.nexus_fs, "_enforce_permissions", None)
            enforce_zone_isolation = getattr(_app_state.nexus_fs, "_enforce_zone_isolation", None)

        # Check if authentication is configured
        has_auth = bool(_app_state.api_key or _app_state.auth_provider)

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
        if _app_state.search_daemon:
            daemon_health = _app_state.search_daemon.get_health()
            health["components"]["search_daemon"] = daemon_health
        else:
            health["components"]["search_daemon"] = {
                "status": "disabled",
                "message": "Set NEXUS_SEARCH_DAEMON=true to enable",
            }

        # Check async ReBAC manager
        health["components"]["rebac"] = {
            "status": "healthy" if _app_state.async_rebac_manager else "disabled",
        }

        # Check subscription manager
        health["components"]["subscriptions"] = {
            "status": "healthy" if _app_state.subscription_manager else "disabled",
        }

        # Check WebSocket manager (Issue #1116)
        if _app_state.websocket_manager:
            ws_stats = _app_state.websocket_manager.get_stats()
            health["components"]["websocket"] = {
                "status": "healthy",
                "current_connections": ws_stats["current_connections"],
                "total_connections": ws_stats["total_connections"],
                "total_messages_sent": ws_stats["total_messages_sent"],
                "connections_by_zone": ws_stats["connections_by_zone"],
            }
        else:
            health["components"]["websocket"] = {"status": "disabled"}

        # Check mounted backends (Issue #708)
        backends_health: dict[str, Any] = {}
        if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "path_router"):
            mounts = _app_state.nexus_fs.path_router.list_mounts()
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
        if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "metadata"):
            try:
                pg_stats = _app_state.nexus_fs.metadata.get_pool_stats()
                metrics["postgres"] = pg_stats
            except Exception as e:
                metrics["postgres"] = {"error": str(e)}
        else:
            metrics["postgres"] = {"status": "not_available"}

        # Redis/Dragonfly pool stats from cache factory
        try:
            from nexus.cache.factory import get_cache_factory

            cache_factory = get_cache_factory()
            if cache_factory.is_using_dragonfly and cache_factory._cache_client:
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

    # API v2 routes - Memory & ACE endpoints (Issue #1193)
    try:
        from nexus.server.api.v2.routers import (
            consolidation,
            feedback,
            memories,
            mobile_search,
            playbooks,
            reflection,
            trajectories,
        )

        app.include_router(memories.router)
        app.include_router(trajectories.router)
        app.include_router(feedback.router)
        app.include_router(playbooks.router)
        app.include_router(reflection.router)
        app.include_router(consolidation.router)
        app.include_router(mobile_search.router)
        logger.info("API v2 routes registered (32 endpoints)")
    except ImportError as e:
        logger.warning(
            f"Failed to import API v2 routes: {e}. Memory/ACE v2 endpoints will not be available."
        )

    # Nexus Pay API routes (Issue #1209)
    try:
        from nexus.server.api.v2.routers.pay import _register_pay_exception_handlers
        from nexus.server.api.v2.routers.pay import router as pay_router

        app.include_router(pay_router)
        _register_pay_exception_handlers(app)
        logger.info("Nexus Pay API routes registered (8 endpoints)")
    except ImportError as e:
        logger.warning(
            f"Failed to import Nexus Pay routes: {e}. Pay endpoints will not be available."
        )

    # Issue #940: Register async files router (lazy initialization via lifespan)
    try:
        from nexus.server.api.v2.routers.async_files import create_async_files_router

        async_files_router = create_async_files_router(
            get_fs=lambda: _app_state.async_nexus_fs,
        )
        app.include_router(async_files_router, prefix="/api/v2/files")
        logger.info("Async files router registered (9 endpoints)")
    except ImportError as e:
        logger.warning(f"Failed to import async files router: {e}")

    # A2A Protocol Endpoint (Issue #1256)
    try:
        from nexus.a2a import create_a2a_router

        a2a_base_url = os.environ.get("NEXUS_A2A_BASE_URL", "http://localhost:2026")
        a2a_router = create_a2a_router(
            nexus_fs=_app_state.nexus_fs,
            config=None,  # Will use defaults; config can be passed when available
            base_url=a2a_base_url,
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
            "methods": list(_app_state.exposed_methods.keys()),
        }

    # =========================================================================
    # Search Daemon API Endpoints (Issue #951)
    # =========================================================================

    @app.get("/api/search/health", tags=["search"])
    async def search_daemon_health() -> dict[str, Any]:
        """Health check for the search daemon.

        Returns daemon initialization status and component availability.
        """
        if not _app_state.search_daemon:
            return {
                "status": "disabled",
                "daemon_enabled": False,
                "message": "Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
            }

        health: dict[str, Any] = _app_state.search_daemon.get_health()
        return health

    @app.get("/api/search/stats", tags=["search"])
    async def search_daemon_stats() -> dict[str, Any]:
        """Get search daemon statistics.

        Returns performance metrics including latency, document counts, and component status.
        """
        if not _app_state.search_daemon:
            raise HTTPException(
                status_code=503,
                detail="Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
            )

        stats: dict[str, Any] = _app_state.search_daemon.get_stats()
        return stats

    @app.get("/api/search/query", tags=["search"])
    async def search_query(
        q: str = Query(..., description="Search query text", min_length=1),
        type: str = Query("hybrid", description="Search type: keyword, semantic, or hybrid"),
        limit: int = Query(10, description="Maximum number of results", ge=1, le=100),
        path: str | None = Query(None, description="Optional path prefix filter"),
        alpha: float = Query(
            0.5, description="Semantic vs keyword weight (0.0-1.0)", ge=0.0, le=1.0
        ),
        fusion: str = Query("rrf", description="Fusion method: rrf, weighted, or rrf_weighted"),
        adaptive_k: bool = Query(
            False,
            description="Adaptive retrieval: dynamically adjust limit based on query complexity",
        ),
        graph_mode: str = Query(
            "none",
            description="Graph enhancement mode (Issue #1040): none, low, high, dual, or auto",
        ),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Execute a fast search query using the search daemon.

        This endpoint uses pre-warmed indexes for sub-50ms response times.

        Args:
            q: Search query text
            type: Search type ("keyword", "semantic", or "hybrid")
            limit: Maximum number of results (1-100). Used as k_base when adaptive_k=True.
            path: Optional path prefix filter (e.g., "/docs/")
            alpha: Weight for semantic search (0.0 = all keyword, 1.0 = all semantic)
            fusion: Fusion method for hybrid search
            adaptive_k: If True, dynamically adjust limit based on query complexity (Issue #1021)
            graph_mode: Graph enhancement mode (Issue #1040):
                - "none": Traditional search only (default)
                - "low": Entity matching + N-hop neighbor expansion
                - "high": Theme/cluster context from hierarchical memory
                - "dual": Full LightRAG-style dual-level search
                - "auto": Automatically select based on query complexity (Issue #1041)

        Returns:
            Search results with scores and metadata
        """
        import time

        start_time = time.perf_counter()

        if not _app_state.search_daemon:
            raise HTTPException(
                status_code=503,
                detail="Search daemon not enabled (set NEXUS_SEARCH_DAEMON=true)",
            )

        if not _app_state.search_daemon.is_initialized:
            raise HTTPException(
                status_code=503,
                detail="Search daemon is still initializing",
            )

        # Validate search type
        if type not in ("keyword", "semantic", "hybrid"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid search type: {type}. Must be 'keyword', 'semantic', or 'hybrid'",
            )

        # Validate fusion method
        if fusion not in ("rrf", "weighted", "rrf_weighted"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid fusion method: {fusion}. Must be 'rrf', 'weighted', or 'rrf_weighted'",
            )

        # Validate graph mode (Issue #1040)
        if graph_mode not in ("none", "low", "high", "dual", "auto"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid graph_mode: {graph_mode}. Must be 'none', 'low', 'high', 'dual', or 'auto'",
            )

        # Query routing for auto mode (Issue #1041)
        routing_info: dict[str, Any] | None = None
        effective_graph_mode = graph_mode
        effective_limit = limit

        if graph_mode == "auto":
            from nexus.search.query_router import QueryRouter, RoutingConfig

            router = QueryRouter(config=RoutingConfig())
            routed = router.route(q, base_limit=limit)

            effective_graph_mode = routed.graph_mode
            effective_limit = routed.adjusted_limit
            routing_info = routed.to_dict()

            logger.info(
                f"[QUERY-ROUTER] {routed.reasoning}, "
                f"graph_mode={effective_graph_mode}, limit={effective_limit}"
            )

        try:
            # Use graph-enhanced search if effective_graph_mode is not "none" (Issue #1040)
            if effective_graph_mode != "none":
                results = await _graph_enhanced_search(
                    query=q,
                    search_type=type,
                    limit=effective_limit,
                    path_filter=path,
                    alpha=alpha,
                    graph_mode=effective_graph_mode,
                )
                latency_ms = (time.perf_counter() - start_time) * 1000

                response: dict[str, Any] = {
                    "query": q,
                    "search_type": type,
                    "graph_mode": effective_graph_mode,
                    "results": [
                        {
                            "path": r.path,
                            "chunk_text": r.chunk_text,
                            "score": round(r.score, 4),
                            "chunk_index": r.chunk_index,
                            "line_start": r.line_start,
                            "line_end": r.line_end,
                            "keyword_score": round(r.keyword_score, 4) if r.keyword_score else None,
                            "vector_score": round(r.vector_score, 4) if r.vector_score else None,
                            "graph_score": round(r.graph_score, 4) if r.graph_score else None,
                            "graph_context": r.graph_context.to_dict() if r.graph_context else None,
                        }
                        for r in results
                    ],
                    "total": len(results),
                    "latency_ms": round(latency_ms, 2),
                }
                if routing_info:
                    response["routing"] = routing_info
                return response

            # Standard search (effective_graph_mode="none")
            results = await _app_state.search_daemon.search(
                query=q,
                search_type=type,
                limit=effective_limit,
                path_filter=path,
                alpha=alpha,
                fusion_method=fusion,
                adaptive_k=adaptive_k,
            )

            latency_ms = (time.perf_counter() - start_time) * 1000

            response = {
                "query": q,
                "search_type": type,
                "graph_mode": "none",
                "results": [
                    {
                        "path": r.path,
                        "chunk_text": r.chunk_text,
                        "score": round(r.score, 4),
                        "chunk_index": r.chunk_index,
                        "line_start": r.line_start,
                        "line_end": r.line_end,
                        "keyword_score": round(r.keyword_score, 4) if r.keyword_score else None,
                        "vector_score": round(r.vector_score, 4) if r.vector_score else None,
                    }
                    for r in results
                ],
                "total": len(results),
                "latency_ms": round(latency_ms, 2),
            }
            if routing_info:
                response["routing"] = routing_info
            return response

        except Exception as e:
            logger.error(f"Search error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Search error: {e}") from e

    @app.post("/api/search/refresh", tags=["search"])
    async def search_refresh_notify(
        path: str = Query(..., description="Path of the changed file"),
        change_type: str = Query("update", description="Type of change: create, update, delete"),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Notify the search daemon of a file change for index refresh.

        This endpoint allows external systems to trigger index updates
        when files are modified outside of the normal Nexus write flow.

        Args:
            path: Virtual path of the changed file
            change_type: Type of change (create, update, delete)

        Returns:
            Acknowledgment of the notification
        """
        if not _app_state.search_daemon:
            raise HTTPException(
                status_code=503,
                detail="Search daemon not enabled",
            )

        await _app_state.search_daemon.notify_file_change(path, change_type)

        return {
            "status": "accepted",
            "path": path,
            "change_type": change_type,
        }

    @app.post("/api/search/expand", tags=["search"])
    async def search_expand(
        q: str = Query(..., description="Query to expand", min_length=1),
        context: str | None = Query(None, description="Optional context about the collection"),
        model: str = Query("deepseek/deepseek-chat", description="LLM model to use"),
        max_lex: int = Query(2, description="Max lexical variants", ge=0, le=5),
        max_vec: int = Query(2, description="Max vector variants", ge=0, le=5),
        max_hyde: int = Query(2, description="Max HyDE passages", ge=0, le=5),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Expand a search query using LLM-based query expansion (Issue #1174).

        Generates multiple query variants to improve search recall:
        - lex: Lexical variants (keywords for BM25)
        - vec: Vector variants (natural language for embeddings)
        - hyde: Hypothetical document passages

        Requires OPENROUTER_API_KEY environment variable.

        Args:
            q: The query to expand
            context: Optional context about the document collection
            model: LLM model to use (default: deepseek/deepseek-chat)
            max_lex: Maximum lexical variants (0-5)
            max_vec: Maximum vector variants (0-5)
            max_hyde: Maximum HyDE passages (0-5)

        Returns:
            Query expansions with metadata
        """
        import os
        import time

        from nexus.search.query_expansion import (
            OpenRouterQueryExpander,
            QueryExpansionConfig,
        )

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise HTTPException(
                status_code=503,
                detail="OPENROUTER_API_KEY not configured for query expansion",
            )

        start_time = time.perf_counter()

        try:
            config = QueryExpansionConfig(
                model=model,
                max_lex_variants=max_lex,
                max_vec_variants=max_vec,
                max_hyde_passages=max_hyde,
                timeout=15.0,
            )
            expander = OpenRouterQueryExpander(config=config, api_key=api_key)

            expansions = await expander.expand(q, context=context)
            await expander.close()

            latency_ms = (time.perf_counter() - start_time) * 1000

            return {
                "query": q,
                "context": context,
                "model": model,
                "expansions": [
                    {
                        "type": e.expansion_type.value,
                        "text": e.text,
                        "weight": e.weight,
                    }
                    for e in expansions
                ],
                "total": len(expansions),
                "latency_ms": round(latency_ms, 2),
            }

        except Exception as e:
            logger.error(f"Query expansion error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Query expansion error: {e}") from e

    # =========================================================================
    # Memory API Endpoints (Issue #1023 - Temporal Query Operators)
    # =========================================================================

    @app.get("/api/memory/query", tags=["memory"])
    async def memory_query(
        scope: str | None = Query(None, description="Filter by scope (agent/user/zone/global)"),
        memory_type: str | None = Query(None, description="Filter by memory type"),
        state: str = Query("active", description="Filter by state (inactive/active/all)"),
        after: str | None = Query(
            None, description="Filter memories created after this time (ISO-8601). #1023"
        ),
        before: str | None = Query(
            None, description="Filter memories created before this time (ISO-8601). #1023"
        ),
        during: str | None = Query(
            None, description="Filter memories during this period (e.g., '2025', '2025-01'). #1023"
        ),
        entity_type: str | None = Query(
            None, description="Filter by entity type (PERSON, ORG, LOCATION, DATE, etc.). #1025"
        ),
        person: str | None = Query(None, description="Filter by person name reference. #1025"),
        event_after: str | None = Query(
            None, description="Filter by event date >= value (ISO-8601). #1028"
        ),
        event_before: str | None = Query(
            None, description="Filter by event date <= value (ISO-8601). #1028"
        ),
        limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Query memories with optional temporal and entity filters.

        Supports temporal operators (Issue #1023):
        - after: Return memories created after this datetime
        - before: Return memories created before this datetime
        - during: Return memories created during this period (partial date like "2025" or "2025-01")

        Supports entity filters (Issue #1025 - SimpleMem symbolic layer):
        - entity_type: Filter by extracted entity type (PERSON, ORG, LOCATION, DATE, etc.)
        - person: Filter by person name reference

        Supports event date filters (Issue #1028 - Temporal anchoring):
        - event_after: Filter by earliest_date >= value (date mentioned in content)
        - event_before: Filter by latest_date <= value (date mentioned in content)

        Note: 'during' cannot be used together with 'after' or 'before'.

        Args:
            scope: Filter by scope
            memory_type: Filter by memory type
            state: Filter by state (default: active)
            after: ISO-8601 datetime or date string
            before: ISO-8601 datetime or date string
            during: Partial date string (year, year-month, or full date)
            entity_type: Entity type to filter by (e.g., PERSON, ORG)
            person: Person name to filter by
            event_after: ISO-8601 date to filter by earliest_date >= value. #1028
            event_before: ISO-8601 date to filter by latest_date <= value. #1028
            limit: Maximum number of results

        Returns:
            List of memories matching the filters
        """
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            context = get_operation_context(_auth_result)

            results = _app_state.nexus_fs.memory.query(
                scope=scope,
                memory_type=memory_type,
                state=state,
                after=after,
                before=before,
                during=during,
                entity_type=entity_type,
                person=person,
                event_after=event_after,
                event_before=event_before,
                limit=limit,
                context=context,
            )

            return {
                "memories": results,
                "total": len(results),
                "filters": {
                    "scope": scope,
                    "memory_type": memory_type,
                    "state": state,
                    "after": after,
                    "before": before,
                    "during": during,
                    "entity_type": entity_type,
                    "person": person,
                    "event_after": event_after,
                    "event_before": event_before,
                },
            }

        except ValueError as e:
            # Handle temporal validation errors
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Memory query error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Memory query error: {e}") from e

    @app.get("/api/memory/list", tags=["memory"])
    async def memory_list(
        scope: str | None = Query(None, description="Filter by scope"),
        memory_type: str | None = Query(None, description="Filter by memory type"),
        namespace: str | None = Query(None, description="Filter by exact namespace"),
        namespace_prefix: str | None = Query(None, description="Filter by namespace prefix"),
        state: str = Query("active", description="Filter by state (inactive/active/all)"),
        after: str | None = Query(
            None, description="Filter memories created after this time (ISO-8601). #1023"
        ),
        before: str | None = Query(
            None, description="Filter memories created before this time (ISO-8601). #1023"
        ),
        during: str | None = Query(
            None, description="Filter memories during this period (e.g., '2025', '2025-01'). #1023"
        ),
        limit: int = Query(100, description="Maximum number of results", ge=1, le=1000),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """List memories with optional temporal filters (Issue #1023).

        Similar to query but also supports namespace filtering.

        Args:
            scope: Filter by scope
            memory_type: Filter by memory type
            namespace: Filter by exact namespace
            namespace_prefix: Filter by namespace prefix
            state: Filter by state
            after: ISO-8601 datetime or date string
            before: ISO-8601 datetime or date string
            during: Partial date string
            limit: Maximum number of results

        Returns:
            List of memories matching the filters
        """
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            context = get_operation_context(_auth_result)

            results = _app_state.nexus_fs.memory.list(
                scope=scope,
                memory_type=memory_type,
                namespace=namespace,
                namespace_prefix=namespace_prefix,
                state=state,
                after=after,
                before=before,
                during=during,
                limit=limit,
                context=context,
            )

            return {
                "memories": results,
                "total": len(results),
            }

        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        except Exception as e:
            logger.error(f"Memory list error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Memory list error: {e}") from e

    @app.post("/api/memory/store", tags=["memory"])
    async def memory_store(
        request: Request,
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Store a new memory.

        Request body:
        {
            "content": "Memory content",
            "scope": "user",
            "memory_type": "fact",
            "importance": 0.8,
            "namespace": "optional/namespace",
            "path_key": "optional_key",
            "state": "active",
            "resolve_coreferences": false,
            "coreference_context": "Prior conversation context",
            "resolve_temporal": false,
            "temporal_reference_time": "2025-01-10T12:00:00Z",
            "extract_temporal": true,
            "extract_relationships": false,
            "relationship_types": ["MANAGES", "WORKS_WITH", "DEPENDS_ON"]
        }

        Returns:
            The created memory ID
        """
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            body = await request.json()
            context = get_operation_context(_auth_result)

            memory_id = _app_state.nexus_fs.memory.store(
                content=body.get("content", ""),
                scope=body.get("scope", "user"),
                memory_type=body.get("memory_type"),
                importance=body.get("importance"),
                namespace=body.get("namespace"),
                path_key=body.get("path_key"),
                state=body.get("state", "active"),
                resolve_coreferences=body.get("resolve_coreferences", False),
                coreference_context=body.get("coreference_context"),
                resolve_temporal=body.get("resolve_temporal", False),
                temporal_reference_time=body.get("temporal_reference_time"),
                extract_temporal=body.get("extract_temporal", True),
                extract_relationships=body.get("extract_relationships", False),
                relationship_types=body.get("relationship_types"),
                store_to_graph=body.get("store_to_graph", False),  # #1039
                context=context,
            )

            return {"memory_id": memory_id, "status": "created"}

        except Exception as e:
            logger.error(f"Memory store error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Memory store error: {e}") from e

    @app.get("/api/memory/{memory_id}", tags=["memory"])
    async def memory_get(
        memory_id: str,
        track_access: bool = Query(True, description="Track this access for decay calculation"),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Get a specific memory by ID.

        Returns memory with effective importance calculated based on time decay (Issue #1030).

        Args:
            memory_id: The memory UUID
            track_access: Whether to track this access for decay calculation (default: True)

        Returns:
            Memory details including:
            - importance: Current stored importance
            - importance_original: Original importance (before any decay)
            - importance_effective: Calculated importance with time decay applied
            - access_count: Number of times this memory has been accessed
            - last_accessed_at: Last access timestamp
        """
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            context = get_operation_context(_auth_result)
            result = _app_state.nexus_fs.memory.get(
                memory_id, track_access=track_access, context=context
            )
            if result is None:
                raise HTTPException(status_code=404, detail=f"Memory not found: {memory_id}")
            return {"memory": result}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Memory get error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Memory get error: {e}") from e

    # =========================================================================
    # Graph API Endpoints (Issue #1039)
    # =========================================================================

    @app.get("/api/graph/entity/{entity_id}", tags=["graph"])
    async def get_graph_entity(
        entity_id: str,
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Get an entity by ID from the knowledge graph.

        Args:
            entity_id: The entity UUID

        Returns:
            Entity details or null if not found
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.search.graph_store import GraphStore

        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            sync_url = _app_state.database_url or ""
            if sync_url.startswith("postgresql://"):
                async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
            elif sync_url.startswith("sqlite:///"):
                async_url = sync_url.replace("sqlite:///", "sqlite+aiosqlite:///")
            else:
                async_url = sync_url

            zone_id = getattr(_app_state.nexus_fs, "zone_id", None) or "default"

            async def _get_entity() -> dict[str, Any] | None:
                engine = create_async_engine(async_url)
                async_session_factory = async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
                try:
                    async with async_session_factory() as session:
                        graph_store = GraphStore(session, zone_id=zone_id)
                        entity = await graph_store.get_entity(entity_id)
                        return entity.to_dict() if entity else None
                finally:
                    await engine.dispose()

            return {"entity": await _get_entity()}

        except Exception as e:
            logger.error(f"Graph entity error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Graph entity error: {e}") from e

    @app.get("/api/graph/entity/{entity_id}/neighbors", tags=["graph"])
    async def get_graph_neighbors(
        entity_id: str,
        hops: int = Query(1, ge=1, le=5, description="Number of hops (1-5)"),
        direction: str = Query("both", description="Direction: outgoing, incoming, both"),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Get N-hop neighbors of an entity.

        Args:
            entity_id: Starting entity UUID
            hops: Number of hops (1-5)
            direction: Relationship direction to follow

        Returns:
            List of neighbor entities with depth and path info
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.search.graph_store import GraphStore

        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            sync_url = _app_state.database_url or ""
            if sync_url.startswith("postgresql://"):
                async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
            elif sync_url.startswith("sqlite:///"):
                async_url = sync_url.replace("sqlite:///", "sqlite+aiosqlite:///")
            else:
                async_url = sync_url

            zone_id = getattr(_app_state.nexus_fs, "zone_id", None) or "default"

            async def _get_neighbors() -> list[dict[str, Any]]:
                engine = create_async_engine(async_url)
                async_session_factory = async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
                try:
                    async with async_session_factory() as session:
                        graph_store = GraphStore(session, zone_id=zone_id)
                        neighbors = await graph_store.get_neighbors(
                            entity_id, hops=hops, direction=direction
                        )
                        return [
                            {
                                "entity": n.entity.to_dict(),
                                "depth": n.depth,
                                "path": n.path,
                            }
                            for n in neighbors
                        ]
                finally:
                    await engine.dispose()

            return {"neighbors": await _get_neighbors()}

        except Exception as e:
            logger.error(f"Graph neighbors error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Graph neighbors error: {e}") from e

    @app.post("/api/graph/subgraph", tags=["graph"])
    async def get_graph_subgraph(
        request: Request,
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Extract a subgraph for GraphRAG context building.

        Request body:
        {
            "entity_ids": ["entity-id-1", "entity-id-2"],
            "max_hops": 2
        }

        Returns:
            Subgraph with entities and relationships
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.search.graph_store import GraphStore

        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            body = await request.json()
            entity_ids = body.get("entity_ids", [])
            max_hops = body.get("max_hops", 2)

            sync_url = _app_state.database_url or ""
            if sync_url.startswith("postgresql://"):
                async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
            elif sync_url.startswith("sqlite:///"):
                async_url = sync_url.replace("sqlite:///", "sqlite+aiosqlite:///")
            else:
                async_url = sync_url

            zone_id = getattr(_app_state.nexus_fs, "zone_id", None) or "default"

            async def _get_subgraph() -> dict[str, Any]:
                engine = create_async_engine(async_url)
                async_session_factory = async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
                try:
                    async with async_session_factory() as session:
                        graph_store = GraphStore(session, zone_id=zone_id)
                        subgraph = await graph_store.get_subgraph(entity_ids, max_hops=max_hops)
                        return subgraph.to_dict()
                finally:
                    await engine.dispose()

            return await _get_subgraph()

        except Exception as e:
            logger.error(f"Graph subgraph error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Graph subgraph error: {e}") from e

    @app.get("/api/graph/search", tags=["graph"])
    async def search_graph_entities(
        name: str = Query(..., description="Entity name to search for"),
        entity_type: str | None = Query(None, description="Filter by entity type"),
        fuzzy: bool = Query(False, description="Search in aliases as well"),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Search for entities by name.

        Args:
            name: Entity name to search for
            entity_type: Optional entity type filter (PERSON, ORG, CONCEPT, etc.)
            fuzzy: If true, search aliases as well

        Returns:
            Matching entity or null
        """
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

        from nexus.search.graph_store import GraphStore

        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        try:
            sync_url = _app_state.database_url or ""
            if sync_url.startswith("postgresql://"):
                async_url = sync_url.replace("postgresql://", "postgresql+asyncpg://")
            elif sync_url.startswith("sqlite:///"):
                async_url = sync_url.replace("sqlite:///", "sqlite+aiosqlite:///")
            else:
                async_url = sync_url

            zone_id = getattr(_app_state.nexus_fs, "zone_id", None) or "default"

            async def _find_entity() -> dict[str, Any] | None:
                engine = create_async_engine(async_url)
                async_session_factory = async_sessionmaker(
                    engine, class_=AsyncSession, expire_on_commit=False
                )
                try:
                    async with async_session_factory() as session:
                        graph_store = GraphStore(session, zone_id=zone_id)
                        entity = await graph_store.find_entity(
                            name=name, entity_type=entity_type, fuzzy=fuzzy
                        )
                        return entity.to_dict() if entity else None
                finally:
                    await engine.dispose()

            return {"entity": await _find_entity()}

        except Exception as e:
            logger.error(f"Graph search error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Graph search error: {e}") from e

    # =========================================================================
    # Hotspot Detection API Endpoints (Issue #921)
    # =========================================================================

    @app.get("/api/v1/admin/hotspot-stats", tags=["admin"])
    async def get_hotspot_stats(
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Get hotspot detection statistics (Issue #921).

        Returns access pattern tracking statistics including:
        - Number of tracked keys
        - Total accesses recorded
        - Hot entries detected (above threshold)
        - Prefetch triggers

        Requires admin authentication.
        """
        permission_enforcer = getattr(_app_state.nexus_fs, "_permission_enforcer", None)
        if not permission_enforcer:
            raise HTTPException(status_code=503, detail="Permission enforcer not available")

        hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
        if not hotspot_detector:
            return {
                "enabled": False,
                "message": "Hotspot tracking not enabled",
            }

        stats: dict[str, Any] = hotspot_detector.get_stats()
        return stats

    @app.get("/api/v1/admin/hot-entries", tags=["admin"])
    async def get_hot_entries(
        limit: int = Query(10, description="Maximum number of entries", ge=1, le=100),
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        """Get current hot permission entries (Issue #921).

        Returns list of frequently accessed permission paths,
        sorted by access count (hottest first).

        Args:
            limit: Maximum number of entries to return

        Requires admin authentication.
        """
        permission_enforcer = getattr(_app_state.nexus_fs, "_permission_enforcer", None)
        if not permission_enforcer:
            raise HTTPException(status_code=503, detail="Permission enforcer not available")

        hotspot_detector = getattr(permission_enforcer, "_hotspot_detector", None)
        if not hotspot_detector:
            return []

        entries = hotspot_detector.get_hot_entries(limit=limit)

        # Convert to dict for JSON serialization
        return [
            {
                "subject_type": e.subject_type,
                "subject_id": e.subject_id,
                "resource_type": e.resource_type,
                "permission": e.permission,
                "zone_id": e.zone_id,
                "access_count": e.access_count,
                "last_access": e.last_access,
            }
            for e in entries
        ]

    # =========================================================================
    # Cache Warmup API Endpoints (Issue #1076)
    # =========================================================================

    @app.post("/api/cache/warmup", tags=["cache"])
    async def warmup_cache(
        request: Request,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Pre-populate caches for faster access (Issue #1076).

        Reduces cold-start latency by pre-caching frequently accessed files.

        Request body:
            path: Optional path to warm (directory warmup)
            user: Optional user for history-based warmup
            hours: Hours to look back for history warmup (default: 24)
            depth: Directory depth (default: 2)
            include_content: Whether to warm content (default: false)
            max_files: Maximum files to warm (default: 1000)

        Returns:
            Warmup statistics including files warmed, duration, etc.
        """
        from nexus.cache.warmer import (
            CacheWarmer,
            WarmupConfig,
            get_file_access_tracker,
        )

        body = await request.json()
        path = body.get("path")
        user = body.get("user")
        hours = body.get("hours", 24)
        depth = body.get("depth", 2)
        include_content = body.get("include_content", False)
        max_files = body.get("max_files", 1000)
        zone_id = auth_result.get("zone_id", "default")

        config = WarmupConfig(
            max_files=max_files,
            depth=depth,
            include_content=include_content,
        )

        file_tracker = get_file_access_tracker() if user else None

        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        warmer = CacheWarmer(
            nexus_fs=_app_state.nexus_fs,
            config=config,
            file_tracker=file_tracker,
        )

        if user:
            stats = await warmer.warmup_from_history(
                user=user,
                hours=hours,
                max_files=max_files,
                zone_id=zone_id,
            )
        elif path:
            stats = await warmer.warmup_directory(
                path=path,
                depth=depth,
                include_content=include_content,
                max_files=max_files,
                zone_id=zone_id,
            )
        else:
            raise HTTPException(
                status_code=400,
                detail="Either 'path' or 'user' must be provided",
            )

        return {
            "status": "completed",
            **stats.to_dict(),
        }

    @app.get("/api/cache/stats", tags=["cache"])
    async def get_cache_stats(
        _auth_result: dict[str, Any] = Depends(require_auth),
    ) -> dict[str, Any]:
        """Get cache statistics (Issue #1076).

        Returns statistics for all cache layers including hit rates,
        memory usage, and entry counts.
        """
        from nexus.cache.warmer import get_file_access_tracker

        nx = _app_state.nexus_fs
        if not nx:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        cache_stats: dict[str, Any] = {}

        # Metadata cache stats
        if hasattr(nx, "metadata") and hasattr(nx.metadata, "_cache"):
            cache = nx.metadata._cache
            if cache:
                cache_stats["metadata_cache"] = {
                    "path_cache_size": len(getattr(cache, "_path_cache", {})),
                    "list_cache_size": len(getattr(cache, "_list_cache", {})),
                    "exists_cache_size": len(getattr(cache, "_exists_cache", {})),
                }

        # Content cache stats
        if hasattr(nx, "backend") and hasattr(nx.backend, "content_cache"):
            cc = nx.backend.content_cache
            if cc and hasattr(cc, "get_stats"):
                cache_stats["content_cache"] = cc.get_stats()

        # Permission cache stats
        if hasattr(nx, "_rebac_manager"):
            rm = nx._rebac_manager
            if hasattr(rm, "_permission_cache") and rm._permission_cache:
                pc = rm._permission_cache
                if hasattr(pc, "get_stats"):
                    cache_stats["permission_cache"] = pc.get_stats()

            if hasattr(rm, "_tiger_cache") and rm._tiger_cache:
                tc = rm._tiger_cache
                if hasattr(tc, "get_stats"):
                    cache_stats["tiger_cache"] = tc.get_stats()

        # Directory visibility cache
        if hasattr(nx, "_dir_visibility_cache") and nx._dir_visibility_cache:
            dvc = nx._dir_visibility_cache
            if hasattr(dvc, "get_metrics"):
                cache_stats["dir_visibility_cache"] = dvc.get_metrics()

        # File access tracker stats
        tracker = get_file_access_tracker()
        cache_stats["file_access_tracker"] = tracker.get_stats()

        return cache_stats

    @app.get("/api/cache/hot-files", tags=["cache"])
    async def get_hot_files(
        limit: int = Query(20, description="Maximum number of entries", ge=1, le=100),
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> list[dict[str, Any]]:
        """Get frequently accessed files (Issue #1076).

        Returns list of hot files based on recent access patterns.
        """
        from nexus.cache.warmer import get_file_access_tracker

        zone_id = auth_result.get("zone_id", "default")
        tracker = get_file_access_tracker()
        hot_files = tracker.get_hot_files(zone_id=zone_id, limit=limit)

        return [
            {
                "path": f.path,
                "zone_id": f.zone_id,
                "access_count": f.access_count,
                "last_access": f.last_access,
                "total_bytes": f.total_bytes,
            }
            for f in hot_files
        ]

    # =========================================================================
    # Subscription API Endpoints
    # =========================================================================

    @app.post("/api/subscriptions", tags=["subscriptions"])
    async def create_subscription(
        request: Request,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Create a new webhook subscription.

        Subscribe to file events (write, delete, rename) with optional path filters.
        """
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        from nexus.server.subscriptions import SubscriptionCreate

        body = await request.json()
        data = SubscriptionCreate(**body)
        zone_id = auth_result.get("zone_id") or "default"
        created_by = auth_result.get("subject_id")

        subscription = _app_state.subscription_manager.create(
            zone_id=zone_id,
            data=data,
            created_by=created_by,
        )
        return JSONResponse(content=subscription.model_dump(mode="json"), status_code=201)

    @app.get("/api/subscriptions", tags=["subscriptions"])
    async def list_subscriptions(
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """List webhook subscriptions for the current zone."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        zone_id = auth_result.get("zone_id") or "default"
        subscriptions = _app_state.subscription_manager.list_subscriptions(
            zone_id=zone_id,
            enabled_only=enabled_only,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            content={"subscriptions": [s.model_dump(mode="json") for s in subscriptions]}
        )

    @app.get("/api/subscriptions/{subscription_id}", tags=["subscriptions"])
    async def get_subscription(
        subscription_id: str,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Get a webhook subscription by ID."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        zone_id = auth_result.get("zone_id") or "default"
        subscription = _app_state.subscription_manager.get(subscription_id, zone_id)
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content=subscription.model_dump(mode="json"))

    @app.patch("/api/subscriptions/{subscription_id}", tags=["subscriptions"])
    async def update_subscription(
        subscription_id: str,
        request: Request,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Update a webhook subscription."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        from nexus.server.subscriptions import SubscriptionUpdate

        body = await request.json()
        data = SubscriptionUpdate(**body)
        zone_id = auth_result.get("zone_id") or "default"

        subscription = _app_state.subscription_manager.update(
            subscription_id=subscription_id,
            zone_id=zone_id,
            data=data,
        )
        if subscription is None:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content=subscription.model_dump(mode="json"))

    @app.delete("/api/subscriptions/{subscription_id}", tags=["subscriptions"])
    async def delete_subscription(
        subscription_id: str,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Delete a webhook subscription."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        zone_id = auth_result.get("zone_id") or "default"
        deleted = _app_state.subscription_manager.delete(subscription_id, zone_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Subscription not found")
        return JSONResponse(content={"deleted": True})

    @app.post("/api/subscriptions/{subscription_id}/test", tags=["subscriptions"])
    async def test_subscription(
        subscription_id: str,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Send a test event to a webhook subscription."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        zone_id = auth_result.get("zone_id") or "default"
        result = await _app_state.subscription_manager.test(subscription_id, zone_id)
        return JSONResponse(content=result)

    # =========================================================================
    # Lock API Endpoints (Issue #1186)
    # =========================================================================

    def _get_lock_manager() -> Any:
        """Get the lock manager from NexusFS or raise 503."""
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not available")
        if not _app_state.nexus_fs._has_distributed_locks():
            raise HTTPException(
                status_code=503,
                detail="Distributed lock manager not configured. "
                "Enable Redis/Dragonfly for distributed locking.",
            )
        return _app_state.nexus_fs._lock_manager

    @app.post("/api/locks", tags=["locks"], status_code=201, response_model=LockResponse)
    async def acquire_lock(
        request: LockAcquireRequest,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> LockResponse:
        """Acquire a distributed lock on a path.

        Supports both mutex (max_holders=1) and semaphore (max_holders>1) modes.
        Use blocking=false for non-blocking acquisition (returns immediately).

        Performance:
        - Typical latency: <5ms p99
        - Uses Redis SET NX EX for atomic acquisition
        - Exponential backoff with jitter to prevent thundering herd
        """
        lock_manager = _get_lock_manager()
        zone_id = auth_result.get("zone_id") or "default"

        # Non-blocking mode: use timeout=0
        timeout = request.timeout if request.blocking else 0.0

        try:
            lock_id = await lock_manager.acquire(
                zone_id=zone_id,
                path=request.path,
                timeout=timeout,
                ttl=request.ttl,
                max_holders=request.max_holders,
            )
        except ValueError as e:
            # SSOT violation (max_holders mismatch)
            raise HTTPException(status_code=409, detail=str(e)) from e

        if lock_id is None:
            # Lock acquisition failed (timeout or non-blocking)
            if request.blocking:
                raise HTTPException(
                    status_code=409,
                    detail=f"Lock acquisition timeout after {request.timeout}s",
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail="Lock not available (non-blocking mode)",
                )

        # Calculate expiration time
        expires_at = datetime.now(UTC).timestamp() + request.ttl
        expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

        return LockResponse(
            lock_id=lock_id,
            path=request.path,
            mode="mutex" if request.max_holders == 1 else "semaphore",
            max_holders=request.max_holders,
            ttl=int(request.ttl),
            expires_at=expires_at_iso,
        )

    @app.get("/api/locks", tags=["locks"], response_model=LockListResponse)
    async def list_locks(
        limit: int = Query(100, ge=1, le=1000, description="Max number of locks to return"),
        pattern: str = Query("*", description="Path pattern filter (glob-style)"),
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> LockListResponse:
        """List active locks for the current zone.

        Uses Redis SCAN for efficient iteration (non-blocking).

        Performance:
        - Uses SCAN instead of KEYS to avoid blocking Redis
        - Pagination via limit parameter
        """
        lock_manager = _get_lock_manager()
        zone_id = auth_result.get("zone_id") or "default"

        # Use SCAN to find locks (non-blocking)
        redis_client = lock_manager._redis.client
        lock_prefix = f"{lock_manager.LOCK_PREFIX}:{zone_id}:"
        sem_prefix = f"{lock_manager.SEMAPHORE_PREFIX}:{zone_id}:"

        locks: list[dict[str, Any]] = []

        # Scan mutex locks
        cursor = 0
        while len(locks) < limit:
            cursor, keys = await redis_client.scan(
                cursor, match=f"{lock_prefix}{pattern}", count=100
            )
            for key in keys:
                if len(locks) >= limit:
                    break
                key_str = key.decode() if isinstance(key, bytes) else key
                path = key_str[len(lock_prefix) :]
                lock_info = await lock_manager.get_lock_info(zone_id, path)
                if lock_info:
                    lock_info["mode"] = "mutex"
                    locks.append(lock_info)
            if cursor == 0:
                break

        # Scan semaphore locks
        cursor = 0
        while len(locks) < limit:
            cursor, keys = await redis_client.scan(
                cursor, match=f"{sem_prefix}{pattern}", count=100
            )
            for key in keys:
                if len(locks) >= limit:
                    break
                key_str = key.decode() if isinstance(key, bytes) else key
                path = key_str[len(sem_prefix) :]
                # Get semaphore info
                members = await redis_client.zrange(key, 0, -1, withscores=True)
                if members:
                    locks.append(
                        {
                            "path": path,
                            "mode": "semaphore",
                            "holders": len(members),
                            "zone_id": zone_id,
                        }
                    )
            if cursor == 0:
                break

        return LockListResponse(locks=locks, count=len(locks))

    @app.get("/api/locks/{path:path}", tags=["locks"], response_model=LockStatusResponse)
    async def get_lock_status(
        path: str,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> LockStatusResponse:
        """Get lock status for a specific path.

        Performance:
        - Single Redis GET operation (~1ms)
        - Pipeline fetches existence + TTL in one round-trip
        """
        lock_manager = _get_lock_manager()
        zone_id = auth_result.get("zone_id") or "default"

        # Normalize path to ensure leading slash (URL path captures without leading /)
        if not path.startswith("/"):
            path = "/" + path

        # Check mutex lock first (most common)
        lock_info = await lock_manager.get_lock_info(zone_id, path)
        if lock_info:
            lock_info["mode"] = "mutex"
            return LockStatusResponse(path=path, locked=True, lock_info=lock_info)

        # Check semaphore
        sem_key = lock_manager._semaphore_key(zone_id, path)
        members = await lock_manager._redis.client.zcard(sem_key)
        if members > 0:
            # Get semaphore details
            config_key = lock_manager._semaphore_config_key(zone_id, path)
            max_holders = await lock_manager._redis.client.get(config_key)
            max_holders_int = (
                int(max_holders.decode() if isinstance(max_holders, bytes) else max_holders)
                if max_holders
                else 0
            )
            return LockStatusResponse(
                path=path,
                locked=True,
                lock_info={
                    "mode": "semaphore",
                    "holders": members,
                    "max_holders": max_holders_int,
                    "path": path,
                    "zone_id": zone_id,
                },
            )

        return LockStatusResponse(path=path, locked=False, lock_info=None)

    @app.delete("/api/locks/{path:path}", tags=["locks"])
    async def release_lock(
        path: str,
        lock_id: str = Query(..., description="Lock ID from acquire response"),
        force: bool = Query(False, description="Force release (admin only)"),
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> JSONResponse:
        """Release a distributed lock.

        The lock_id must match the ID returned during acquisition.
        Use force=true for admin recovery of stuck locks (requires admin role).

        Performance:
        - Single Lua script execution (~1ms)
        - Atomic check-then-delete
        """
        lock_manager = _get_lock_manager()
        zone_id = auth_result.get("zone_id") or "default"

        # Normalize path to ensure leading slash (URL path captures without leading /)
        if not path.startswith("/"):
            path = "/" + path

        if force:
            # Check admin permission
            if not auth_result.get("is_admin", False):
                raise HTTPException(
                    status_code=403, detail="Force release requires admin privileges"
                )
            # Force release regardless of owner
            released = await lock_manager.force_release(zone_id, path)
            if not released:
                raise HTTPException(status_code=404, detail=f"No lock found for path: {path}")
            logger.warning(f"Lock force-released by admin: zone={zone_id}, path={path}")
            return JSONResponse(content={"released": True, "forced": True})

        # Normal release with ownership check
        released = await lock_manager.release(lock_id, zone_id, path)
        if not released:
            raise HTTPException(
                status_code=403,
                detail="Lock release failed: not owned by this lock_id or already expired",
            )
        return JSONResponse(content={"released": True})

    @app.patch("/api/locks/{path:path}", tags=["locks"], response_model=LockResponse)
    async def extend_lock(
        path: str,
        request: LockExtendRequest,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> LockResponse:
        """Extend a lock's TTL (heartbeat).

        Call this periodically (e.g., every TTL/2) to keep long-running
        operations alive. The lock must be owned by the caller (lock_id match).

        Performance:
        - Single Lua script execution (~1ms)
        - Atomic check-then-expire
        """
        lock_manager = _get_lock_manager()
        zone_id = auth_result.get("zone_id") or "default"

        # Normalize path to ensure leading slash (URL path captures without leading /)
        if not path.startswith("/"):
            path = "/" + path

        extended = await lock_manager.extend(request.lock_id, zone_id, path, ttl=request.ttl)
        if not extended:
            raise HTTPException(
                status_code=403,
                detail="Lock extend failed: not owned by this lock_id or already expired",
            )

        # Calculate new expiration
        expires_at = datetime.now(UTC).timestamp() + request.ttl
        expires_at_iso = datetime.fromtimestamp(expires_at, tz=UTC).isoformat()

        # Determine mode (check mutex first)
        lock_info = await lock_manager.get_lock_info(zone_id, path)
        mode: Literal["mutex", "semaphore"] = "mutex" if lock_info else "semaphore"
        max_holders = 1
        if mode == "semaphore":
            config_key = lock_manager._semaphore_config_key(zone_id, path)
            max_raw = await lock_manager._redis.client.get(config_key)
            if max_raw:
                max_holders = int(max_raw.decode() if isinstance(max_raw, bytes) else max_raw)

        return LockResponse(
            lock_id=request.lock_id,
            path=path,
            mode=mode,
            max_holders=max_holders,
            ttl=int(request.ttl),
            expires_at=expires_at_iso,
        )

    # ========================================================================
    # WebSocket Endpoint for Real-Time Events (Issue #1116)
    # ========================================================================

    @app.websocket("/ws/events/{subscription_id}")
    async def websocket_events(
        websocket: WebSocket,
        subscription_id: str,
        token: str = Query(None, description="Authentication token"),
    ) -> None:
        """WebSocket endpoint for real-time file system events.

        Clients connect with a subscription ID to receive filtered events.
        Authentication is via query parameter token (browser compatible).

        Protocol:
        - Server sends: {"type": "event", "data": {...}}
        - Server sends: {"type": "ping"}
        - Client sends: {"type": "pong"}
        - Client sends: {"type": "subscribe", "patterns": [...], "event_types": [...]}

        Args:
            websocket: WebSocket connection
            subscription_id: Subscription ID for event filtering
            token: Bearer token for authentication

        Close Codes:
            1000: Normal closure
            1008: Policy violation (auth failed)
            1011: Internal error
        """
        import uuid

        if not _app_state.websocket_manager:
            await websocket.close(
                code=http_status.WS_1011_INTERNAL_ERROR, reason="WebSocket manager not available"
            )
            return

        # Authenticate
        auth_result = None
        if token:
            # Reuse existing auth validation
            auth_result = await get_auth_result(authorization=f"Bearer {token}")

        # Allow unauthenticated if no auth configured (open access mode)
        if not auth_result and (_app_state.api_key or _app_state.auth_provider):
            await websocket.close(
                code=http_status.WS_1008_POLICY_VIOLATION, reason="Authentication required"
            )
            return

        zone_id = (auth_result or {}).get("zone_id") or "default"
        user_id = (auth_result or {}).get("subject_id")

        # Lookup subscription to get patterns and event types
        patterns: list[str] = []
        event_types: list[str] = []

        if _app_state.subscription_manager and subscription_id != "all":
            subscription = _app_state.subscription_manager.get(subscription_id, zone_id)
            if subscription:
                patterns = subscription.patterns or []
                event_types = subscription.event_types or []
            else:
                # Allow connection even without valid subscription - can subscribe dynamically
                logger.debug(
                    f"Subscription {subscription_id} not found, allowing dynamic subscription"
                )

        # Generate unique connection ID
        connection_id = f"{subscription_id}:{uuid.uuid4().hex[:8]}"

        # Connect
        _conn_info = await _app_state.websocket_manager.connect(
            websocket=websocket,
            zone_id=zone_id,
            connection_id=connection_id,
            user_id=user_id,
            subscription_id=subscription_id if subscription_id != "all" else None,
            patterns=patterns,
            event_types=event_types,
        )

        # Send welcome message
        await websocket.send_json(
            {
                "type": "connected",
                "connection_id": connection_id,
                "zone_id": zone_id,
                "patterns": patterns,
                "event_types": event_types,
            }
        )

        try:
            # Handle client messages (ping/pong, subscribe, etc.)
            await _app_state.websocket_manager.handle_client(websocket, connection_id)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.warning(f"WebSocket error for {connection_id}: {e}")
        finally:
            await _app_state.websocket_manager.disconnect(connection_id)

    @app.websocket("/ws/events")
    async def websocket_events_all(
        websocket: WebSocket,
        token: str = Query(None, description="Authentication token"),
    ) -> None:
        """WebSocket endpoint for all zone events (no subscription filter).

        Same as /ws/events/{subscription_id} but receives all events for the zone.
        """
        # Redirect to the subscription handler with "all" as subscription_id
        await websocket_events(websocket, "all", token)

    # ========================================================================
    # Long-Polling Watch Endpoint (Issue #1117)
    # ========================================================================

    @app.get("/api/watch", tags=["watch"])
    async def watch_for_changes(
        path: str = Query(
            "/**/*",
            description="Path or glob pattern to watch (e.g., /inbox/, **/*.py)",
        ),
        timeout: float = Query(
            30.0,
            ge=0.1,
            le=300.0,
            description="Maximum time to wait in seconds (default: 30, max: 300)",
        ),
        _auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> dict[str, Any]:
        """Long-polling endpoint to wait for file system changes.

        Blocks until a matching change occurs or timeout is reached.
        This is more efficient than polling for AI agents and automation.

        Args:
            path: Virtual path or glob pattern to watch
                - File path (e.g., "/inbox/file.txt"): Watches for content changes
                - Directory path (e.g., "/inbox/"): Watches for file create/delete/rename
                - Glob pattern (e.g., "**/*.py"): Watches matching files
            timeout: Maximum wait time in seconds (0.1-300, default: 30)

        Returns:
            On change detected:
            ```json
            {
                "changes": [{
                    "type": "file_write",
                    "path": "/inbox/new.txt",
                    "timestamp": "2024-01-15T10:30:00Z"
                }],
                "timeout": false
            }
            ```

            On timeout:
            ```json
            {
                "changes": [],
                "timeout": true
            }
            ```

        Example:
            ```bash
            # Watch for Python file changes (30s timeout)
            curl "http://localhost:2026/api/watch?path=**/*.py&timeout=30"

            # Watch inbox directory for new files
            curl "http://localhost:2026/api/watch?path=/inbox/&timeout=60"
            ```
        """
        if not _app_state.nexus_fs:
            raise HTTPException(status_code=503, detail="NexusFS not initialized")

        # Create operation context for permission checks
        context = None
        if _auth_result:
            context = get_operation_context(_auth_result)

        try:
            # Use the existing wait_for_changes method
            change = await _app_state.nexus_fs.wait_for_changes(
                path=path,
                timeout=timeout,
                _context=context,
            )

            if change is None:
                # Timeout - no changes detected
                return {
                    "changes": [],
                    "timeout": True,
                }

            # Change detected
            return {
                "changes": [change],
                "timeout": False,
            }

        except NotImplementedError as e:
            # No event source available (no Redis, not same-box)
            raise HTTPException(
                status_code=501,
                detail=f"Watch not available: {e}. Requires Redis event bus or same-box backend.",
            ) from None
        except NexusFileNotFoundError:
            raise HTTPException(status_code=404, detail=f"Path not found: {path}") from None
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from None
        except Exception as e:
            logger.error(f"Watch error for {path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Watch error: {e}") from e

    # ========================================================================
    # Streaming Endpoint for Local Backend
    # ========================================================================

    @app.get("/api/stream/{path:path}", tags=["streaming"])
    async def stream_file(
        path: str,
        token: str = Query(..., description="Signed stream token"),
        zone_id: str = Query("default", description="Zone ID"),
    ) -> StreamingResponse:
        """Stream file content directly via HTTP for memory-efficient large file downloads.

        This endpoint is used by the local backend when return_url=True is requested.
        The token is generated by _generate_download_url() and contains a signed
        expiration timestamp for security.

        Args:
            path: Virtual file path (URL-encoded)
            token: Signed stream token from _sign_stream_token()
            zone_id: Zone ID for token verification

        Returns:
            StreamingResponse with file content

        Raises:
            HTTPException 403: Invalid or expired token
            HTTPException 404: File not found
            HTTPException 500: Backend error
        """
        # Verify token
        if not _verify_stream_token(token, f"/{path}", zone_id):
            raise HTTPException(status_code=403, detail="Invalid or expired stream token")

        nexus_fs = _app_state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        try:
            # Get file metadata to retrieve content_hash
            full_path = f"/{path}"

            # Create minimal context for the operation
            from nexus.core.permissions import OperationContext

            context = OperationContext(
                user="system",
                groups=[],
                zone_id=zone_id,
                subject_type="system",
                subject_id="stream",
            )

            # Get metadata (includes content_hash/etag) with timeout (Issue #932)
            meta = await to_thread_with_timeout(nexus_fs.stat, full_path, context=context)
            content_hash = meta.get("etag") or meta.get("content_hash")
            if not content_hash:
                raise HTTPException(status_code=500, detail="File has no content hash")

            # Get the backend for this path
            route = nexus_fs.router.route(full_path)
            backend = route.backend

            # Check if backend supports streaming
            if not hasattr(backend, "stream_content"):
                raise HTTPException(status_code=501, detail="Backend does not support streaming")

            # Create streaming generator
            def generate() -> Iterator[bytes]:
                yield from backend.stream_content(content_hash, context=context)

            # Return streaming response
            return StreamingResponse(
                generate(),
                media_type="application/octet-stream",
                headers={
                    "Content-Length": str(meta.get("size", 0)),
                    "Content-Disposition": f'attachment; filename="{path.split("/")[-1]}"',
                    "X-Content-Hash": content_hash,
                },
            )

        except NexusFileNotFoundError:
            raise HTTPException(status_code=404, detail=f"File not found: /{path}") from None
        except NexusPermissionError as e:
            raise HTTPException(status_code=403, detail=str(e)) from None
        except Exception as e:
            logger.error(f"Stream error for /{path}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Stream error: {e}") from e

    # ========================================================================
    # Share Link Endpoints (Issue #227)
    # ========================================================================

    @app.get("/api/share/{link_id}", tags=["share"])
    async def get_share_link_info(
        link_id: str,
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> JSONResponse:
        """Get share link information.

        Anonymous users get minimal info. Authenticated owners get full details.
        This endpoint does NOT count as an access - use POST /access for that.
        """
        nexus_fs = _app_state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        # Build context if authenticated
        context = None
        if auth_result and auth_result.get("authenticated"):
            context = get_operation_context(auth_result)

        result = await to_thread_with_timeout(nexus_fs.get_share_link, link_id, context=context)

        if not result.success:
            error_msg = (result.error_message or "").lower()
            status_code = 404 if "not found" in error_msg else 400
            raise HTTPException(status_code=status_code, detail=result.error_message or "Error")

        return JSONResponse(content=result.data)

    @app.post("/api/share/{link_id}/access", tags=["share"])
    async def access_share_link(
        link_id: str,
        request: Request,
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> JSONResponse:
        """Access a shared resource via share link.

        This validates the link, checks password if required, logs the access,
        and returns resource info if valid. This DOES count as an access.

        Request body (optional):
            - password: Password if the link is password-protected
        """
        nexus_fs = _app_state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        # Parse request body
        password = None
        try:
            body = await request.json()
            password = body.get("password")
        except Exception:
            pass  # No body or invalid JSON is fine

        # Get client info for logging
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        # Build context if authenticated
        context = None
        if auth_result and auth_result.get("authenticated"):
            context = get_operation_context(auth_result)

        result = await to_thread_with_timeout(
            nexus_fs.access_share_link,
            link_id,
            password=password,
            ip_address=ip_address,
            user_agent=user_agent,
            context=context,
        )

        if not result.success:
            # Map error messages to appropriate HTTP status codes
            error_msg = (result.error_message or "").lower()
            if "not found" in error_msg:
                status_code = 404
            elif "expired" in error_msg or "revoked" in error_msg:
                status_code = 410  # Gone
            elif "password" in error_msg:
                status_code = 401
            elif "limit" in error_msg:
                status_code = 429  # Too Many Requests
            else:
                status_code = 400
            raise HTTPException(
                status_code=status_code, detail=result.error_message or "Access denied"
            )

        return JSONResponse(content=result.data)

    @app.get("/api/share/{link_id}/download", tags=["share"])
    async def download_via_share_link(
        link_id: str,
        request: Request,
        password: str | None = Query(None, description="Password if link is protected"),
        auth_result: dict[str, Any] | None = Depends(get_auth_result),
    ) -> StreamingResponse:
        """Download a file directly via share link.

        Validates the link and streams the file content if valid.
        """
        nexus_fs = _app_state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        # Get client info for logging
        ip_address = request.client.host if request.client else None
        user_agent = request.headers.get("user-agent")

        # Build context if authenticated
        context = None
        if auth_result and auth_result.get("authenticated"):
            context = get_operation_context(auth_result)

        # First validate the share link
        access_result = await to_thread_with_timeout(
            nexus_fs.access_share_link,
            link_id,
            password=password,
            ip_address=ip_address,
            user_agent=user_agent,
            context=context,
        )

        if not access_result.success:
            error_msg = (access_result.error_message or "").lower()
            if "not found" in error_msg:
                status_code = 404
            elif "expired" in error_msg or "revoked" in error_msg:
                status_code = 410
            elif "password" in error_msg:
                status_code = 401
            elif "limit" in error_msg:
                status_code = 429
            else:
                status_code = 400
            raise HTTPException(
                status_code=status_code, detail=access_result.error_message or "Access denied"
            )

        # Get the file path and read permissions from access result
        data = access_result.data or {}
        file_path = data.get("path")
        zone_id = data.get("zone_id", "default")

        if not file_path:
            raise HTTPException(status_code=500, detail="Share link missing file path")

        try:
            # Create context for file access (system context with the link's zone)
            from nexus.core.permissions import OperationContext

            stream_context = OperationContext(
                user="share_link",
                groups=[],
                zone_id=zone_id,
                subject_type="share_link",
                subject_id=link_id,
                is_admin=True,  # Bypass ReBAC - link already validated
            )

            # Get file metadata
            meta = await to_thread_with_timeout(nexus_fs.stat, file_path, context=stream_context)
            content_hash = meta.get("etag") or meta.get("content_hash")
            if not content_hash:
                raise HTTPException(status_code=500, detail="File has no content hash")

            # Get the backend
            route = nexus_fs.router.route(file_path)
            backend = route.backend

            # Check if backend supports streaming
            if not hasattr(backend, "stream_content"):
                # Fall back to read
                content = await to_thread_with_timeout(
                    nexus_fs.read, file_path, context=stream_context
                )
                # Convert to bytes for streaming
                if isinstance(content, str):
                    content_bytes: bytes = content.encode()
                elif isinstance(content, bytes):
                    content_bytes = content
                else:
                    content_bytes = b""
                return StreamingResponse(
                    iter([content_bytes]),
                    media_type="application/octet-stream",
                    headers={
                        "Content-Disposition": f'attachment; filename="{file_path.split("/")[-1]}"',
                    },
                )

            # Create streaming generator
            def generate() -> Iterator[bytes]:
                yield from backend.stream_content(content_hash, context=stream_context)

            return StreamingResponse(
                generate(),
                media_type="application/octet-stream",
                headers={
                    "Content-Length": str(meta.get("size", 0)),
                    "Content-Disposition": f'attachment; filename="{file_path.split("/")[-1]}"',
                    "X-Content-Hash": content_hash,
                },
            )

        except NexusFileNotFoundError:
            raise HTTPException(status_code=404, detail=f"File not found: {file_path}") from None
        except Exception as e:
            logger.error(f"Share link download error for {link_id}: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Download error: {e}") from e

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
                and _app_state.nexus_fs
            ):
                try:
                    # Get ETag from metadata without reading content (fast!)
                    cached_etag = _app_state.nexus_fs.get_etag(params.path, context=context)
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

            # Issue #1187: Handle X-Nexus-Zookie header for consistency
            x_nexus_zookie = request.headers.get("X-Nexus-Zookie")
            if x_nexus_zookie and method in ("read", "list", "glob", "get_metadata", "exists"):
                try:
                    from nexus.core.zookie import (
                        ConsistencyTimeoutError,
                        InvalidZookieError,
                        Zookie,
                    )

                    zookie = Zookie.decode(x_nexus_zookie)
                    # Wait for revision if needed (AT_LEAST_AS_FRESH semantics)
                    if _app_state.nexus_fs:
                        # Get zone from context
                        zone_id = context.zone_id if context else "default"
                        if zone_id != zookie.zone_id:
                            logger.warning(
                                f"Zookie zone mismatch: request={zone_id}, zookie={zookie.zone_id}"
                            )
                        # Wait for revision with 5s timeout
                        if not _app_state.nexus_fs._wait_for_revision(
                            zookie.zone_id, zookie.revision, timeout_ms=5000
                        ):
                            raise ConsistencyTimeoutError(
                                f"Timeout waiting for revision {zookie.revision}",
                                zone_id=zookie.zone_id,
                                requested_revision=zookie.revision,
                                current_revision=_app_state.nexus_fs._get_current_revision(
                                    zookie.zone_id
                                ),
                                timeout_ms=5000,
                            )
                except InvalidZookieError as e:
                    return _error_response(
                        rpc_request.id,
                        RPCErrorCode.INVALID_PARAMS,
                        f"Invalid X-Nexus-Zookie header: {e.message}",
                    )
                except ConsistencyTimeoutError as e:
                    return _error_response(
                        rpc_request.id,
                        RPCErrorCode.INTERNAL_ERROR,
                        f"Consistency timeout: requested revision {e.requested_revision}, "
                        f"current revision {e.current_revision}",
                    )

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
    import hashlib

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
    elif method in ("write", "delete", "rename", "copy", "mkdir", "rmdir", "delta_write"):
        headers["Cache-Control"] = "no-store"
        # Issue #1187: Return zookie in response header for consistency tracking
        # Check for zookie at top level or nested in bytes_written (for write)
        if isinstance(result, dict):
            if "zookie" in result:
                headers["X-Nexus-Zookie"] = result["zookie"]
            elif (
                "bytes_written" in result
                and isinstance(result["bytes_written"], dict)
                and "zookie" in result["bytes_written"]
            ):
                headers["X-Nexus-Zookie"] = result["bytes_written"]["zookie"]

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
    if not _app_state.subscription_manager:
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
        await _app_state.subscription_manager.broadcast(event_type, data, zone_id)
    except Exception as e:
        logger.warning(f"[RPC] Failed to fire event {event_type} for {path}: {e}")


async def _dispatch_method(method: str, params: Any, context: Any) -> Any:
    """Dispatch RPC method call.

    Handles both sync and async methods.
    """
    nexus_fs = _app_state.nexus_fs
    if nexus_fs is None:
        raise RuntimeError("NexusFS not initialized")

    # Methods that need special handling
    MANUAL_METHODS = {
        "read",
        "write",
        "exists",
        "list",
        "delete",
        "rename",
        "copy",
        "mkdir",
        "rmdir",
        "get_metadata",
        "search",
        "glob",
        "grep",
        "is_directory",
        "delta_read",  # Issue #869: Delta sync
        "delta_write",  # Issue #869: Delta sync
        "semantic_search_index",  # Issue #947: HNSW tuning
    }

    # Try auto-dispatch first for exposed methods
    if method in _app_state.exposed_methods and method not in MANUAL_METHODS:
        return await _auto_dispatch(method, params, context)

    # Manual dispatch for core filesystem operations
    # Use to_thread_with_timeout to run sync handlers with timeout (Issue #932)
    # Issue #1115: Fire events after mutation operations
    if method == "read":
        # Use async handler for read to support async parsing
        return await _handle_read_async(params, context)
    elif method == "write":
        result = await to_thread_with_timeout(_handle_write, params, context)
        await _fire_rpc_event("file_write", params.path, context, size=result.get("bytes_written"))
        return result
    elif method == "exists":
        return await to_thread_with_timeout(_handle_exists, params, context)
    elif method == "list":
        return await to_thread_with_timeout(_handle_list, params, context)
    elif method == "delete":
        result = await to_thread_with_timeout(_handle_delete, params, context)
        await _fire_rpc_event("file_delete", params.path, context)
        return result
    elif method == "rename":
        result = await to_thread_with_timeout(_handle_rename, params, context)
        await _fire_rpc_event("file_rename", params.new_path, context, old_path=params.old_path)
        return result
    elif method == "copy":
        return await to_thread_with_timeout(_handle_copy, params, context)
    elif method == "mkdir":
        result = await to_thread_with_timeout(_handle_mkdir, params, context)
        await _fire_rpc_event("dir_create", params.path, context)
        return result
    elif method == "rmdir":
        result = await to_thread_with_timeout(_handle_rmdir, params, context)
        await _fire_rpc_event("dir_delete", params.path, context)
        return result
    elif method == "get_metadata":
        return await to_thread_with_timeout(_handle_get_metadata, params, context)
    elif method == "glob":
        return await to_thread_with_timeout(_handle_glob, params, context)
    elif method == "grep":
        return await to_thread_with_timeout(_handle_grep, params, context)
    elif method == "search":
        return await to_thread_with_timeout(_handle_search, params, context)
    elif method == "is_directory":
        return await to_thread_with_timeout(_handle_is_directory, params, context)
    # Delta sync methods (Issue #869)
    elif method == "delta_read":
        return await to_thread_with_timeout(_handle_delta_read, params, context)
    elif method == "delta_write":
        return await to_thread_with_timeout(_handle_delta_write, params, context)
    # Semantic search methods (Issue #947)
    elif method == "semantic_search_index":
        return await _handle_semantic_search_index(params, context)
    # Memory API methods (Issue #4)
    elif method == "store_memory":
        return await to_thread_with_timeout(_handle_store_memory, params, context)
    elif method == "list_memories":
        return await to_thread_with_timeout(_handle_list_memories, params, context)
    elif method == "query_memories":
        return await to_thread_with_timeout(_handle_query_memories, params, context)
    elif method == "retrieve_memory":
        return await to_thread_with_timeout(_handle_retrieve_memory, params, context)
    elif method == "delete_memory":
        return await to_thread_with_timeout(_handle_delete_memory, params, context)
    elif method == "approve_memory":
        return await to_thread_with_timeout(_handle_approve_memory, params, context)
    elif method == "deactivate_memory":
        return await to_thread_with_timeout(_handle_deactivate_memory, params, context)
    elif method == "approve_memory_batch":
        return await to_thread_with_timeout(_handle_approve_memory_batch, params, context)
    elif method == "deactivate_memory_batch":
        return await to_thread_with_timeout(_handle_deactivate_memory_batch, params, context)
    elif method == "delete_memory_batch":
        return await to_thread_with_timeout(_handle_delete_memory_batch, params, context)
    # Admin API methods (v0.5.1)
    elif method == "admin_create_key":
        return await to_thread_with_timeout(_handle_admin_create_key, params, context)
    elif method == "admin_list_keys":
        return await to_thread_with_timeout(_handle_admin_list_keys, params, context)
    elif method == "admin_get_key":
        return await to_thread_with_timeout(_handle_admin_get_key, params, context)
    elif method == "admin_revoke_key":
        return await to_thread_with_timeout(_handle_admin_revoke_key, params, context)
    elif method == "admin_update_key":
        return await to_thread_with_timeout(_handle_admin_update_key, params, context)
    elif method in _app_state.exposed_methods:
        return await _auto_dispatch(method, params, context)
    else:
        raise ValueError(f"Unknown method: {method}")


async def _auto_dispatch(method: str, params: Any, context: Any) -> Any:
    """Auto-dispatch to exposed method."""
    import inspect

    func = _app_state.exposed_methods[method]

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
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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
    # If result is already a dict (e.g., with metadata), return as-is
    return result


def _handle_write(params: Any, context: Any) -> dict[str, Any]:
    """Handle write method."""
    nexus_fs = _app_state.nexus_fs
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

    bytes_written = nexus_fs.write(params.path, content, **kwargs)
    return {"bytes_written": bytes_written}


def _handle_exists(params: Any, context: Any) -> dict[str, Any]:
    """Handle exists method."""
    nexus_fs = _app_state.nexus_fs
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

    nexus_fs = _app_state.nexus_fs
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
        response = {
            "files": paginated["items"],
            "next_cursor": paginated["next_cursor"],
            "has_more": paginated["has_more"],
            "total_count": paginated.get("total_count"),
        }
        _build_elapsed = (_time.time() - _build_start) * 1000
        _total_elapsed = (_time.time() - _handle_start) * 1000
        logger.info(
            f"[HANDLE-LIST] path={params.path}, list={_list_elapsed:.1f}ms, "
            f"build={_build_elapsed:.1f}ms, total={_total_elapsed:.1f}ms, "
            f"files={len(paginated['items'])}, has_more={paginated['has_more']}"
        )
        return response

    # Fallback for non-paginated result (shouldn't happen)
    _build_start = _time.time()
    entries = result if isinstance(result, list) else []
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
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.delete supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        result = nexus_fs.delete(params.path, context=context)
    except TypeError:
        result = nexus_fs.delete(params.path)

    # Issue #1187: Include zookie in response for consistency tracking
    response: dict[str, Any] = {"deleted": True}
    if isinstance(result, dict):
        if "zookie" in result:
            response["zookie"] = result["zookie"]
        if "revision" in result:
            response["revision"] = result["revision"]
    return response


def _handle_rename(params: Any, context: Any) -> dict[str, Any]:
    """Handle rename method."""
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.rename supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        result = nexus_fs.rename(params.old_path, params.new_path, context=context)
    except TypeError:
        result = nexus_fs.rename(params.old_path, params.new_path)

    # Issue #1187: Include zookie in response for consistency tracking
    response: dict[str, Any] = {"renamed": True}
    if isinstance(result, dict):
        if "zookie" in result:
            response["zookie"] = result["zookie"]
        if "revision" in result:
            response["revision"] = result["revision"]
    return response


def _handle_copy(params: Any, context: Any) -> dict[str, Any]:
    """Handle copy method."""
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    nexus_fs.copy(params.src_path, params.dst_path, context=context)  # type: ignore[attr-defined]
    return {"copied": True}


def _handle_mkdir(params: Any, context: Any) -> dict[str, Any]:
    """Handle mkdir method."""
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    metadata = nexus_fs.get_metadata(params.path, context=context)
    return {"metadata": metadata}


def _handle_glob(params: Any, context: Any) -> dict[str, Any]:
    """Handle glob method."""
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None

    kwargs: dict[str, Any] = {"context": context}
    if hasattr(params, "path") and params.path:
        kwargs["path"] = params.path

    matches = nexus_fs.glob(params.pattern, **kwargs)
    return {"matches": matches}


def _handle_grep(params: Any, context: Any) -> dict[str, Any]:
    """Handle grep method."""
    nexus_fs = _app_state.nexus_fs
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
    # Return "results" key to match RemoteNexusFS.grep() expectations
    return {"results": results}


def _handle_search(params: Any, context: Any) -> dict[str, Any]:
    """Handle search method."""
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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
    nexus_fs = _app_state.nexus_fs
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

    nexus_fs = _app_state.nexus_fs
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

    nexus_fs = _app_state.nexus_fs
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
    from datetime import UTC, datetime, timedelta

    from nexus.core.entity_registry import EntityRegistry
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    _require_admin(context)

    auth_provider = _app_state.auth_provider
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
    from datetime import UTC, datetime

    from sqlalchemy import func, or_, select

    from nexus.storage.models import APIKeyModel

    _require_admin(context)

    auth_provider = _app_state.auth_provider
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

    auth_provider = _app_state.auth_provider
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

    auth_provider = _app_state.auth_provider
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
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from nexus.core.exceptions import NexusFileNotFoundError
    from nexus.storage.models import APIKeyModel

    _require_admin(context)

    auth_provider = _app_state.auth_provider
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
