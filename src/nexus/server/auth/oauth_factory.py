"""OAuth factory (backward-compat shim).

Canonical location: ``nexus.auth.oauth.factory``
"""

import warnings

from nexus.auth.oauth.factory import OAuthProviderFactory

warnings.warn(
    "Importing OAuthProviderFactory from nexus.server.auth.oauth_factory is deprecated. "
    "Use nexus.auth.oauth.factory instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["OAuthProviderFactory"]
