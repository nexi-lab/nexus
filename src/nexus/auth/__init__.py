"""Auth brick — self-contained authentication module (Issue #1399).

Exports types and protocol only at package level (lazy loading).
Import providers and service explicitly when needed.
"""

from nexus.auth.constants import (
    API_KEY_MIN_LENGTH,
    API_KEY_PREFIX,
    HMAC_SALT,
    PERSONAL_EMAIL_DOMAINS,
    RESERVED_ZONE_IDS,
)
from nexus.auth.protocol import AuthBrickProtocol
from nexus.auth.types import AuthConfig, AuthResult

__all__ = [
    # Types
    "AuthResult",
    "AuthConfig",
    # Protocol
    "AuthBrickProtocol",
    # Constants
    "API_KEY_PREFIX",
    "API_KEY_MIN_LENGTH",
    "HMAC_SALT",
    "PERSONAL_EMAIL_DOMAINS",
    "RESERVED_ZONE_IDS",
    # OAuth (lazy-loaded via submodules)
    # nexus.auth.oauth_provider — OAuthProvider, OAuthCredential, OAuthError
    # nexus.auth.oauth_config — OAuthConfig, OAuthProviderConfig
    # nexus.auth.oauth_crypto — OAuthCrypto
    # nexus.auth.oauth_factory — OAuthProviderFactory
    # nexus.auth.token_manager — TokenManager
]
