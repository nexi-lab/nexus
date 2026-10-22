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

import asyncio
import hashlib
import logging
import os
import time
from contextlib import suppress
from functools import lru_cache
from typing import Any

from limits import RateLimitItem
from limits import parse as _parse_limit
from limits.storage import storage_from_string
from limits.strategies import FixedWindowRateLimiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from nexus.bricks.mcp.credentials import http_api_key


@lru_cache(maxsize=16)
def _parse_limit_cached(limit_str: str) -> RateLimitItem:
    return _parse_limit(limit_str)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier defaults
# ---------------------------------------------------------------------------
DEFAULT_ANON: str = "60/minute"
DEFAULT_AUTH: str = "300/minute"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rate_limit_key(request: Request) -> str:
    """Bucket an opaque caller credential without interpreting its identity."""
    token = http_api_key(request.headers)
    if token:
        return f"token:{hashlib.sha256(token.encode()).hexdigest()}"
    return f"ip:{get_remote_address(request) or 'unknown'}"


def _tier_for_request(request: Request) -> str:
    """Choose an ingress budget; native authentication remains authoritative."""
    return "authenticated" if http_api_key(request.headers) else "anonymous"


def _limit_for_tier(tier: str) -> str:
    """Return the rate-limit string for the given tier (reads from env)."""
    if tier == "anonymous":
        return os.environ.get("NEXUS_MCP_RATE_LIMIT_ANONYMOUS", DEFAULT_ANON)
    return os.environ.get("NEXUS_MCP_RATE_LIMIT_AUTHENTICATED", DEFAULT_AUTH)


def _dynamic_limit(request: Request) -> str:
    """Return the limit string for the request's tier."""
    return _limit_for_tier(_tier_for_request(request))


# ---------------------------------------------------------------------------
# Custom middleware
# ---------------------------------------------------------------------------

_RATE_LIMIT_METRIC_TTL_SECONDS = 600
_RATE_LIMIT_METRIC_TIMEOUT_SECONDS = 2


async def _write_rate_limit_hit(client: Any, key: str) -> None:
    await client.incr(key)
    await client.expire(key, _RATE_LIMIT_METRIC_TTL_SECONDS)


async def _record_rate_limit_hit(tier: str) -> None:
    """Record an expiring ingress rate-limit counter."""
    url = os.environ.get("NEXUS_REDIS_URL") or os.environ.get("DRAGONFLY_URL")
    if not url or url == "memory://":
        return
    try:
        import redis.asyncio as redis
    except ImportError:
        return

    client = None
    try:
        client = redis.from_url(
            url,
            socket_connect_timeout=_RATE_LIMIT_METRIC_TIMEOUT_SECONDS,
            socket_timeout=_RATE_LIMIT_METRIC_TIMEOUT_SECONDS,
        )
        epoch_min = int(time.time()) // 60
        key = f"nexus:hub:ratelimit:tier:{tier}:{epoch_min}"
        await asyncio.wait_for(
            _write_rate_limit_hit(client, key),
            timeout=_RATE_LIMIT_METRIC_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001
        return
    finally:
        if client is not None:
            with suppress(Exception):
                await client.close()


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
        limit_item = _parse_limit_cached(limit_str)
        key = _rate_limit_key(request)

        try:
            allowed = self._strategy.hit(limit_item, key)
        except Exception:
            logger.warning(
                "Rate-limit storage unavailable — failing open for key=%s",
                key,
                exc_info=True,
            )
            return await call_next(request)

        if not allowed:
            stats = self._strategy.get_window_stats(limit_item, key)
            retry_after = max(0, int(stats.reset_time - time.time()))
            await _record_rate_limit_hit(tier)
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


def build_rate_limit_middleware() -> Any:
    """Return a Starlette ``Middleware`` wrapper for use with
    ``mcp.run(middleware=[...])``.

    FastMCP's ``http_app()`` returns a new Starlette instance on each call,
    so middleware must be passed at ``run()`` time — not added after the
    fact. This helper mirrors the configuration read by ``install_rate_limit``.
    """
    from starlette.middleware import Middleware

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
    return Middleware(_MCPRateLimitMiddleware, storage=storage, enabled=enabled)


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
      requests (default ``300/minute``).
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


__all__ = ["build_rate_limit_middleware", "install_rate_limit"]
