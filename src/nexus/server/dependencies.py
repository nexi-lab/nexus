"""FastAPI dependencies for authentication and operation context.

This module contains the authentication dependency functions
(get_auth_result, require_auth), and the OperationContext factory
(get_operation_context) used by route handlers.

Auth caching uses CacheStoreABC (the CacheStore pillar) per
KERNEL-ARCHITECTURE.md §2 — accessed via ``app_state.auth_cache_store``.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Header, HTTPException, Request

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.server.token_utils import parse_sk_token

if TYPE_CHECKING:
    from nexus.contracts.cache_store import CacheStoreABC

logger = logging.getLogger(__name__)

# Auth cache TTL: 15 minutes (900 seconds) - balances performance vs permission freshness
_AUTH_CACHE_TTL = 900

# Singleflight: at most one in-flight provider auth call per unique token (Issue #15)
_auth_inflight: dict[str, asyncio.Future[dict[str, Any] | None]] = {}


def _auth_cache_key(token: str) -> str:
    """Compute the cache key for an auth token (SHA-256 prefix)."""
    return f"auth:cache:{hashlib.sha256(token.encode()).hexdigest()[:32]}"


async def _get_cached_auth(
    cache_store: "CacheStoreABC | None", token: str
) -> dict[str, Any] | None:
    """Get cached auth result if valid via CacheStoreABC."""
    if cache_store is None:
        return None
    raw = await cache_store.get(_auth_cache_key(token))
    if raw is None:
        return None
    result: dict[str, Any] = json.loads(raw)
    return result


async def _set_cached_auth(
    cache_store: "CacheStoreABC | None", token: str, result: dict[str, Any]
) -> None:
    """Cache auth result with TTL via CacheStoreABC."""
    if cache_store is None:
        return
    await cache_store.set(_auth_cache_key(token), json.dumps(result).encode(), ttl=_AUTH_CACHE_TTL)


async def _reset_auth_cache(cache_store: "CacheStoreABC | None") -> None:
    """Reset the auth cache. Used by tests for isolation."""
    if cache_store is None:
        return
    await cache_store.delete_by_pattern("auth:cache:*")


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


def _is_loopback(host: str | None) -> bool:
    """Check whether a client IP is a loopback address.

    Handles IPv4 (127.0.0.0/8), IPv6 (::1), IPv4-mapped IPv6
    (::ffff:127.x.x.x), and "localhost".
    """
    if not host:
        # None means no network connection info (e.g. ASGI TestClient) — treat as local
        return True
    if host in ("localhost", "testclient"):
        return True
    import ipaddress

    try:
        addr = ipaddress.ip_address(host)
        return addr.is_loopback
    except ValueError:
        return False


async def resolve_auth(
    app_state: Any,
    authorization: str | None = None,
    x_agent_id: str | None = None,
    x_nexus_subject: str | None = None,
    x_nexus_zone_id: str | None = None,
    client_host: str | None = None,
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
        # Restrict open-access mode to loopback to prevent remote privilege escalation.
        if not _is_loopback(client_host):
            logger.warning(
                "[AUTH] Open access request rejected from non-loopback address %s. "
                "Configure an API key or auth provider for remote access.",
                client_host,
            )
            return None

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

        # Check cache first (15 min TTL) via CacheStoreABC
        _auth_cache: CacheStoreABC | None = getattr(_state, "auth_cache_store", None)
        cached_result = await _get_cached_auth(_auth_cache, token)
        if cached_result and cached_result.get("authenticated"):
            # Update per-request fields (zone header, agent ID, timing)
            # Safe: cached_result is already a copy from _get_cached_auth
            if x_nexus_zone_id:
                cached_result["zone_id"] = x_nexus_zone_id
            cached_result["x_agent_id"] = x_agent_id
            cached_result["_auth_time_ms"] = 0.0  # Cache hit = no auth time
            cached_result["_auth_cached"] = True
            return cached_result

        # Singleflight: deduplicate concurrent provider calls (Issue #15)
        _flight_key = _auth_cache_key(token)
        if _flight_key in _auth_inflight:
            base = await _auth_inflight[_flight_key]
            if base is not None:
                coalesced = dict(base)
                if x_nexus_zone_id:
                    coalesced["zone_id"] = x_nexus_zone_id
                coalesced["x_agent_id"] = x_agent_id
                coalesced["_auth_time_ms"] = 0.0
                coalesced["_auth_cached"] = True
                return coalesced
            # Provider rejected — fall through to static key check

        _fut: asyncio.Future[dict[str, Any] | None] = asyncio.get_running_loop().create_future()
        _fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        _auth_inflight[_flight_key] = _fut
        try:
            _auth_start = _time.time()
            result = await _state.auth_provider.authenticate(token)
            _auth_elapsed = (_time.time() - _auth_start) * 1000
            if _auth_elapsed > 10:  # Log if auth takes >10ms
                logger.info(f"[AUTH-TIMING] provider auth took {_auth_elapsed:.1f}ms (cache miss)")
            if result is None or not result.authenticated:
                # Provider didn't recognize this token (None) or explicitly
                # rejected it (authenticated=False).  Don't return yet —
                # fall through to the static API key check below, matching
                # the gRPC servicer's check order (static key first).
                await asyncio.sleep(random.uniform(0.001, 0.005))
                _fut.set_result(None)
                # break out of the auth_provider block; continue to static key
            else:
                auth_result = {
                    "authenticated": result.authenticated,
                    "is_admin": result.is_admin,
                    "subject_type": result.subject_type,
                    "subject_id": result.subject_id,
                    "zone_id": x_nexus_zone_id or result.zone_id,
                    "zone_set": list(getattr(result, "zone_set", ()) or ()),
                    "zone_perms": [list(t) for t in getattr(result, "zone_perms", ()) or ()],
                    "inherit_permissions": result.inherit_permissions
                    if hasattr(result, "inherit_permissions")
                    else True,
                    "metadata": result.metadata if hasattr(result, "metadata") else {},
                    "agent_generation": getattr(result, "agent_generation", None),
                    "x_agent_id": x_agent_id,
                    "_auth_time_ms": _auth_elapsed,
                    "_auth_cached": False,
                }
                # Cache a copy without per-request fields (x_agent_id, timing)
                cache_entry = {
                    k: v
                    for k, v in auth_result.items()
                    if k not in ("x_agent_id", "_auth_time_ms", "_auth_cached")
                }
                await _set_cached_auth(_auth_cache, token, cache_entry)
                _fut.set_result(cache_entry)
                return auth_result
        except BaseException as exc:
            _fut.set_exception(exc)
            raise
        finally:
            _auth_inflight.pop(_flight_key, None)

    # Fall back to static API key (constant-time comparison to prevent timing attacks)
    if _state.api_key:
        if hmac.compare_digest(token, _state.api_key):
            return {
                "authenticated": True,
                "is_admin": True,
                "subject_type": "user",
                "subject_id": "admin",
                "zone_id": x_nexus_zone_id,
                "inherit_permissions": True,  # Static admin key always inherits
                "x_agent_id": x_agent_id,
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
    client_host = request.client.host if request.client else None
    return await resolve_auth(
        app_state=request.app.state,
        authorization=authorization,
        x_agent_id=x_agent_id,
        x_nexus_subject=x_nexus_subject,
        x_nexus_zone_id=x_nexus_zone_id,
        client_host=client_host,
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


async def require_admin(
    auth_result: dict[str, Any] = Depends(require_auth),
) -> dict[str, Any]:
    """Require admin privileges for endpoint (Issue #1596).

    Chains on ``require_auth`` so unauthenticated requests get 401 first,
    then non-admin users get 403.

    Raises:
        HTTPException: 401 if not authenticated, 403 if not admin.
    """
    if not auth_result.get("is_admin", False):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    return auth_result


def get_operation_context(auth_result: dict[str, Any]) -> Any:
    """Create OperationContext from auth result.

    Args:
        auth_result: Authentication result dict

    Returns:
        OperationContext for filesystem operations
    """
    from nexus.contracts.types import OperationContext

    subject_type = auth_result.get("subject_type") or "user"
    subject_id = auth_result.get("subject_id") or "anonymous"
    zone_id = auth_result.get("zone_id") or ROOT_ZONE_ID
    is_admin = auth_result.get("is_admin", False)
    agent_id = auth_result.get("x_agent_id")
    user_id = subject_id

    # Handle agent authentication
    if subject_type == "agent":
        agent_id = subject_id

    # Handle X-Agent-ID header — only admins may impersonate arbitrary agents.
    # Non-admin users must authenticate as the agent directly (subject_type="agent").
    if agent_id and subject_type == "user":
        if is_admin:
            subject_type = "agent"
            subject_id = agent_id
        else:
            logger.warning(
                "Non-admin user %s attempted agent impersonation via X-Agent-ID: %s",
                subject_id,
                agent_id,
            )

    # Admin capabilities
    admin_capabilities = set()
    if is_admin:
        from nexus.bricks.rebac.permissions_enhanced import AdminCapability

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
        user_id=user_id,
        agent_id=agent_id,
        subject_type=subject_type,
        subject_id=subject_id,
        zone_id=zone_id,
        is_admin=is_admin,
        groups=[],
        admin_capabilities=admin_capabilities,
        agent_generation=agent_generation,
    )
