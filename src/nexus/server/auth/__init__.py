"""Authentication providers for Nexus server.

Backward-compatibility shim: Auth providers and OAuth services now
live in the nexus.auth brick. This module re-exports for backward compat.
"""

import warnings

# Auth brick re-exports (moved to nexus.auth in Issue #1399)
from nexus.auth.providers.base import AuthProvider, AuthResult  # noqa: F401
from nexus.auth.providers.database_key import DatabaseAPIKeyAuth  # noqa: F401
from nexus.auth.providers.database_local import DatabaseLocalAuth  # noqa: F401
from nexus.auth.providers.discriminator import DiscriminatingAuthProvider  # noqa: F401
from nexus.auth.providers.local import LocalAuth  # noqa: F401
from nexus.auth.providers.oidc import MultiOIDCAuth, OIDCAuth  # noqa: F401
from nexus.auth.providers.static_key import StaticAPIKeyAuth  # noqa: F401

# Factory function — stays here but delegates to brick providers
from nexus.server.auth.factory import create_auth_provider  # noqa: F401

# OAuth components — canonical: nexus.auth (moved in Issue #1526)
from nexus.auth.oauth_config import OAuthConfig, OAuthProviderConfig  # noqa: F401
from nexus.auth.oauth_crypto import OAuthCrypto  # noqa: F401
from nexus.auth.oauth_factory import OAuthProviderFactory  # noqa: F401
from nexus.auth.oauth_provider import (  # noqa: F401
    OAuthCredential,
    OAuthError,
    OAuthProvider,
)
from nexus.auth.token_manager import TokenManager  # noqa: F401
from nexus.auth.google_oauth import GoogleOAuthProvider  # noqa: F401
from nexus.auth.microsoft_oauth import MicrosoftOAuthProvider  # noqa: F401

__all__ = [
    # Auth brick (canonical: nexus.auth)
    "AuthProvider",
    "AuthResult",
    "StaticAPIKeyAuth",
    "DatabaseAPIKeyAuth",
    "DatabaseLocalAuth",
    "DiscriminatingAuthProvider",
    "LocalAuth",
    "OIDCAuth",
    "MultiOIDCAuth",
    "create_auth_provider",
    # OAuth components (stay in server/auth for Phase 2)
    "OAuthProvider",
    "OAuthCredential",
    "OAuthError",
    "OAuthCrypto",
    "OAuthConfig",
    "OAuthProviderConfig",
    "OAuthProviderFactory",
    "GoogleOAuthProvider",
    "MicrosoftOAuthProvider",
    "TokenManager",
]

def __getattr__(name: str) -> object:
    """Emit deprecation warning for auth provider imports from server.auth."""
    if name in (
        "AuthProvider",
        "AuthResult",
        "StaticAPIKeyAuth",
        "DatabaseAPIKeyAuth",
        "DatabaseLocalAuth",
        "DiscriminatingAuthProvider",
        "LocalAuth",
        "OIDCAuth",
        "MultiOIDCAuth",
    ):
        warnings.warn(
            f"Importing {name} from nexus.server.auth is deprecated. Use nexus.auth instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return globals()[name]
