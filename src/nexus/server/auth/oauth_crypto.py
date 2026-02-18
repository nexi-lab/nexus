"""OAuth crypto (backward-compat shim).

Canonical location: ``nexus.auth.oauth.crypto``
"""

import warnings

from nexus.auth.oauth.crypto import OAUTH_ENCRYPTION_KEY_NAME, OAuthCrypto

warnings.warn(
    "Importing OAuthCrypto from nexus.server.auth.oauth_crypto is deprecated. "
    "Use nexus.auth.oauth.crypto instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["OAuthCrypto", "OAUTH_ENCRYPTION_KEY_NAME"]
