"""Microsoft OAuth provider (backward-compat shim).

Canonical location: ``nexus.auth.oauth.providers.microsoft``
"""

import warnings

from nexus.auth.oauth.providers.microsoft import MicrosoftOAuthProvider

warnings.warn(
    "Importing MicrosoftOAuthProvider from nexus.server.auth.microsoft_oauth is deprecated. "
    "Use nexus.auth.oauth.providers.microsoft instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["MicrosoftOAuthProvider"]
