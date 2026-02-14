"""RPC server for Nexus filesystem.

This module implements an HTTP server that exposes all NexusFileSystem
operations through a clean JSON-RPC API. This allows remote clients
(including FUSE mounts) to access Nexus over the network.

The server maps each NexusFilesystem method to an RPC endpoint:
- POST /api/nfs/read
- POST /api/nfs/write
- POST /api/nfs/list
- POST /api/nfs/glob
- etc.

Authentication is done via simple API key in the Authorization header.
"""

from __future__ import annotations

import asyncio
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

if TYPE_CHECKING:
    from nexus import NexusFilesystem
else:
    from nexus.core.filesystem import NexusFilesystem
from nexus.core.exceptions import (
    ConflictError,
    InvalidPathError,
    NexusError,
    NexusFileNotFoundError,
    NexusPermissionError,
    ValidationError,
)
from nexus.core.filters import is_os_metadata_file
from nexus.core.nexus_fs import NexusFS
from nexus.core.virtual_views import (
    add_virtual_views_to_listing,
    get_parsed_content,
    parse_virtual_path,
)
from nexus.server.path_utils import (
    unscope_internal_dict,
    unscope_internal_path,
    unscope_result,
)
from nexus.server.protocol import (
    RPCErrorCode,
    RPCRequest,
    RPCResponse,
    decode_rpc_message,
    encode_rpc_message,
    parse_method_params,
)

logger = logging.getLogger(__name__)


class RPCRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for Nexus RPC API.

    Implements JSON-RPC 2.0 protocol for all NexusFilesystem operations.
    """

    # Class-level attributes set by server
    nexus_fs: NexusFilesystem | NexusFS
    api_key: str | None = None
    auth_provider: Any = None
    exposed_methods: dict[str, Any] = {}
    event_loop: Any = None
    _method_context_params: dict[str, str | None] = {}
    # Per-request auth cache. Thread-safe: ThreadingHTTPServer creates a new
    # handler instance per request, so do_POST/do_GET sets an instance attribute
    # that shadows this class default. Each thread has its own handler.
    _cached_auth_result: Any = None

    # Legacy executor fields kept for backward compatibility (now unused — Issue #1300)
    _async_executor: Any = None
    _async_executor_lock: Any = None

    def log_message(self, format: str, *args: Any) -> None:
        """Override to use Python logging instead of stderr."""
        logger.info(f"{self.address_string()} - {format % args}")

    def _set_cors_headers(self) -> None:
        """Set CORS headers to allow requests from frontend."""
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")

    def do_OPTIONS(self) -> None:
        """Handle OPTIONS requests (CORS preflight)."""
        self.send_response(200)
        self._set_cors_headers()
        self.end_headers()

    def do_POST(self) -> None:
        """Handle POST requests (all RPC methods)."""
        # Clear per-request auth cache (avoids double authenticate() call)
        self._cached_auth_result = None
        try:
            # Parse URL
            parsed = urlparse(self.path)
            path_parts = parsed.path.strip("/").split("/")

            # Check if this is an RPC endpoint
            # Expected: /api/nfs/{method}
            if len(path_parts) != 3 or path_parts[0] != "api" or path_parts[1] != "nfs":
                self._send_error_response(
                    None, RPCErrorCode.INVALID_REQUEST, "Invalid endpoint path"
                )
                return

            method_name = path_parts[2]

            # Validate authentication
            if not self._validate_auth():
                self._send_error_response(
                    None, RPCErrorCode.ACCESS_DENIED, "Invalid or missing API key"
                )
                return

            # Read request body
            content_length = int(self.headers.get("Content-Length", 0))
            if content_length == 0:
                self._send_error_response(None, RPCErrorCode.INVALID_REQUEST, "Empty request body")
                return

            body = self.rfile.read(content_length)

            # Parse JSON-RPC request
            try:
                request_dict = decode_rpc_message(body)
                request = RPCRequest.from_dict(request_dict)
            except Exception as e:
                self._send_error_response(
                    None, RPCErrorCode.PARSE_ERROR, f"Failed to parse request: {e}"
                )
                return

            # Validate method matches URL
            if request.method and request.method != method_name:
                self._send_error_response(
                    request.id,
                    RPCErrorCode.INVALID_REQUEST,
                    f"Method mismatch: URL={method_name}, body={request.method}",
                )
                return

            # Set method from URL if not in body
            if not request.method:
                request.method = method_name

            # Handle RPC call
            self._handle_rpc_call(request)

        except Exception as e:
            logger.exception("Error handling POST request")
            self._send_error_response(None, RPCErrorCode.INTERNAL_ERROR, str(e))

    def do_GET(self) -> None:
        """Handle GET requests (health check, status)."""
        self._cached_auth_result = None
        try:
            parsed = urlparse(self.path)

            # Health check endpoint
            if parsed.path == "/health":
                self._send_json_response(200, {"status": "healthy", "service": "nexus-rpc"})
                return

            # Asyncio debug endpoint (Python 3.14+)
            if parsed.path == "/debug/asyncio":
                self._send_json_response(200, self._get_asyncio_debug_info())
                return

            # Whoami endpoint - returns authenticated user info
            if parsed.path == "/api/auth/whoami":
                # Validate authentication
                if not self._validate_auth():
                    self._send_json_response(
                        401, {"error": "Unauthorized", "message": "Invalid or missing API key"}
                    )
                    return

                # Get authenticated user context
                context = self._get_operation_context()
                if context:
                    self._send_json_response(
                        200,
                        {
                            "authenticated": True,
                            "subject_type": context.subject_type,
                            "subject_id": context.subject_id,
                            "zone_id": context.zone_id,
                            "is_admin": context.is_admin,
                            "user": context.user,  # For backward compatibility
                        },
                    )
                else:
                    self._send_json_response(
                        200,
                        {
                            "authenticated": False,
                            "subject_type": None,
                            "subject_id": None,
                            "zone_id": None,
                            "is_admin": False,
                        },
                    )
                return

            # Status endpoint
            if parsed.path == "/api/nfs/status":
                # Get backend information
                backend_info = self._get_backend_info()

                # Get metadata store information
                metadata_info = self._get_metadata_info()

                self._send_json_response(
                    200,
                    {
                        "status": "running",
                        "service": "nexus-rpc",
                        "version": "1.0",
                        "backend": backend_info,
                        "metadata": metadata_info,
                        "methods": [
                            "read",
                            "write",
                            "delete",
                            "rename",
                            "exists",
                            "list",
                            "glob",
                            "grep",
                            "mkdir",
                            "rmdir",
                            "is_directory",
                            "get_available_namespaces",
                        ],
                    },
                )
                return

            self.send_response(404)
            self.end_headers()

        except Exception:
            logger.exception("Error handling GET request")
            self.send_response(500)
            self.end_headers()

    def _run_async_safe(self, coro: Any) -> Any:
        """Run async coroutine safely in threaded HTTP server context.

        Uses the unified sync_bridge to avoid the ThreadPoolExecutor + asyncio.run()
        anti-pattern (Issue #1300). The sync_bridge shares a single background event
        loop across all threads, eliminating per-call loop creation overhead.

        Args:
            coro: Coroutine to run

        Returns:
            Result of the coroutine
        """
        from nexus.core.sync_bridge import run_sync

        return run_sync(coro)

    def _validate_auth(self) -> bool:
        """Validate API key authentication.

        Caches the auth result on self._cached_auth_result so that
        _get_operation_context() can reuse it without a second authenticate() call.

        Returns:
            True if authentication is valid or not required
        """
        # If no authentication is configured, allow all requests
        if not self.api_key and not self.auth_provider:
            return True

        # Check Authorization header
        auth_header = self.headers.get("Authorization")
        if not auth_header:
            # If auth is configured but no header provided, deny
            return not (self.api_key or self.auth_provider)

        # Expected format: "Bearer <api_key>"
        if not auth_header.startswith("Bearer "):
            return False

        token = auth_header[7:]  # Remove "Bearer " prefix

        # Try auth_provider first (new auth system)
        if self.auth_provider:
            # Use event loop to run async authenticate method
            if self.event_loop is None:
                logger.error("Event loop not initialized for auth provider")
                return False
            result = self._run_async_safe(self.auth_provider.authenticate(token))
            if result is None:
                return False
            # Cache auth result to avoid double authenticate() call
            self._cached_auth_result = result
            return cast(bool, result.authenticated)

        # Fall back to static API key (backward compatibility)
        if self.api_key:
            return bool(token == self.api_key)

        return False

    def _get_operation_context(self) -> Any:
        """Get operation context from authentication.

        Extracts authentication information and creates an OperationContext
        for use in filesystem operations.

        v0.5.0: Added X-Agent-ID header support for user-authenticated agents.

        Returns:
            OperationContext or None if no authentication
        """

        # Extract from auth provider if available
        if self.auth_provider:
            auth_header = self.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                # Reuse cached auth result from _validate_auth() to avoid
                # a second authenticate() call (~10-30ms saved per request)
                result = getattr(self, "_cached_auth_result", None)
                if result is None:
                    token = auth_header[7:]
                    if self.event_loop is None:
                        logger.error("Event loop not initialized on request handler")
                        return None
                    result = self._run_async_safe(self.auth_provider.authenticate(token))
                if result is None:
                    return None
                if result.authenticated and result.subject_type and result.subject_id:
                    # v0.5.0: Check for X-Agent-ID header
                    agent_id = self.headers.get("X-Agent-ID")
                    user_id = result.subject_id

                    # If agent_id provided and subject is user, validate agent ownership
                    if (
                        agent_id
                        and result.subject_type == "user"
                        and hasattr(self.nexus_fs, "entity_registry")
                        and self.nexus_fs.entity_registry
                    ):
                        # Validate agent belongs to user
                        from nexus.core.agents import validate_agent_ownership

                        if not validate_agent_ownership(
                            agent_id, result.subject_id, self.nexus_fs.entity_registry
                        ):
                            logger.warning(
                                f"Agent {agent_id} not owned by user {result.subject_id}, ignoring X-Agent-ID"
                            )
                            agent_id = None

                    # For agent-authenticated requests (via API key), extract user and agent
                    if result.subject_type == "agent":
                        agent_id = result.subject_id
                        # Get user from metadata (owner of the agent key)
                        user_id = result.metadata.get("legacy_user_id", result.subject_id)

                    # Determine the subject for permission checking
                    # If X-Agent-ID header is provided and validated, use agent as subject
                    if agent_id and result.subject_type == "user":
                        # User authenticated with X-Agent-ID header - use agent as subject
                        subject_type = "agent"
                        subject_id = agent_id
                    else:
                        # Direct authentication (user or agent API key)
                        subject_type = result.subject_type
                        subject_id = result.subject_id

                    # Use OperationContext for ReBAC/permission support
                    from nexus.core.permissions import OperationContext

                    # P0-4: Grant zone-scoped admin capabilities to admin users
                    admin_capabilities = set()
                    if result.is_admin:
                        from nexus.services.permissions.permissions_enhanced import AdminCapability

                        # Grant zone-scoped admin capabilities for full zone access
                        # ReBAC enforces zone isolation, so these capabilities are automatically
                        # scoped to the admin's zone (zone_id from context)
                        #
                        # Zone admins can:
                        # - Read/write/delete any file in their zone (READ_ALL, WRITE_ALL, DELETE_ANY)
                        # - Manage ReBAC permissions within their zone (MANAGE_REBAC)
                        #
                        # Zone admins cannot:
                        # - Access system paths (/system/*) - these are system-wide infrastructure
                        # - Manage other zones (MANAGE_ZONES) - system admin only
                        admin_capabilities = {
                            AdminCapability.READ_ALL,  # Read any file in zone (zone-scoped via ReBAC)
                            AdminCapability.WRITE_ALL,  # Write any file in zone (zone-scoped via ReBAC)
                            AdminCapability.DELETE_ANY,  # Delete any file in zone (zone-scoped via ReBAC)
                            AdminCapability.MANAGE_REBAC,  # Manage ReBAC permissions in zone
                            # Excluded: READ_SYSTEM, WRITE_SYSTEM, DELETE_SYSTEM (system paths are system-wide)
                            # Excluded: MANAGE_ZONES (managing other zones is system admin only)
                        }

                    return OperationContext(
                        user=user_id,  # Owner (human user) - LEGACY field
                        agent_id=agent_id,  # v0.5.0: Agent identity (if present)
                        subject_type=subject_type,  # Subject for permission checks
                        subject_id=subject_id,  # Subject ID for permission checks
                        zone_id=result.zone_id,
                        is_admin=result.is_admin,
                        groups=[],  # TODO: Extract groups from auth result if available
                        admin_capabilities=admin_capabilities,  # P0-4: Admin capabilities
                    )

        # Check for explicit subject header (for backward compatibility)
        subject_header = self.headers.get("X-Nexus-Subject")
        if subject_header:
            parts = subject_header.split(":", 1)
            if len(parts) == 2:
                # Use OperationContext for ReBAC/permission support
                from nexus.core.permissions import OperationContext

                return OperationContext(
                    user=parts[1],  # Required
                    subject_type=parts[0],
                    subject_id=parts[1],
                    groups=[],
                    admin_capabilities=set(),
                )

        # No authentication - return None to use default context
        return None

    def _get_memory_api_with_context(self) -> Any:
        """Get Memory API instance with authenticated context.

        Returns:
            Memory API instance with user/agent/zone from authentication
        """
        context = self._get_operation_context()

        # Convert OperationContext to dict format needed by _get_memory_api
        context_dict = {}
        if context:
            if hasattr(context, "zone_id") and context.zone_id:
                context_dict["zone_id"] = context.zone_id
            if hasattr(context, "user_id") and context.user_id:
                context_dict["user_id"] = context.user_id
            if hasattr(context, "agent_id") and context.agent_id:
                context_dict["agent_id"] = context.agent_id

        # Cast to NexusFS since _get_memory_api is only available on NexusFS, not base NexusFilesystem
        return cast(NexusFS, self.nexus_fs)._get_memory_api(context_dict if context_dict else None)

    def _require_admin(self) -> bool:
        """Check if the current request has admin privileges.

        Returns:
            True if admin, False otherwise

        Raises:
            NexusPermissionError: Always raises if not admin
        """
        context = self._get_operation_context()
        if not context or not context.is_admin:
            from nexus.core.exceptions import NexusPermissionError

            raise NexusPermissionError("Admin privileges required for this operation")
        return True

    def _admin_create_key(self, params: Any) -> dict[str, Any]:
        """Create a new API key (admin only).

        Args:
            params: AdminCreateKeyParams

        Returns:
            Dictionary with key details and raw API key
        """
        import uuid
        from datetime import timedelta

        from nexus.server.auth.database_key import DatabaseAPIKeyAuth
        from nexus.services.permissions.entity_registry import EntityRegistry

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        # Auto-generate user_id if not provided
        user_id = params.user_id
        if not user_id:
            user_id = f"user_{uuid.uuid4().hex[:12]}"

        # Register user in entity registry (for agent permission inheritance)
        # This is safe to call multiple times - it returns existing entity if already registered
        if params.subject_type == "user" or not params.subject_type:
            entity_registry = EntityRegistry(self.auth_provider.session_factory)
            entity_registry.register_entity(
                entity_type="user",
                entity_id=user_id,
                parent_type="zone",
                parent_id=params.zone_id,
            )

        # Calculate expiry if specified
        expires_at = None
        if params.expires_days:
            from datetime import UTC, datetime

            expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

        # Create API key
        with self.auth_provider.session_factory() as session:
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

            # Return key details (IMPORTANT: raw_key only shown once!)
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

    def _admin_list_keys(self, params: Any) -> dict[str, Any]:
        """List API keys (admin only).

        Args:
            params: AdminListKeysParams

        Returns:
            Dictionary with list of keys
        """
        from datetime import UTC, datetime

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        with self.auth_provider.session_factory() as session:
            # Build query with filters
            stmt = select(APIKeyModel)

            if params.user_id:
                stmt = stmt.where(APIKeyModel.user_id == params.user_id)

            if params.zone_id:
                stmt = stmt.where(APIKeyModel.zone_id == params.zone_id)

            if params.is_admin is not None:
                stmt = stmt.where(APIKeyModel.is_admin == int(params.is_admin))

            if not params.include_revoked:
                stmt = stmt.where(APIKeyModel.revoked == 0)

            # Apply pagination
            stmt = stmt.limit(params.limit).offset(params.offset)

            # Execute query
            api_keys = list(session.scalars(stmt).all())

            # Filter expired keys if needed
            now = datetime.now(UTC)
            if not params.include_expired:
                api_keys = [
                    key
                    for key in api_keys
                    if not key.expires_at
                    or (
                        key.expires_at.replace(tzinfo=UTC)
                        if key.expires_at.tzinfo is None
                        else key.expires_at
                    )
                    > now
                ]

            # Convert to serializable format (never include key_hash!)
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

            return {"keys": keys, "total": len(keys)}

    def _admin_get_key(self, params: Any) -> dict[str, Any]:
        """Get API key details (admin only).

        Args:
            params: AdminGetKeyParams

        Returns:
            Dictionary with key details
        """
        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        with self.auth_provider.session_factory() as session:
            stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
            api_key = session.scalar(stmt)

            if not api_key:
                raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

            # Return key details (never include key_hash or raw key!)
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

    def _admin_revoke_key(self, params: Any) -> dict[str, Any]:
        """Revoke an API key (admin only).

        Args:
            params: AdminRevokeKeyParams

        Returns:
            Success status
        """
        from nexus.server.auth.database_key import DatabaseAPIKeyAuth

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        with self.auth_provider.session_factory() as session:
            success = DatabaseAPIKeyAuth.revoke_key(session, params.key_id)
            if not success:
                raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

            session.commit()
            return {"success": True, "key_id": params.key_id}

    def _admin_update_key(self, params: Any) -> dict[str, Any]:
        """Update API key properties (admin only).

        Args:
            params: AdminUpdateKeyParams

        Returns:
            Updated key details
        """
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import select

        from nexus.storage.models import APIKeyModel

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        with self.auth_provider.session_factory() as session:
            stmt = select(APIKeyModel).where(APIKeyModel.key_id == params.key_id)
            api_key = session.scalar(stmt)

            if not api_key:
                raise NexusFileNotFoundError(f"API key not found: {params.key_id}")

            # Update fields if provided
            if params.expires_days is not None:
                api_key.expires_at = datetime.now(UTC) + timedelta(days=params.expires_days)

            if params.is_admin is not None:
                # Safety check: prevent self-demotion if this is the last admin key
                if not params.is_admin and api_key.is_admin:
                    # Check if there are other admin keys
                    admin_count_stmt = select(APIKeyModel).where(
                        APIKeyModel.is_admin == 1,
                        APIKeyModel.revoked == 0,
                        APIKeyModel.key_id != params.key_id,
                    )
                    other_admin_keys = list(session.scalars(admin_count_stmt).all())
                    if not other_admin_keys:
                        raise ValidationError(
                            "Cannot remove admin privileges from the last admin key"
                        )

                api_key.is_admin = int(params.is_admin)

            if params.name is not None:
                api_key.name = params.name

            session.commit()

            # Return updated key details
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

    def _admin_gc_versions(self, params: Any) -> dict[str, Any]:
        """Trigger version history garbage collection (admin only, Issue #974).

        Args:
            params: AdminGcVersionsParams with dry_run, retention_days, max_versions

        Returns:
            GC statistics including deleted counts and bytes reclaimed
        """
        from nexus.storage.version_gc import VersionGCSettings, VersionHistoryGC

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        gc = VersionHistoryGC(self.auth_provider.session_factory)
        config = VersionGCSettings.from_env()

        stats = gc.run_gc(
            config=config,
            dry_run=params.dry_run,
            retention_days=params.retention_days,
            max_versions=params.max_versions,
        )

        return stats.to_dict()

    def _admin_gc_versions_stats(self, _params: Any) -> dict[str, Any]:
        """Get version history table statistics (admin only, Issue #974).

        Args:
            params: AdminGcVersionsStatsParams (no parameters)

        Returns:
            Statistics about version_history table size and age
        """
        from nexus.storage.version_gc import VersionGCSettings, VersionHistoryGC

        if not self.auth_provider or not hasattr(self.auth_provider, "session_factory"):
            raise RuntimeError("Database auth provider not configured")

        gc = VersionHistoryGC(self.auth_provider.session_factory)
        stats = gc.get_stats()

        # Add current GC config
        config = VersionGCSettings.from_env()
        stats["gc_config"] = {
            "enabled": config.enabled,
            "retention_days": config.retention_days,
            "max_versions_per_resource": config.max_versions_per_resource,
            "run_interval_hours": config.run_interval_hours,
        }

        return stats

    def _get_asyncio_debug_info(self) -> dict[str, Any]:
        """Get asyncio task introspection information.

        Returns information about running async tasks, including:
        - Total task count
        - Current task info
        - Call graph (Python 3.14+ only)

        Returns:
            Dictionary with asyncio debug information
        """
        import asyncio
        import sys

        result: dict[str, Any] = {
            "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        }

        # Get all running tasks (if event loop is running)
        try:
            loop = asyncio.get_running_loop()
            all_tasks = asyncio.all_tasks(loop)
            current = asyncio.current_task(loop)
            result["event_loop_running"] = True
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
        except RuntimeError:
            # No running event loop (normal for sync RPC server)
            result["event_loop_running"] = False
            result["task_count"] = 0
            result["note"] = "No asyncio event loop running (sync server)"

        # Python 3.14+ call graph introspection
        try:
            from asyncio import format_call_graph  # type: ignore[attr-defined]

            # Format call graph for current task (no args needed)
            result["call_graph_available"] = True
            result["call_graph"] = format_call_graph()
        except ImportError:
            result["call_graph_available"] = False
            result["call_graph_note"] = "Requires Python 3.14+"
        except RuntimeError:
            result["call_graph_available"] = True
            result["call_graph_note"] = "No event loop running"
        except Exception as e:
            result["call_graph_error"] = str(e)

        return result

    def _get_backend_info(self) -> dict[str, Any]:
        """Get backend configuration information.

        Returns:
            Dictionary with backend type and location information
        """
        # Check if filesystem has backend attribute (concrete implementations like NexusFS)
        if not hasattr(self.nexus_fs, "backend"):
            return {"type": "unknown"}

        backend = self.nexus_fs.backend
        backend_type = backend.name

        info: dict[str, Any] = {
            "type": backend_type,
        }

        # Add backend-specific location information
        if backend_type == "local":
            info["location"] = str(backend.root_path)
        elif backend_type == "gcs":
            info["location"] = backend.bucket_name
            info["bucket"] = backend.bucket_name

        return info

    def _get_metadata_info(self) -> dict[str, Any]:
        """Get metadata store configuration information.

        Returns:
            Dictionary with metadata store type and location information
        """
        import os

        # Check if filesystem has metadata attribute (concrete implementations like NexusFS)
        if not hasattr(self.nexus_fs, "metadata"):
            return {"type": "unknown"}

        metadata_store = self.nexus_fs.metadata
        db_type = getattr(metadata_store, "db_type", "sled")

        info: dict[str, Any] = {
            "type": db_type,
        }

        # Add database-specific location information
        if db_type == "sqlite":
            db_path = getattr(metadata_store, "db_path", None)
            info["location"] = str(db_path) if db_path else None
        elif db_type == "postgresql":
            # Check if we're using Cloud SQL via proxy
            cloud_sql_instance = os.getenv("CLOUD_SQL_INSTANCE")

            if cloud_sql_instance:
                # Show Cloud SQL instance info instead of localhost proxy
                info["cloud_sql_instance"] = cloud_sql_instance
                # Parse project, region, and instance name
                parts = cloud_sql_instance.split(":")
                if len(parts) == 3:
                    info["project"] = parts[0]
                    info["region"] = parts[1]
                    info["instance"] = parts[2]

            # Extract database name from URL (only if RecordStore provides it)
            db_url = getattr(metadata_store, "database_url", None)
            if db_url and "@" in db_url and "/" in db_url:
                # Format: postgresql://user:pass@host:port/database
                try:
                    host_part = db_url.split("@")[1]
                    database = host_part.split("/")[1] if "/" in host_part else None
                    if database:
                        info["database"] = database

                    # If no Cloud SQL instance, show the connection host
                    if not cloud_sql_instance:
                        host = host_part.split("/")[0] if "/" in host_part else host_part
                        info["host"] = host
                except (IndexError, AttributeError):
                    pass

        return info

    def _handle_rpc_call(self, request: RPCRequest) -> None:
        """Handle RPC method call.

        Args:
            request: Parsed RPC request
        """
        method = request.method

        try:
            # Parse and validate parameters
            params = parse_method_params(method, request.params)

            # Dispatch to appropriate method
            result = self._dispatch_method(method, params)

            # Send success response
            response = RPCResponse.success(request.id, result)
            self._send_rpc_response(response)

        except ValueError as e:
            # Invalid parameters - expected user error
            self._send_error_response(
                request.id,
                RPCErrorCode.INVALID_PARAMS,
                f"Invalid parameters: {e}",
                is_expected=True,
            )
        except NexusFileNotFoundError as e:
            self._send_error_response(
                request.id,
                RPCErrorCode.FILE_NOT_FOUND,
                str(e),
                data={"path": str(e)},
                is_expected=e.is_expected,
            )
        except FileExistsError as e:
            self._send_error_response(
                request.id, RPCErrorCode.FILE_EXISTS, str(e), is_expected=True
            )
        except InvalidPathError as e:
            self._send_error_response(
                request.id, RPCErrorCode.INVALID_PATH, str(e), is_expected=e.is_expected
            )
        except NexusPermissionError as e:
            self._send_error_response(
                request.id, RPCErrorCode.PERMISSION_ERROR, str(e), is_expected=e.is_expected
            )
        except ValidationError as e:
            self._send_error_response(
                request.id, RPCErrorCode.VALIDATION_ERROR, str(e), is_expected=e.is_expected
            )
        except ConflictError as e:
            # v0.3.9: Handle optimistic concurrency conflicts
            self._send_error_response(
                request.id,
                RPCErrorCode.CONFLICT,
                str(e),
                data={
                    "path": e.path,
                    "expected_etag": e.expected_etag,
                    "current_etag": e.current_etag,
                },
                is_expected=e.is_expected,
            )
        except NexusError as e:
            # Use error classification for logging
            if e.is_expected:
                logger.info(f"Expected error in method {method}: {e}")
            else:
                logger.warning(f"NexusError in method {method}: {e}")
            self._send_error_response(
                request.id,
                RPCErrorCode.INTERNAL_ERROR,
                f"Nexus error: {e}",
                is_expected=e.is_expected,
            )
        except Exception as e:
            # Unexpected system error
            logger.exception(f"Error executing method {method}")
            self._send_error_response(
                request.id, RPCErrorCode.INTERNAL_ERROR, f"Internal error: {e}", is_expected=False
            )

    def _dispatch_method(self, method: str, params: Any) -> Any:
        """Dispatch RPC method to NexusFilesystem.

        Args:
            method: Method name
            params: Parsed parameters

        Returns:
            Method result
        """
        # Try auto-dispatch first (for methods decorated with @rpc_expose)
        # Skip auto-dispatch for methods with special handling (virtual views, wrapping, etc.)
        MANUAL_DISPATCH_METHODS = {
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
            "get_available_namespaces",
        }

        if (
            isinstance(self.exposed_methods, dict)
            and method in self.exposed_methods
            and method not in MANUAL_DISPATCH_METHODS
        ):
            return self._auto_dispatch(method, params)

        # Extract authentication context for manual dispatch
        context = self._get_operation_context()

        # Fall back to manual dispatch for backward compatibility
        # Core file operations
        if method == "read":
            # Check if this is a virtual view request (.txt or .md)
            original_path, view_type = parse_virtual_path(params.path, self.nexus_fs.exists)

            if view_type:
                # Read raw content and parse it (virtual views don't support metadata)
                raw_content = self.nexus_fs.read(original_path, context=context)
                # Type narrowing: when return_metadata=False (default), result is bytes
                assert isinstance(raw_content, bytes), "Expected bytes from read()"
                return get_parsed_content(raw_content, original_path, view_type)
            else:
                # v0.3.9: Support return_metadata parameter
                result = self.nexus_fs.read(
                    params.path, context=context, return_metadata=params.return_metadata
                )
                # Issue #1202: Strip internal prefixes from metadata path
                if isinstance(result, dict):
                    result = unscope_internal_dict(result, ["path", "virtual_path"])
                return result

        elif method == "write":
            # v0.3.9: Support optimistic concurrency control parameters
            result = self.nexus_fs.write(
                params.path,
                params.content,
                context=context,
                if_match=params.if_match,
                if_none_match=params.if_none_match,
                force=params.force,
            )
            # Issue #1202: Strip internal prefixes from response path
            if isinstance(result, dict):
                result = unscope_internal_dict(result, ["path", "virtual_path"])
            return result

        elif method == "append":
            # Append content to file (creates if doesn't exist)
            result = self.nexus_fs.append(
                params.path,
                params.content,
                context=context,
                if_match=params.if_match,
                force=params.force,
            )
            # Issue #1202: Strip internal prefixes from response path
            if isinstance(result, dict):
                result = unscope_internal_dict(result, ["path", "virtual_path"])
            return result

        elif method == "delete":
            self.nexus_fs.delete(params.path, context=context)  # type: ignore[call-arg]
            return {"success": True}

        elif method == "rename":
            self.nexus_fs.rename(params.old_path, params.new_path, context=context)  # type: ignore[call-arg]
            return {"success": True}

        elif method == "exists":
            # Check if this is a virtual view request
            original_path, view_type = parse_virtual_path(params.path, self.nexus_fs.exists)

            if view_type:
                # Virtual view exists if the original file exists
                return {"exists": self.nexus_fs.exists(original_path, context=context)}  # type: ignore[call-arg]
            else:
                return {"exists": self.nexus_fs.exists(params.path, context=context)}  # type: ignore[call-arg]

        # Discovery operations
        elif method == "list":
            files = self.nexus_fs.list(
                params.path,
                recursive=params.recursive,
                details=params.details,
                prefix=params.prefix,
                context=context,
            )
            # Debug: Check what we got
            logger.debug(f"List returned {len(files)} items, type={type(files)}")
            if files:
                logger.debug(f"First item type: {type(files[0])}, value: {files[0]!r}")

            # Convert to serializable format (handle dataclass objects)
            serializable_files = []
            for file in files:
                if isinstance(file, dict | str):
                    serializable_files.append(file)
                else:
                    # Convert dataclass/object to dict
                    logger.warning(f"Found non-serializable object: {type(file)}")
                    if hasattr(file, "__dict__"):
                        serializable_files.append(
                            {
                                k: v
                                for k, v in file.__dict__.items()
                                if not k.startswith("_") and not callable(v)
                            }
                        )
                    else:
                        serializable_files.append(str(file))

            # Filter out OS metadata files (._*, .DS_Store, etc.)
            serializable_files = [
                f
                for f in serializable_files
                if not is_os_metadata_file(f.get("path", "") if isinstance(f, dict) else str(f))
            ]

            # Add virtual views (_parsed.{ext}.md) for parseable files
            # Only add if not recursive (to avoid clutter in full tree listings)
            if not params.recursive:
                serializable_files = add_virtual_views_to_listing(  # type: ignore[assignment]
                    serializable_files,  # type: ignore[arg-type]
                    self.nexus_fs.is_directory,
                    show_parsed=params.show_parsed,
                )

            # Issue #1202: Strip internal zone/tenant prefixes from paths
            # Metadata stores paths with internal prefixes (/tenant:X/, /zone/X/user:Y/)
            # but API clients should see user-friendly paths (/workspace/file.txt)
            serializable_files = [
                unscope_internal_dict(f, ["path", "virtual_path"])
                if isinstance(f, dict)
                else unscope_internal_path(f)
                for f in serializable_files
            ]

            return {"files": serializable_files}

        elif method == "glob":
            matches = self.nexus_fs.glob(params.pattern, params.path, context=context)
            # Issue #1202: Strip internal prefixes from glob matches
            matches = [unscope_internal_path(m) if isinstance(m, str) else m for m in matches]
            return {"matches": matches}

        elif method == "grep":
            results = self.nexus_fs.grep(
                params.pattern,
                path=params.path,
                file_pattern=params.file_pattern,
                ignore_case=params.ignore_case,
                max_results=params.max_results,
                search_mode=params.search_mode,
                context=context,
            )
            # Convert to serializable format
            serializable_results = []
            for result in results:
                if isinstance(result, dict):
                    serializable_results.append(result)
                elif hasattr(result, "__dict__"):
                    serializable_results.append(
                        {
                            k: v
                            for k, v in result.__dict__.items()
                            if not k.startswith("_") and not callable(v)
                        }
                    )
                else:
                    serializable_results.append(str(result))
            # Issue #1202: Strip internal prefixes from grep result paths
            serializable_results = [unscope_result(r) for r in serializable_results]
            return {"results": serializable_results}

        # Directory operations
        elif method == "mkdir":
            self.nexus_fs.mkdir(  # type: ignore[call-arg]
                params.path, parents=params.parents, exist_ok=params.exist_ok, context=context
            )
            return {"success": True}

        elif method == "rmdir":
            self.nexus_fs.rmdir(params.path, recursive=params.recursive, context=context)  # type: ignore[call-arg]
            return {"success": True}

        elif method == "is_directory":
            return {"is_directory": self.nexus_fs.is_directory(params.path, context=context)}

        elif method == "get_available_namespaces":
            import time

            start = time.time()
            logger.warning("[PERF] get_available_namespaces: START")
            namespaces = self.nexus_fs.get_available_namespaces()
            elapsed = time.time() - start
            logger.warning(
                f"[PERF] get_available_namespaces: DONE in {elapsed:.3f}s, returned {len(namespaces)} namespaces: {namespaces}"
            )
            return {"namespaces": namespaces}

        elif method == "get_metadata":
            # Get file metadata
            # Only available for local filesystems with metadata store
            if not hasattr(self.nexus_fs, "metadata"):
                # Return None for remote filesystems or those without metadata
                return {"metadata": None}

            metadata = self.nexus_fs.metadata.get(params.path)
            if metadata is None:
                return {"metadata": None}

            # Check if it's a directory
            is_dir = self.nexus_fs.is_directory(params.path, context=context)

            # Serialize metadata object to dict
            # Note: UNIX-style permissions (owner/group/mode) have been removed
            # All permissions are now managed through ReBAC relationships
            # Issue #1202: Strip internal prefixes from metadata path
            meta_dict = {
                "path": metadata.path,
                "backend_name": metadata.backend_name,
                "physical_path": metadata.physical_path,
                "size": metadata.size,
                "etag": metadata.etag,
                "mime_type": metadata.mime_type,
                "created_at": metadata.created_at,
                "modified_at": metadata.modified_at,
                "version": metadata.version,
                "zone_id": metadata.zone_id,
                "is_directory": is_dir,
            }
            return {"metadata": unscope_internal_dict(meta_dict, ["path"])}

        # ========== Memory API (v0.5.0) ==========
        # Trajectory operations
        elif method == "start_trajectory":
            trajectory_id = self.nexus_fs.memory.start_trajectory(  # type: ignore[attr-defined]
                task_description=params.task_description,
                task_type=params.task_type,
            )
            return {"trajectory_id": trajectory_id}

        elif method == "log_trajectory_step":
            self.nexus_fs.memory.log_step(  # type: ignore[attr-defined]
                trajectory_id=params.trajectory_id,
                step_type=params.step_type,
                description=params.description,
                result=params.result,
            )
            return {"success": True}

        elif method == "complete_trajectory":
            trajectory_id = self.nexus_fs.memory.complete_trajectory(  # type: ignore[attr-defined]
                trajectory_id=params.trajectory_id,
                status=params.status,
                success_score=params.success_score,
                error_message=params.error_message,
            )
            return {"trajectory_id": trajectory_id}

        elif method == "query_trajectories":
            trajectories = self.nexus_fs.memory.query_trajectories(  # type: ignore[attr-defined]
                agent_id=params.agent_id,
                status=params.status,
                limit=params.limit,
            )
            return {"trajectories": trajectories}

        # Playbook operations
        elif method == "get_playbook":
            playbook = self.nexus_fs.memory.get_playbook(playbook_name=params.playbook_name)  # type: ignore[attr-defined]
            return playbook

        elif method == "curate_playbook":
            result = self.nexus_fs.memory.curate_playbook(  # type: ignore[attr-defined]
                reflections=params.reflection_memory_ids,  # Map RPC param to API param
                playbook_name=params.playbook_name,
            )
            return result

        elif method == "query_playbooks":
            playbooks = self.nexus_fs.memory.query_playbooks(  # type: ignore[attr-defined]
                agent_id=params.agent_id,
                scope=params.scope,
                limit=params.limit,
            )
            return {"playbooks": playbooks}

        elif method == "process_relearning":
            results = self.nexus_fs.memory.process_relearning(  # type: ignore[attr-defined]
                limit=params.limit,
            )
            return {"results": results}

        # Reflection operations
        elif method == "batch_reflect":
            result = self.nexus_fs.memory.batch_reflect(  # type: ignore[attr-defined]
                agent_id=params.agent_id,
                since=params.since,
                min_trajectories=params.min_trajectories,
                task_type=params.task_type,
            )
            return result

        # Memory storage operations
        elif method == "store_memory":
            # v0.7.1+v0.8.0: Use memory API with authenticated context
            memory_api = self._get_memory_api_with_context()
            memory_id = memory_api.store(
                content=params.content,
                memory_type=params.memory_type,
                scope=params.scope,
                importance=params.importance,
                namespace=params.namespace,  # v0.8.0
                path_key=params.path_key,  # v0.8.0
                state=params.state,  # #368
                # Note: tags param in RPC but not in Memory.store() - ignore it
            )
            return {"memory_id": memory_id}

        elif method == "list_memories":
            # v0.7.1+v0.8.0: Use memory API with authenticated context
            memory_api = self._get_memory_api_with_context()
            memories = memory_api.list(
                scope=params.scope,
                memory_type=params.memory_type,
                namespace=params.namespace,  # v0.8.0
                namespace_prefix=params.namespace_prefix,  # v0.8.0
                state=params.state,  # #368
                limit=params.limit,
            )
            return {"memories": memories}

        elif method == "retrieve_memory":  # v0.8.0
            memory_api = self._get_memory_api_with_context()
            memory = memory_api.retrieve(
                namespace=params.namespace,
                path_key=params.path_key,
                path=params.path,
            )
            return {"memory": memory}

        elif method == "delete_memory":  # v0.8.0
            memory_api = self._get_memory_api_with_context()
            deleted = memory_api.delete(params.memory_id)
            return {"deleted": deleted}

        elif method == "approve_memory":  # #368
            memory_api = self._get_memory_api_with_context()
            approved = memory_api.approve(params.memory_id)
            return {"approved": approved}

        elif method == "deactivate_memory":  # #368
            memory_api = self._get_memory_api_with_context()
            deactivated = memory_api.deactivate(params.memory_id)
            return {"deactivated": deactivated}

        elif method == "approve_memory_batch":  # #368
            memory_api = self._get_memory_api_with_context()
            result = memory_api.approve_batch(params.memory_ids)
            return result

        elif method == "deactivate_memory_batch":  # #368
            memory_api = self._get_memory_api_with_context()
            result = memory_api.deactivate_batch(params.memory_ids)
            return result

        elif method == "delete_memory_batch":  # #368
            memory_api = self._get_memory_api_with_context()
            result = memory_api.delete_batch(params.memory_ids)
            return result

        elif method == "query_memories":
            # v0.7.1+v0.8.0: Use memory API with authenticated context
            memory_api = self._get_memory_api_with_context()

            # #406: Support semantic search if query is provided
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
                    state=params.state,  # #368
                    limit=params.limit,
                )
            return {"memories": memories}

        # ========== Admin API (v0.5.1) ==========
        elif method == "admin_create_key":
            self._require_admin()
            return self._admin_create_key(params)

        elif method == "admin_list_keys":
            self._require_admin()
            return self._admin_list_keys(params)

        elif method == "admin_get_key":
            self._require_admin()
            return self._admin_get_key(params)

        elif method == "admin_revoke_key":
            self._require_admin()
            return self._admin_revoke_key(params)

        elif method == "admin_update_key":
            self._require_admin()
            return self._admin_update_key(params)

        elif method == "admin_gc_versions":
            self._require_admin()
            return self._admin_gc_versions(params)

        elif method == "admin_gc_versions_stats":
            self._require_admin()
            return self._admin_gc_versions_stats(params)

        else:
            raise ValueError(f"Unknown method: {method}")

    def _auto_dispatch(self, method: str, params: Any) -> Any:
        """Auto-dispatch to decorated method.

        Args:
            method: Method name
            params: Parsed parameters (dataclass instance)

        Returns:
            Serialized method result
        """
        logger.debug(f"[AUTO-DISPATCH] method={method}")
        fn = self.exposed_methods[method]

        # Convert params dataclass to kwargs dict
        if hasattr(params, "__dict__"):
            kwargs = {k: v for k, v in params.__dict__.items() if not k.startswith("_")}
        else:
            kwargs = {}

        # Use cached signature analysis (populated during _discover_exposed_methods)
        # to avoid calling inspect.signature() on every request
        context_param_name = self._method_context_params.get(method)

        if context_param_name:
            context = self._get_operation_context()
            logger.debug(f"[AUTO-DISPATCH] method={method}, context_param={context_param_name}")
            if context is not None:
                kwargs[context_param_name] = context

        # Call the method
        result = fn(**kwargs)

        # Check if result is a coroutine (async method) and await it
        import inspect

        if inspect.iscoroutine(result):
            result = self._run_async_safe(result)

        # Serialize the result
        return self._serialize_result(result)

    def _serialize_result(self, result: Any) -> Any:
        """Serialize method result for RPC response.

        Handles common return types and converts them to JSON-serializable format.

        Args:
            result: Method return value

        Returns:
            JSON-serializable result
        """
        # Handle None
        if result is None:
            return {"success": True}

        # Handle bytes (already serialized by RPCEncoder)
        if isinstance(result, bytes):
            return result

        # Handle dict (already serializable, just validate nested objects)
        if isinstance(result, dict):
            return self._serialize_dict(result)

        # Handle list
        if isinstance(result, list):
            return [self._serialize_result(item) for item in result]

        # Handle dataclass or object with __dict__
        if hasattr(result, "__dict__"):
            return self._serialize_dict(
                {
                    k: v
                    for k, v in result.__dict__.items()
                    if not k.startswith("_") and not callable(v)
                }
            )

        # Handle primitives (str, int, float, bool)
        if isinstance(result, (str, int, float, bool)):
            return result

        # Default: convert to string
        return str(result)

    def _serialize_dict(self, data: dict[str, Any]) -> dict[str, Any]:
        """Recursively serialize dictionary values.

        Args:
            data: Dictionary to serialize

        Returns:
            Serialized dictionary
        """
        serialized = {}
        for key, value in data.items():
            if (
                isinstance(value, (dict, list))
                or hasattr(value, "__dict__")
                and not callable(value)
            ):
                serialized[key] = self._serialize_result(value)
            else:
                serialized[key] = value
        return serialized

    def _send_rpc_response(self, response: RPCResponse) -> None:
        """Send RPC response with optional compression (zstd preferred, gzip fallback).

        Args:
            response: RPC response object
        """
        from nexus.server.compression import compress_response

        response_dict = response.to_dict()
        body = encode_rpc_message(response_dict)

        # Compress response if client supports it (prefers zstd over gzip)
        accept_encoding = self.headers.get("Accept-Encoding", "")
        body, content_encoding = compress_response(body, accept_encoding)

        self.send_response(200)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()  # Ensure all data is sent before connection closes

    def _send_error_response(
        self,
        request_id: str | int | None,
        code: RPCErrorCode,
        message: str,
        data: Any = None,
        is_expected: bool = False,
    ) -> None:
        """Send error response.

        Args:
            request_id: Request ID (if available)
            code: Error code
            message: Error message
            data: Optional error data
            is_expected: Whether this is an expected error (user error) vs
                        unexpected (system error). Used for logging/alerting.
        """
        response = RPCResponse.create_error(request_id, code, message, data, is_expected)
        self._send_rpc_response(response)

    def _send_json_response(self, status_code: int, data: dict[str, Any]) -> None:
        """Send JSON response with optional compression (zstd preferred, gzip fallback).

        Args:
            status_code: HTTP status code
            data: Response data
        """
        from nexus.server.compression import compress_response

        body = encode_rpc_message(data)

        # Compress response if client supports it (prefers zstd over gzip)
        accept_encoding = self.headers.get("Accept-Encoding", "")
        body, content_encoding = compress_response(body, accept_encoding)

        self.send_response(status_code)
        self._set_cors_headers()
        self.send_header("Content-Type", "application/json")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()  # Ensure all data is sent before connection closes


class NexusRPCServer:
    """RPC server for Nexus filesystem.

    Provides JSON-RPC endpoints for all NexusFilesystem operations.
    """

    def __init__(
        self,
        nexus_fs: NexusFilesystem,
        host: str = "0.0.0.0",
        port: int = 2026,
        api_key: str | None = None,
        auth_provider: Any = None,
    ):
        """Initialize server.

        Args:
            nexus_fs: Nexus filesystem instance
            host: Server host
            port: Server port
            api_key: Optional API key for authentication (if None, no auth required)
            auth_provider: Optional authentication provider
        """
        self.nexus_fs = nexus_fs
        self.host = host
        self.port = port
        self.api_key = api_key
        self.auth_provider = auth_provider
        self._event_loop = asyncio.new_event_loop()

        # Auto-discover all @rpc_expose decorated methods
        self._exposed_methods = self._discover_exposed_methods()

        # Create HTTP server
        # Use ThreadingHTTPServer to handle concurrent requests (fixes FUSE deadlock issue #391)
        self.server = ThreadingHTTPServer((host, port), RPCRequestHandler)

        # Configure handler
        RPCRequestHandler.nexus_fs = nexus_fs
        RPCRequestHandler.api_key = api_key
        RPCRequestHandler.auth_provider = auth_provider
        RPCRequestHandler.exposed_methods = self._exposed_methods
        RPCRequestHandler.event_loop = self._event_loop

    def _discover_exposed_methods(self) -> dict[str, Any]:
        """Discover all methods marked with @rpc_expose decorator.

        Also pre-caches which methods accept a context parameter, so that
        _auto_dispatch() avoids calling inspect.signature() on every request.

        Returns:
            Dictionary mapping method names to callable methods
        """
        import inspect

        exposed = {}
        context_params: dict[str, str | None] = {}

        logger.debug(f"Starting method discovery on {type(self.nexus_fs).__name__}")

        # Iterate through all attributes of the NexusFS instance
        dir_names = dir(self.nexus_fs)

        for name in dir_names:
            # Skip private methods
            if name.startswith("_"):
                continue

            try:
                attr = getattr(self.nexus_fs, name)

                # Check if it's callable and has the _rpc_exposed marker
                if callable(attr) and hasattr(attr, "_rpc_exposed"):
                    method_name = getattr(attr, "_rpc_name", name)
                    exposed[method_name] = attr

                    # Cache context parameter name for this method
                    sig = inspect.signature(attr)
                    if "context" in sig.parameters:
                        context_params[method_name] = "context"
                    elif "_context" in sig.parameters:
                        context_params[method_name] = "_context"

                    logger.debug(f"Discovered RPC method: {method_name}")

            except Exception as e:
                logger.debug(f"Skipping attribute {name}: {e}")
                continue

        # Store context param cache on the handler CLASS for _auto_dispatch
        # (this method runs on NexusRPCServer, but the cache is read by RPCRequestHandler)
        RPCRequestHandler._method_context_params = context_params

        logger.debug(f"Auto-discovered {len(exposed)} RPC methods")
        return exposed

    def serve_forever(self) -> None:
        """Start server and handle requests."""
        logger.info(f"Starting Nexus RPC server on {self.host}:{self.port}")
        logger.info(f"Endpoint: http://{self.host}:{self.port}/api/nfs/{{method}}")

        # Check both authentication methods
        if self.auth_provider:
            logger.info(f"Authentication: Database provider ({type(self.auth_provider).__name__})")
        elif self.api_key:
            logger.info("Authentication: Static API key")
        else:
            logger.info("Authentication: None (open access)")

        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Server stopped by user")
            self.shutdown()

    def shutdown(self) -> None:
        """Shutdown server gracefully."""
        logger.info("Shutting down server...")
        self.server.shutdown()
        self.server.server_close()

        # Cleanup shared async executor
        if RPCRequestHandler._async_executor is not None:
            RPCRequestHandler._async_executor.shutdown(wait=True)
            RPCRequestHandler._async_executor = None
            logger.debug("Shared async executor shutdown complete")

        if hasattr(self.nexus_fs, "close"):
            self.nexus_fs.close()
        logger.info("Server stopped")
