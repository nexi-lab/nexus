"""Backward-compatibility shim — use nexus.auth.oauth.config instead.

Issue #2281: Moved to nexus.auth.oauth.config (auth brick).
"""

from nexus.bricks.auth.oauth.config import OAuthConfig, OAuthProviderConfig

__all__ = ["OAuthConfig", "OAuthProviderConfig"]
