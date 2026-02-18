"""X OAuth provider (backward-compat shim).

Canonical location: ``nexus.auth.oauth.providers.x``
"""

import warnings

from nexus.auth.oauth.providers.x import XOAuthProvider

warnings.warn(
    "Importing XOAuthProvider from nexus.server.auth.x_oauth is deprecated. "
    "Use nexus.auth.oauth.providers.x instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["XOAuthProvider"]
