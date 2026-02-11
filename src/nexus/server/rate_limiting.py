"""Rate limiting configuration and handlers for FastAPI (Issue #780).

This module contains rate limit tiers, key extraction logic, the global
Limiter instance declaration, and the custom 429 response handler.

Rate Limit Tiers (when enabled):
    - Anonymous: 60 requests/minute (NEXUS_RATE_LIMIT_ANONYMOUS)
    - Authenticated: 300 requests/minute (NEXUS_RATE_LIMIT_AUTHENTICATED)
    - Premium/Admin: 1000 requests/minute (NEXUS_RATE_LIMIT_PREMIUM)

Environment Variables:
    NEXUS_RATE_LIMIT_ENABLED: Set to "true" to enable rate limiting (disabled by default)
    NEXUS_RATE_LIMIT_ANONYMOUS: Override anonymous rate limit (default: "60/minute")
    NEXUS_RATE_LIMIT_AUTHENTICATED: Override authenticated rate limit (default: "300/minute")
    NEXUS_RATE_LIMIT_PREMIUM: Override premium/admin rate limit (default: "1000/minute")
    NEXUS_REDIS_URL: Redis/Dragonfly URL for distributed rate limiting
    DRAGONFLY_URL: Alternative Redis URL (Dragonfly-specific)
"""

from __future__ import annotations

import hashlib
import os

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

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


# Global limiter instance (initialized in create_app via fastapi_server.create_app).
# Starts as a no-op limiter; replaced with a configured instance at app startup.
# Note: This is set before routes are registered, so it's never None when decorators run.
limiter: Limiter = Limiter(key_func=_get_rate_limit_key, enabled=False)
