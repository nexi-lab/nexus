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
import hmac
import logging
import os
import secrets
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any, TypeVar

from anyio import to_thread
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
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


class WhoamiResponse(BaseModel):
    """Authentication info response."""

    authenticated: bool
    subject_type: str | None = None
    subject_id: str | None = None
    tenant_id: str | None = None
    is_admin: bool = False
    inherit_permissions: bool = True  # v0.5.1: Whether agent inherits owner's permissions
    user: str | None = None


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
        self.auth_provider: Any = None
        self.api_key: str | None = None
        self.exposed_methods: dict[str, Any] = {}
        self.async_rebac_manager: Any = None
        self.database_url: str | None = None
        self.subscription_manager: Any = None  # SubscriptionManager for webhooks
        # Thread pool and timeout settings (Issue #932)
        self.thread_pool_size: int = 200
        self.operation_timeout: float = 30.0


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


def _sign_stream_token(path: str, expires_in: int, tenant_id: str = "default") -> str:
    """Generate a signed token for streaming access to a file.

    Token format: {expires_at}.{signature}
    Where signature = HMAC-SHA256(path:expires_at:tenant_id)[:16]

    Args:
        path: Virtual file path
        expires_in: Token validity in seconds
        tenant_id: Tenant ID for isolation

    Returns:
        Signed token string
    """
    expires_at = int(time.time()) + expires_in
    payload = f"{path}:{expires_at}:{tenant_id}"
    signature = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]
    return f"{expires_at}.{signature}"


def _verify_stream_token(token: str, path: str, tenant_id: str = "default") -> bool:
    """Verify a stream token is valid and not expired.

    Args:
        token: Token string from _sign_stream_token
        path: Virtual file path (must match token)
        tenant_id: Tenant ID (must match token)

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
        payload = f"{path}:{expires_at}:{tenant_id}"
        expected_sig = hmac.new(_get_stream_secret(), payload.encode(), "sha256").hexdigest()[:16]

        return hmac.compare_digest(signature, expected_sig)
    except (ValueError, TypeError):
        return False


# ============================================================================
# Dependencies
# ============================================================================


async def get_auth_result(
    authorization: str | None = Header(None, alias="Authorization"),
    x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
    x_nexus_tenant_id: str | None = Header(None, alias="X-Nexus-Tenant-ID"),
) -> dict[str, Any] | None:
    """Validate authentication and return auth result.

    Args:
        authorization: Bearer token from Authorization header
        x_agent_id: Optional agent ID header
        x_nexus_subject: Optional identity hint header (e.g., "user:alice")
        x_nexus_tenant_id: Optional tenant hint header

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
        tenant_id: str | None = x_nexus_tenant_id

        if x_nexus_subject:
            st, sid = _parse_subject_header(x_nexus_subject)
            subject_type, subject_id = st, sid
        elif authorization and authorization.startswith("Bearer "):
            token = authorization[7:]
            # Best-effort: infer tenant/user from DatabaseAPIKeyAuth format
            # Format: sk-<tenant>_<user>_<id>_<random-hex>
            if token.startswith("sk-"):
                remainder = token[len("sk-") :]
                parts = remainder.split("_")
                if len(parts) >= 2:
                    inferred_tenant = parts[0] or None
                    inferred_user = parts[1] or None
                    tenant_id = tenant_id or inferred_tenant
                    subject_type = "user"
                    subject_id = inferred_user

        return {
            "authenticated": True,
            "is_admin": False,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "tenant_id": tenant_id,
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
        result = await _app_state.auth_provider.authenticate(token)
        if result is None:
            return None
        return {
            "authenticated": result.authenticated,
            "is_admin": result.is_admin,
            "subject_type": result.subject_type,
            "subject_id": result.subject_id,
            "tenant_id": result.tenant_id,
            "inherit_permissions": result.inherit_permissions
            if hasattr(result, "inherit_permissions")
            else True,
            "metadata": result.metadata if hasattr(result, "metadata") else {},
            "x_agent_id": x_agent_id,
        }

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
    tenant_id = auth_result.get("tenant_id") or "default"
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
        tenant_id=tenant_id,
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
        except Exception as e:
            logger.warning(f"Failed to initialize async ReBAC manager: {e}")

    # Tiger Cache queue processor (Issue #935)
    # NOTE: Disabled by default - write-through handles grants/revokes immediately
    # Enable with NEXUS_ENABLE_TIGER_WORKER=true for cache warming scenarios
    tiger_task: asyncio.Task | None = None
    if _app_state.nexus_fs and os.getenv("NEXUS_ENABLE_TIGER_WORKER", "false").lower() in ("true", "1", "yes"):
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

    yield

    # Cleanup
    logger.info("Shutting down FastAPI Nexus server...")

    # Cancel Tiger Cache task
    if tiger_task:
        tiger_task.cancel()
        with suppress(asyncio.CancelledError):
            await tiger_task
        logger.info("Tiger Cache queue processor stopped")

    if _app_state.subscription_manager:
        await _app_state.subscription_manager.close()
    if _app_state.nexus_fs and hasattr(_app_state.nexus_fs, "close"):
        _app_state.nexus_fs.close()


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
        if hasattr(nexus_fs, "metadata") and hasattr(nexus_fs.metadata, "SessionLocal"):
            from nexus.server.subscriptions import SubscriptionManager

            _app_state.subscription_manager = SubscriptionManager(nexus_fs.metadata.SessionLocal)
            # Inject into NexusFS for automatic event broadcasting
            nexus_fs.subscription_manager = _app_state.subscription_manager
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

    # Register routes
    _register_routes(app)

    # Initialize OAuth provider if credentials are available
    _initialize_oauth_provider(nexus_fs, auth_provider, database_url)

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
        elif hasattr(nexus_fs, "metadata") and hasattr(nexus_fs.metadata, "SessionLocal"):
            from sqlalchemy.orm import sessionmaker

            # Create session factory from metadata
            session_factory = sessionmaker(bind=nexus_fs.metadata.engine)
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


def _register_routes(app: FastAPI) -> None:
    """Register all routes."""

    # Health check
    @app.get("/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        return HealthResponse(status="healthy", service="nexus-rpc")

    # Authentication routes
    try:
        from nexus.server.auth.auth_routes import router as auth_router

        app.include_router(auth_router)
        logger.info("Authentication routes registered")
    except ImportError as e:
        logger.warning(f"Failed to import auth routes: {e}. OAuth endpoints will not be available.")

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
            tenant_id=auth_result.get("tenant_id"),
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
        tenant_id = auth_result.get("tenant_id") or "default"
        created_by = auth_result.get("subject_id")

        subscription = _app_state.subscription_manager.create(
            tenant_id=tenant_id,
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
        """List webhook subscriptions for the current tenant."""
        if not _app_state.subscription_manager:
            raise HTTPException(status_code=503, detail="Subscription manager not available")

        tenant_id = auth_result.get("tenant_id") or "default"
        subscriptions = _app_state.subscription_manager.list_subscriptions(
            tenant_id=tenant_id,
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

        tenant_id = auth_result.get("tenant_id") or "default"
        subscription = _app_state.subscription_manager.get(subscription_id, tenant_id)
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
        tenant_id = auth_result.get("tenant_id") or "default"

        subscription = _app_state.subscription_manager.update(
            subscription_id=subscription_id,
            tenant_id=tenant_id,
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

        tenant_id = auth_result.get("tenant_id") or "default"
        deleted = _app_state.subscription_manager.delete(subscription_id, tenant_id)
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

        tenant_id = auth_result.get("tenant_id") or "default"
        result = await _app_state.subscription_manager.test(subscription_id, tenant_id)
        return JSONResponse(content=result)

    # ========================================================================
    # Streaming Endpoint for Local Backend
    # ========================================================================

    @app.get("/api/stream/{path:path}", tags=["streaming"])
    async def stream_file(
        path: str,
        token: str = Query(..., description="Signed stream token"),
        tenant_id: str = Query("default", description="Tenant ID"),
    ) -> StreamingResponse:
        """Stream file content directly via HTTP for memory-efficient large file downloads.

        This endpoint is used by the local backend when return_url=True is requested.
        The token is generated by _generate_download_url() and contains a signed
        expiration timestamp for security.

        Args:
            path: Virtual file path (URL-encoded)
            token: Signed stream token from _sign_stream_token()
            tenant_id: Tenant ID for token verification

        Returns:
            StreamingResponse with file content

        Raises:
            HTTPException 403: Invalid or expired token
            HTTPException 404: File not found
            HTTPException 500: Backend error
        """
        # Verify token
        if not _verify_stream_token(token, f"/{path}", tenant_id):
            raise HTTPException(status_code=403, detail="Invalid or expired stream token")

        nexus_fs = _app_state.nexus_fs
        if nexus_fs is None:
            raise HTTPException(status_code=503, detail="NexusFS not available")

        try:
            # Get file metadata to retrieve content_hash
            full_path = f"/{path}"

            # Create minimal context for the operation
            from nexus.core.context import OperationContext

            context = OperationContext(
                tenant_id=tenant_id,
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

    # Main RPC endpoint
    @app.post("/api/nfs/{method}")
    async def rpc_endpoint(
        method: str,
        request: Request,
        auth_result: dict[str, Any] = Depends(require_auth),
    ) -> Response:
        """Handle RPC method calls."""
        try:
            # Parse request body using decode_rpc_message to handle bytes encoding
            body_bytes = await request.body()
            body = decode_rpc_message(body_bytes) if body_bytes else {}
            rpc_request = RPCRequest.from_dict(body)

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

            # Dispatch method
            result = await _dispatch_method(method, params, context)

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
            success_response = {
                "jsonrpc": "2.0",
                "id": rpc_request.id,
                "result": result,
            }
            # encode_rpc_message handles bytes, datetime, etc.
            encoded = encode_rpc_message(success_response)

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
    if method == "read":
        # Use async handler for read to support async parsing
        return await _handle_read_async(params, context)
    elif method == "write":
        return await to_thread_with_timeout(_handle_write, params, context)
    elif method == "exists":
        return await to_thread_with_timeout(_handle_exists, params, context)
    elif method == "list":
        return await to_thread_with_timeout(_handle_list, params, context)
    elif method == "delete":
        return await to_thread_with_timeout(_handle_delete, params, context)
    elif method == "rename":
        return await to_thread_with_timeout(_handle_rename, params, context)
    elif method == "copy":
        return await to_thread_with_timeout(_handle_copy, params, context)
    elif method == "mkdir":
        return await to_thread_with_timeout(_handle_mkdir, params, context)
    elif method == "rmdir":
        return await to_thread_with_timeout(_handle_rmdir, params, context)
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
        return await to_thread_with_timeout(func, **kwargs)


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
            # Get tenant_id from context
            tenant_id = "default"
            if context and hasattr(context, "tenant_id"):
                tenant_id = context.tenant_id or "default"

            # Generate signed token for streaming access
            token = _sign_stream_token(path, expires_in, tenant_id)

            # URL-encode the path (remove leading slash for URL construction)
            from urllib.parse import quote

            encoded_path = quote(path.lstrip("/"), safe="")

            return {
                "download_url": f"/api/stream/{encoded_path}?token={token}&tenant_id={tenant_id}",
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
    limit = getattr(params, "limit", None)
    cursor = getattr(params, "cursor", None)

    if limit is not None:
        # Paginated mode - pass limit and cursor to list()
        kwargs["limit"] = limit
        if cursor:
            kwargs["cursor"] = cursor

        result = nexus_fs.list(params.path, **kwargs)

        # Result is PaginatedResult when limit is provided
        if hasattr(result, "to_dict"):
            paginated = result.to_dict()
            return {
                "files": paginated["items"],
                "next_cursor": paginated["next_cursor"],
                "has_more": paginated["has_more"],
                "total_count": paginated.get("total_count"),
            }

    # Legacy mode - return flat list
    entries = nexus_fs.list(params.path, **kwargs)
    # Client expects "files" key, not "entries"
    return {"files": entries}


def _handle_delete(params: Any, context: Any) -> dict[str, Any]:
    """Handle delete method."""
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.delete supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        nexus_fs.delete(params.path, context=context)
    except TypeError:
        nexus_fs.delete(params.path)
    return {"deleted": True}


def _handle_rename(params: Any, context: Any) -> dict[str, Any]:
    """Handle rename method."""
    nexus_fs = _app_state.nexus_fs
    assert nexus_fs is not None
    # IMPORTANT: NexusFS.rename supports context and permissions depend on it.
    # Some older NexusFilesystem implementations may not accept context, so fall back safely.
    try:
        nexus_fs.rename(params.old_path, params.new_path, context=context)
    except TypeError:
        nexus_fs.rename(params.old_path, params.new_path)
    return {"renamed": True}


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
    from datetime import UTC, datetime, timedelta

    from nexus.core.entity_registry import EntityRegistry
    from nexus.server.auth.database_key import DatabaseAPIKeyAuth

    _require_admin(context)

    auth_provider = _app_state.auth_provider
    if not auth_provider or not hasattr(auth_provider, "session_factory"):
        raise RuntimeError("Database auth provider not configured")

    # Register user in entity registry (for agent permission inheritance)
    if params.subject_type == "user" or not params.subject_type:
        entity_registry = EntityRegistry(auth_provider.session_factory)
        entity_registry.register_entity(
            entity_type="user",
            entity_id=params.user_id,
            parent_type="tenant",
            parent_id=params.tenant_id,
        )

    # Calculate expiry if specified
    expires_at = None
    if params.expires_days:
        expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

    # Create API key
    with auth_provider.session_factory() as session:
        key_id, raw_key = DatabaseAPIKeyAuth.create_key(
            session,
            user_id=params.user_id,
            name=params.name,
            subject_type=params.subject_type,
            subject_id=params.subject_id,
            tenant_id=params.tenant_id,
            is_admin=params.is_admin,
            expires_at=expires_at,
        )
        session.commit()

        return {
            "key_id": key_id,
            "api_key": raw_key,
            "user_id": params.user_id,
            "name": params.name,
            "subject_type": params.subject_type,
            "subject_id": params.subject_id or params.user_id,
            "tenant_id": params.tenant_id,
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
        if params.tenant_id:
            stmt = stmt.where(APIKeyModel.tenant_id == params.tenant_id)
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
                    "tenant_id": key.tenant_id,
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
            "tenant_id": api_key.tenant_id,
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
