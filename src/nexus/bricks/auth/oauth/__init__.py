"""OAuth brick — self-contained OAuth module (Issue #1399).

Provides protocol, types, providers, factory, crypto, pending manager,
and user auth with zero imports from nexus.server/core/rebac.
"""

from nexus.bricks.auth.oauth.protocol import (
    OAuthProviderProtocol,
    OAuthTokenManagerProtocol,
)
from nexus.bricks.auth.oauth.types import (
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
    #   nexus.bricks.auth.oauth.base_provider.BaseOAuthProvider
    #   nexus.bricks.auth.oauth.providers.google.GoogleOAuthProvider
    #   nexus.bricks.auth.oauth.providers.microsoft.MicrosoftOAuthProvider
    #   nexus.bricks.auth.oauth.providers.x.XOAuthProvider
    #   nexus.bricks.auth.oauth.crypto.OAuthCrypto
    #   nexus.bricks.auth.oauth.factory.OAuthProviderFactory
    #   nexus.bricks.auth.oauth.pending.PendingOAuthManager
    #   nexus.bricks.auth.oauth.token_manager.TokenManager
]
