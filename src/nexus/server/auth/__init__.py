"""Authentication providers for Nexus server.

Backward-compatibility shim: OAuth components moved to nexus.auth.oauth brick.
Auth providers live in nexus.auth brick.
"""

import warnings

from nexus.auth_config import OAuthConfig, OAuthProviderConfig  # noqa: F401

# OAuth components — now in nexus.auth.oauth brick (re-exported for backward compat)
from nexus.bricks.auth.oauth.crypto import OAuthCrypto  # noqa: F401
from nexus.bricks.auth.oauth.factory import OAuthProviderFactory  # noqa: F401
from nexus.bricks.auth.oauth.providers.google import GoogleOAuthProvider  # noqa: F401
from nexus.bricks.auth.oauth.providers.microsoft import MicrosoftOAuthProvider  # noqa: F401
from nexus.bricks.auth.oauth.token_manager import TokenManager  # noqa: F401

# Auth brick re-exports (moved to nexus.auth in Issue #1399)
from nexus.bricks.auth.providers.base import AuthProvider, AuthResult  # noqa: F401
from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth  # noqa: F401
from nexus.bricks.auth.providers.database_local import DatabaseLocalAuth  # noqa: F401
from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider  # noqa: F401
from nexus.bricks.auth.providers.local import LocalAuth  # noqa: F401
from nexus.bricks.auth.providers.oidc import MultiOIDCAuth, OIDCAuth  # noqa: F401
from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth  # noqa: F401

# Factory function — stays here but delegates to brick providers
from nexus.server.auth.factory import create_auth_provider  # noqa: F401

# Keep original OAuthCredential/OAuthProvider/OAuthError from server layer
# (token_manager.py still uses mutable OAuthCredential)
from nexus.server.auth.oauth_provider import (  # noqa: F401
    OAuthCredential,
    OAuthError,
    OAuthProvider,
)

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
    # OAuth components (canonical: nexus.auth.oauth)
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
    _auth_brick_names = {
        "AuthProvider",
        "AuthResult",
        "StaticAPIKeyAuth",
        "DatabaseAPIKeyAuth",
        "DatabaseLocalAuth",
        "DiscriminatingAuthProvider",
        "LocalAuth",
        "OIDCAuth",
        "MultiOIDCAuth",
    }
    _oauth_brick_names = {
        "OAuthCrypto",
        "OAuthProviderFactory",
        "GoogleOAuthProvider",
        "MicrosoftOAuthProvider",
    }
    if name in _auth_brick_names:
        warnings.warn(
            f"Importing {name} from nexus.server.auth is deprecated. Use nexus.auth instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    elif name in _oauth_brick_names:
        warnings.warn(
            f"Importing {name} from nexus.server.auth is deprecated. Use nexus.auth.oauth instead.",
            DeprecationWarning,
            stacklevel=2,
        )
    return globals()[name]
