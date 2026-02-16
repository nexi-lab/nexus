"""FastAPI dependencies for authentication and operation context.

This module contains the auth cache, authentication dependency functions
(get_auth_result, require_auth), and the OperationContext factory
(get_operation_context) used by route handlers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from typing import Any

from cachetools import TTLCache
from fastapi import Depends, Header, HTTPException, Request

from nexus.server.token_utils import parse_sk_token

logger = logging.getLogger(__name__)

# Auth cache: token_hash -> auth result dict
# TTL: 15 minutes (900 seconds) - balances performance vs permission freshness
# Max 1000 entries - cachetools handles eviction automatically (LRU when full)
_AUTH_CACHE_TTL = 900
_AUTH_CACHE_MAX_SIZE = 1000
_AUTH_CACHE: TTLCache[str, dict[str, Any]] = TTLCache(
    maxsize=_AUTH_CACHE_MAX_SIZE, ttl=_AUTH_CACHE_TTL
)


def _get_cached_auth(token: str) -> dict[str, Any] | None:
    """Get cached auth result if valid. Returns a copy to prevent mutation."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
    cached = _AUTH_CACHE.get(token_hash)
    if cached is not None:
        # Return a shallow copy to prevent callers from mutating the cached entry
        return dict(cached)
    return None


def _set_cached_auth(token: str, result: dict[str, Any]) -> None:
    """Cache auth result with TTL."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()[:32]
    _AUTH_CACHE[token_hash] = result


def _reset_auth_cache() -> None:
    """Reset the auth cache. Used by tests for isolation."""
    _AUTH_CACHE.clear()


# NEXUS_STATIC_ADMINS: comma-separated subject IDs that get admin privileges
# in open access mode (no api_key, no auth_provider). Parsed once at import.
# WARNING: In open access mode, identity comes from unauthenticated headers.
# This should ONLY be used in development/testing, never in production.
_STATIC_ADMINS_CSV = os.environ.get("NEXUS_STATIC_ADMINS", "")
_STATIC_ADMINS: frozenset[str] = frozenset(
    a.strip() for a in _STATIC_ADMINS_CSV.split(",") if a.strip()
)
if _STATIC_ADMINS:
    logger.warning(
        "[AUTH] NEXUS_STATIC_ADMINS configured: %s. "
        "Grants admin privileges in open access mode. DO NOT use in production.",
        _STATIC_ADMINS,
    )


async def resolve_auth(
    app_state: Any,
    authorization: str | None = None,
    x_agent_id: str | None = None,
    x_nexus_subject: str | None = None,
    x_nexus_zone_id: str | None = None,
) -> dict[str, Any] | None:
    """Core authentication logic — usable from both HTTP and WebSocket contexts.

    Args:
        app_state: Application state (request.app.state or websocket.app.state).
        authorization: Bearer token or raw sk- token.
        x_agent_id: Optional agent ID.
        x_nexus_subject: Optional identity hint (e.g., "user:alice").
        x_nexus_zone_id: Optional zone hint.

    Returns:
        Auth result dict or None if not authenticated.
    """
    _state = app_state

    def _parse_subject_header(value: str) -> tuple[str | None, str | None]:
        parts = value.split(":", 1)
        if len(parts) != 2:
            return (None, None)
        subject_type, subject_id = parts[0].strip(), parts[1].strip()
        if not subject_type or not subject_id:
            return (None, None)
        return (subject_type, subject_id)

    # No auth configured = open access
    if not getattr(_state, "api_key", None) and not getattr(_state, "auth_provider", None):
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
            parsed = parse_sk_token(token)
            if parsed is not None:
                zone_id = zone_id or parsed.zone
                subject_type = "user"
                subject_id = parsed.user

        is_admin = subject_id in _STATIC_ADMINS if subject_id else False

        return {
            "authenticated": True,
            "is_admin": is_admin,
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
    if _state.auth_provider:
        import time as _time

        # Check cache first (15 min TTL)
        cached_result = _get_cached_auth(token)
        if cached_result:
            # Update x_agent_id and timing for this request
            # Safe: cached_result is already a copy from _get_cached_auth
            cached_result["x_agent_id"] = x_agent_id
            cached_result["_auth_time_ms"] = 0.0  # Cache hit = no auth time
            cached_result["_auth_cached"] = True
            return cached_result

        # Cache miss - call provider
        _auth_start = _time.time()
        result = await _state.auth_provider.authenticate(token)
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
            "agent_generation": getattr(result, "agent_generation", None),  # Issue #1445: from JWT
            "x_agent_id": x_agent_id,
            "_auth_time_ms": _auth_elapsed,  # Pass to RPC for logging
            "_auth_cached": False,
        }
        # Cache a copy without per-request fields (x_agent_id, timing)
        cache_entry = {
            k: v
            for k, v in auth_result.items()
            if k not in ("x_agent_id", "_auth_time_ms", "_auth_cached")
        }
        _set_cached_auth(token, cache_entry)
        return auth_result

    # Fall back to static API key (constant-time comparison to prevent timing attacks)
    if _state.api_key:
        if hmac.compare_digest(token, _state.api_key):
            return {
                "authenticated": True,
                "is_admin": True,
                "subject_type": "user",
                "subject_id": "admin",
                "inherit_permissions": True,  # Static admin key always inherits
            }
        return None

    return None


async def get_auth_result(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
    x_agent_id: str | None = Header(None, alias="X-Agent-ID"),
    x_nexus_subject: str | None = Header(None, alias="X-Nexus-Subject"),
    x_nexus_zone_id: str | None = Header(None, alias="X-Nexus-Zone-ID"),
) -> dict[str, Any] | None:
    """FastAPI dependency wrapper for :func:`resolve_auth`.

    Extracts headers via FastAPI DI and delegates to the core auth logic.
    For WebSocket endpoints (where ``Depends()`` is unsupported), call
    :func:`resolve_auth` directly with ``websocket.app.state``.
    """
    return await resolve_auth(
        app_state=request.app.state,
        authorization=authorization,
        x_agent_id=x_agent_id,
        x_nexus_subject=x_nexus_subject,
        x_nexus_zone_id=x_nexus_zone_id,
    )


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
        from nexus.services.permissions.permissions_enhanced import AdminCapability

        admin_capabilities = {
            AdminCapability.READ_ALL,
            AdminCapability.WRITE_ALL,
            AdminCapability.DELETE_ANY,
            AdminCapability.MANAGE_REBAC,
        }

    # Issue #1445: agent_generation comes from JWT claims (via auth pipeline),
    # not from a DB lookup.  SK-key agents will have agent_generation=None
    # and skip stale-session detection (documented limitation).
    agent_generation = auth_result.get("agent_generation")

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
