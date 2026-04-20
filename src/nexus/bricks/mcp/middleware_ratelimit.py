"""SlowAPI-based rate limit middleware for the MCP HTTP transport (#3779).

Per-token rate limiting with configurable tiers. Redis/Dragonfly backend
for cross-replica consistency; falls back to in-memory if the URL is
unreachable or unset.

Public API
----------
- ``install_rate_limit(app)`` — attach rate-limiting to a Starlette app.

Implementation notes
--------------------
SlowAPI 0.1.9's ``SlowAPIMiddleware`` reads the limiter from
``app.state.limiter`` (not kwargs) and the ``default_limits`` callable
API does not receive the ``Request`` object in middleware context. We
therefore drive the ``limits`` library directly from a
``BaseHTTPMiddleware`` subclass so that per-request tier selection works
correctly.  The public interface (``install_rate_limit``) and constants
are unchanged; the internals swap SlowAPIMiddleware for a thin custom
wrapper.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

from limits import parse as parse_limit
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nexus.bricks.mcp.auth_cache import get_auth_identity_cache, hash_api_key
from nexus.server.token_utils import parse_sk_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier defaults
# ---------------------------------------------------------------------------
DEFAULT_ANON: str = "60/minute"
DEFAULT_AUTH: str = "300/minute"
DEFAULT_PREMIUM: str = "1000/minute"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_token(request: Request) -> str | None:
    """Extract bearer token from Authorization or X-Nexus-API-Key header."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.headers.get("X-Nexus-API-Key") or None


def _rate_limit_key(request: Request) -> str:
    """Return a per-client rate-limit bucket key.

    Priority: parsed sk-token fields → hashed bearer/api-key → agent header → IP.

    Note: the parameter must be named ``request`` so SlowAPI's
    ``__evaluate_limits`` can detect it via ``inspect.signature``.
    """
    token = _extract_token(request)
    if token:
        parsed = parse_sk_token(token)
        if parsed is not None:
            return f"user:{parsed.zone or 'unknown'}:{parsed.user or 'unknown'}"
        return f"token:{hashlib.sha256(token.encode()).hexdigest()[:16]}"
    agent = request.headers.get("X-Agent-ID")
    if agent:
        return f"agent:{agent}"
    client_host = request.client.host if request.client else "unknown"
    return f"ip:{client_host}"


def _tier_for_request(request: Request) -> str:
    """Return the rate-limit tier for this request.

    Consults ``AuthIdentityCache`` for cached identity.  Unknown tokens
    default to ``"authenticated"`` to avoid a synchronous auth round-trip
    inside ASGI middleware.
    """
    token = _extract_token(request)
    if not token:
        return "anonymous"
    cache = get_auth_identity_cache()
    hit = cache.get(hash_api_key(token))
    if hit is None:
        # Token present but not yet in cache — treat as authenticated.
        return "authenticated"
    return hit.tier


def _limit_for_tier(tier: str) -> str:
    """Return the rate-limit string for the given tier (reads from env)."""
    if tier == "premium":
        return os.environ.get("NEXUS_MCP_RATE_LIMIT_PREMIUM", DEFAULT_PREMIUM)
    if tier == "anonymous":
        return os.environ.get("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", DEFAULT_ANON)
    return os.environ.get("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", DEFAULT_AUTH)


def _dynamic_limit(request: Request) -> str:
    """Return the limit string for the request's tier."""
    return _limit_for_tier(_tier_for_request(request))


def _rate_limit_exceeded_handler(_request: Request, exc: Exception) -> JSONResponse:
    """Produce a structured 429 response with Retry-After."""
    retry_after = getattr(exc, "retry_after", 60)
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": str(exc),
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# Custom middleware
# ---------------------------------------------------------------------------


class _MCPRateLimitMiddleware(BaseHTTPMiddleware):
    """ASGI rate-limit middleware driven directly by the ``limits`` library.

    Uses ``FixedWindowRateLimiter`` so that per-request dynamic tier
    selection works correctly.  When ``enabled=False`` every request
    passes through immediately.
    """

    def __init__(self, app: Any, *, storage: Any, enabled: bool) -> None:
        super().__init__(app)
        self._storage = storage
        self._strategy = FixedWindowRateLimiter(storage)
        self._enabled = enabled

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._enabled:
            return await call_next(request)

        tier = _tier_for_request(request)
        limit_str = _limit_for_tier(tier)
        limit_item = parse_limit(limit_str)
        key = _rate_limit_key(request)

        allowed = self._strategy.hit(limit_item, key)
        if not allowed:
            stats = self._strategy.get_window_stats(limit_item, key)
            retry_after = max(0, int(stats.reset_time - time.time()))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "Rate limit exceeded",
                    "detail": f"Rate limit exceeded: {limit_str}",
                    "retry_after": retry_after,
                },
                headers={"Retry-After": str(retry_after)},
            )

        return await call_next(request)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install_rate_limit(app: Any) -> None:
    """Install rate-limit middleware + 429 handler on a Starlette app.

    Reads configuration from environment variables:

    - ``MCP_RATE_LIMIT_ENABLED`` (default ``"false"``) — set to ``"true"``
      to enforce limits.
    - ``NEXUS_REDIS_URL`` / ``DRAGONFLY_URL`` — Redis/Dragonfly backend
      URI; falls back to ``"memory://"`` if unset or unreachable.
    - ``NEXUS_MCP_RATE_LIMIT_ANONYMOUS`` — limit for unauthenticated
      requests (default ``60/minute``).
    - ``NEXUS_MCP_RATE_LIMIT_AUTHENTICATED`` — limit for token-bearing
      requests whose identity is not yet cached (default ``300/minute``).
    - ``NEXUS_MCP_RATE_LIMIT_PREMIUM`` — limit for admin/premium tokens
      (default ``1000/minute``).
    """
    enabled = os.environ.get("MCP_RATE_LIMIT_ENABLED", "false").lower() == "true"
    storage_uri = (
        os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL") or "memory://"
    )

    try:
        storage = storage_from_string(storage_uri)
    except Exception:
        logger.warning(
            "Rate-limit storage init failed for uri=%s — falling back to memory://",
            storage_uri,
            exc_info=True,
        )
        storage = storage_from_string("memory://")

    app.add_middleware(_MCPRateLimitMiddleware, storage=storage, enabled=enabled)


__all__ = [
    "install_rate_limit",
    "_rate_limit_exceeded_handler",
    "_rate_limit_key",
    "_tier_for_request",
    "_dynamic_limit",
    "_limit_for_tier",
    "_extract_token",
]
