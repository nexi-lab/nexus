"""Backward-compat shim — canonical module is nexus.auth.oauth_provider."""

from nexus.auth.oauth_provider import (  # noqa: F401
    OAuthCredential,
    OAuthError,
    OAuthProvider,
)

__all__ = ["OAuthCredential", "OAuthError", "OAuthProvider"]
