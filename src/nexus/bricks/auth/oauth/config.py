"""OAuth provider configuration models.

Backward-compat shim: canonical location is now ``nexus.contracts.oauth_types``
(Issue #3230).  This module re-exports for backward compatibility.

Previous canonical locations:
    - ``nexus.bricks.auth.oauth.config`` (Issue #2281)
    - ``nexus.auth_config``
    - ``nexus.server.auth.oauth_config``
"""

from nexus.contracts.oauth_types import OAuthConfig as OAuthConfig
from nexus.contracts.oauth_types import OAuthProviderConfig as OAuthProviderConfig

__all__ = ["OAuthConfig", "OAuthProviderConfig"]
