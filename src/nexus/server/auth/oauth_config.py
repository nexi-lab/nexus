"""OAuth provider configuration (re-export shim).

Issue #1389: Models moved to ``nexus.auth_config`` so that ``nexus/config.py``
can import them without reaching into the server layer.

All public names are re-exported here for backward compatibility.
"""

from nexus.auth_config import OAuthConfig, OAuthProviderConfig

__all__ = ["OAuthConfig", "OAuthProviderConfig"]
