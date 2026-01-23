"""Rate limiting utilities for Nexus API (Issue #780).

This module provides additional rate limiting utilities and documentation.
The core rate limiting is implemented in fastapi_server.py.

Rate Limit Tiers (when enabled):
    - Anonymous: 60 requests/minute (NEXUS_RATE_LIMIT_ANONYMOUS)
    - Authenticated: 300 requests/minute (NEXUS_RATE_LIMIT_AUTHENTICATED)
    - Premium/Admin: 1000 requests/minute (NEXUS_RATE_LIMIT_PREMIUM)
    - Health endpoints: Unlimited (exempt from rate limiting)

Environment Variables:
    NEXUS_RATE_LIMIT_ENABLED: Set to "true" to enable rate limiting (disabled by default)
    NEXUS_RATE_LIMIT_ANONYMOUS: Override anonymous rate limit (default: "60/minute")
    NEXUS_RATE_LIMIT_AUTHENTICATED: Override authenticated rate limit (default: "300/minute")
    NEXUS_RATE_LIMIT_PREMIUM: Override premium/admin rate limit (default: "1000/minute")
    NEXUS_REDIS_URL: Redis/Dragonfly URL for distributed rate limiting
    DRAGONFLY_URL: Alternative Redis URL (Dragonfly-specific)

Storage Backends:
    - In-memory: Used when no Redis URL is configured (single instance only)
    - Redis/Dragonfly: Used when NEXUS_REDIS_URL or DRAGONFLY_URL is set
      (recommended for multi-instance deployments)

Usage:
    Rate limiting is DISABLED by default for better performance.
    To enable, set NEXUS_RATE_LIMIT_ENABLED=true

Example Rate Limit Headers in Response:
    X-RateLimit-Limit: 300
    X-RateLimit-Remaining: 299
    X-RateLimit-Reset: 1704307200
    Retry-After: 60 (only on 429 responses)
"""

from __future__ import annotations

# Re-export key functions from fastapi_server for convenience
from nexus.server.fastapi_server import (
    RATE_LIMIT_ANONYMOUS,
    RATE_LIMIT_AUTHENTICATED,
    RATE_LIMIT_PREMIUM,
    limiter,
)
from nexus.server.fastapi_server import (
    _get_rate_limit_key as get_rate_limit_key,
)
from nexus.server.fastapi_server import (
    _rate_limit_exceeded_handler as rate_limit_exceeded_handler,
)

__all__ = [
    "RATE_LIMIT_ANONYMOUS",
    "RATE_LIMIT_AUTHENTICATED",
    "RATE_LIMIT_PREMIUM",
    "get_rate_limit_key",
    "rate_limit_exceeded_handler",
    "limiter",
]
