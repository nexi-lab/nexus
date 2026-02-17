"""Backward-compatibility shim — use nexus.auth.providers.oidc instead."""

from nexus.auth.providers.oidc import (
    ALLOWED_ALGORITHMS,
    CLOCK_SKEW_SECONDS,
    JWKS_CACHE_TTL,
    MultiOIDCAuth,
    OIDCAuth,
)

__all__ = [
    "OIDCAuth",
    "MultiOIDCAuth",
    "ALLOWED_ALGORITHMS",
    "CLOCK_SKEW_SECONDS",
    "JWKS_CACHE_TTL",
]
