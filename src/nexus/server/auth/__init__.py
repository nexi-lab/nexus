"""Authentication providers for Nexus server."""

from nexus.server.auth.base import AuthProvider, AuthResult
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.factory import create_auth_provider
from nexus.server.auth.local import LocalAuth
from nexus.server.auth.oidc import MultiOIDCAuth, OIDCAuth
from nexus.server.auth.static_key import StaticAPIKeyAuth

__all__ = [
    "AuthProvider",
    "AuthResult",
    "StaticAPIKeyAuth",
    "DatabaseAPIKeyAuth",
    "LocalAuth",
    "OIDCAuth",
    "MultiOIDCAuth",
    "create_auth_provider",
]
