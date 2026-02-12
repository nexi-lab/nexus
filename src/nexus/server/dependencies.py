"""FastAPI dependencies for authentication and operation context.

This module contains the auth cache, authentication dependency functions
(get_auth_result, require_auth), and the OperationContext factory
(get_operation_context) used by route handlers.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Any

from fastapi import Depends, Header, HTTPException

logger = logging.getLogger(__name__)

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
    # Import _app_state lazily to avoid circular imports
    from nexus.server.fastapi_server import _app_state

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

    # Extract token: support both "Bearer <token>" and raw "sk-<token>" formats
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    elif authorization.startswith("sk-"):
        # API keys (sk-*) can be sent directly without Bearer prefix
        token = authorization
    else:
        return None

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
    # Import _app_state lazily to avoid circular imports
    from nexus.core.permissions import OperationContext
    from nexus.server.fastapi_server import _app_state

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

    # Issue #1240: Populate agent_generation from AgentRegistry
    # TODO: In production, agent_generation should come from client JWT token,
    # not a server-side DB lookup (which makes stale-session detection a no-op
    # since both sides read the same DB). This is a temporary bridge until
    # JWT token integration is implemented.
    agent_generation = None
    if subject_type == "agent" and _app_state.agent_registry:
        try:
            agent_record = _app_state.agent_registry.get(subject_id)
            if agent_record:
                agent_generation = agent_record.generation
        except Exception:
            logger.debug(
                "[AGENT-GEN] Failed to look up generation for agent %s",
                subject_id,
            )

    return OperationContext(
        user=user_id,
        agent_id=agent_id,
        subject_type=subject_type,
        subject_id=subject_id,
        zone_id=zone_id,
        is_admin=is_admin,
        groups=[],
        admin_capabilities=admin_capabilities,
        agent_generation=agent_generation,
    )
