"""OAuth user auth (backward-compat shim).

Canonical location: ``nexus.auth.oauth.user_auth``
"""

import warnings

from nexus.auth.oauth.user_auth import OAuthUserAuth

warnings.warn(
    "Importing OAuthUserAuth from nexus.server.auth.oauth_user_auth is deprecated. "
    "Use nexus.auth.oauth.user_auth instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["OAuthUserAuth"]
