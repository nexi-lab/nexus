"""OAuth brick — self-contained OAuth module (Issue #1399).

Provides protocol, types, providers, factory, crypto, pending manager,
and user auth with zero imports from nexus.server/core/rebac.
"""

from nexus.auth.oauth.protocol import (
    OAuthProviderProtocol,
    OAuthTokenManagerProtocol,
)
from nexus.auth.oauth.types import (
    OAuthCredential,
    OAuthError,
    PendingOAuthRegistration,
)

__all__ = [
    # Types
    "OAuthCredential",
    "OAuthError",
    "PendingOAuthRegistration",
    # Protocols
    "OAuthProviderProtocol",
    "OAuthTokenManagerProtocol",
    # Lazy-loaded (import explicitly when needed):
    #   nexus.auth.oauth.base_provider.BaseOAuthProvider
    #   nexus.auth.oauth.providers.google.GoogleOAuthProvider
    #   nexus.auth.oauth.providers.microsoft.MicrosoftOAuthProvider
    #   nexus.auth.oauth.providers.x.XOAuthProvider
    #   nexus.auth.oauth.crypto.OAuthCrypto
    #   nexus.auth.oauth.factory.OAuthProviderFactory
    #   nexus.auth.oauth.pending.PendingOAuthManager
]
