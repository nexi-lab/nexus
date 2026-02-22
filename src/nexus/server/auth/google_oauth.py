"""Google OAuth provider (backward-compat shim).

Canonical location: ``nexus.auth.oauth.providers.google``
"""

import warnings

from nexus.auth.oauth.providers.google import GoogleOAuthProvider

warnings.warn(
    "Importing GoogleOAuthProvider from nexus.server.auth.google_oauth is deprecated. "
    "Use nexus.auth.oauth.providers.google instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["GoogleOAuthProvider"]
