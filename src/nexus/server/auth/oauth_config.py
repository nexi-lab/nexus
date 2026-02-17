"""Backward-compat shim — canonical module is nexus.auth.oauth_config."""

from nexus.auth.oauth_config import (  # noqa: F401
    OAuthConfig,
    OAuthProviderConfig,
)

__all__ = ["OAuthConfig", "OAuthProviderConfig"]
