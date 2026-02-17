"""OAuth provider configuration — re-export from canonical location.

Issue #1389: Models moved to nexus.auth_config to eliminate the
config.py → server/ architecture violation. This module re-exports
for backward compatibility within the server layer.
"""

from nexus.auth_config import OAuthConfig, OAuthProviderConfig

__all__ = ["OAuthConfig", "OAuthProviderConfig"]
