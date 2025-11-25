"""Authentication providers for Nexus server."""

from nexus.server.auth.base import AuthProvider, AuthResult
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.factory import create_auth_provider
from nexus.server.auth.google_oauth import GoogleOAuthProvider
from nexus.server.auth.local import LocalAuth
from nexus.server.auth.microsoft_oauth import MicrosoftOAuthProvider
from nexus.server.auth.oauth_config import OAuthConfig, OAuthProviderConfig
from nexus.server.auth.oauth_crypto import OAuthCrypto
from nexus.server.auth.oauth_factory import OAuthProviderFactory
from nexus.server.auth.oauth_provider import OAuthCredential, OAuthError, OAuthProvider
from nexus.server.auth.oidc import MultiOIDCAuth, OIDCAuth
from nexus.server.auth.static_key import StaticAPIKeyAuth
from nexus.server.auth.token_manager import TokenManager

__all__ = [
    "AuthProvider",
    "AuthResult",
    "StaticAPIKeyAuth",
    "DatabaseAPIKeyAuth",
    "LocalAuth",
    "OIDCAuth",
    "MultiOIDCAuth",
    "create_auth_provider",
    # OAuth components
    "OAuthProvider",
    "OAuthCredential",
    "OAuthError",
    "OAuthCrypto",
    # OAuth configuration
    "OAuthConfig",
    "OAuthProviderConfig",
    "OAuthProviderFactory",
    # Google OAuth providers
    "GoogleOAuthProvider",
    # Microsoft OAuth
    "MicrosoftOAuthProvider",
    "TokenManager",
]
