"""Factory for creating authentication providers from configuration."""

import logging
from typing import Any

from authlib.jose import jwt

from nexus.server.auth.base import AuthProvider, AuthResult
from nexus.server.auth.database_key import DatabaseAPIKeyAuth
from nexus.server.auth.local import LocalAuth
from nexus.server.auth.oidc import MultiOIDCAuth, OIDCAuth
from nexus.server.auth.static_key import StaticAPIKeyAuth

logger = logging.getLogger(__name__)

# P0-1: Token type discrimination constants
API_KEY_PREFIX = "sk-"  # Static/Database API keys


class DiscriminatingAuthProvider(AuthProvider):
    """Auth provider with explicit token type discrimination.

    P0-1: Don't overload a single api_key field. Detect type explicitly:
    - prefix sk- → Database/Static key
    - else → Parse JWT/OIDC

    Rejects ambiguous/unknown token types early with clear error.
    """

    def __init__(
        self,
        api_key_provider: AuthProvider | None = None,
        jwt_provider: AuthProvider | None = None,
    ):
        """Initialize discriminating auth provider.

        Args:
            api_key_provider: Provider for API keys (static or database)
            jwt_provider: Provider for JWT tokens (local or OIDC)
        """
        self.api_key_provider = api_key_provider
        self.jwt_provider = jwt_provider

        providers = []
        if api_key_provider:
            providers.append("API keys")
        if jwt_provider:
            providers.append("JWT/OIDC")

        logger.info(f"Initialized DiscriminatingAuthProvider with: {', '.join(providers)}")

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate with explicit token type discrimination.

        P0-1: Detect token type by prefix/format, then route to appropriate provider

        Args:
            token: Token from Authorization header

        Returns:
            AuthResult
        """
        if not token:
            return AuthResult(authenticated=False)

        # P0-1: Discriminate by prefix
        if token.startswith(API_KEY_PREFIX):
            # API key (static or database)
            if self.api_key_provider:
                logger.debug("Routing to API key provider (prefix: sk-)")
                return await self.api_key_provider.authenticate(token)
            else:
                logger.error("UNAUTHORIZED: API key provided but no API key provider configured")
                return AuthResult(authenticated=False)

        else:
            # Assume JWT/OIDC token
            if self.jwt_provider:
                # Validate it looks like a JWT before routing
                if self._looks_like_jwt(token):
                    logger.debug("Routing to JWT/OIDC provider")
                    return await self.jwt_provider.authenticate(token)
                else:
                    logger.error("UNAUTHORIZED: Token format not recognized (not API key, not JWT)")
                    return AuthResult(authenticated=False)
            else:
                logger.error("UNAUTHORIZED: JWT token provided but no JWT provider configured")
                return AuthResult(authenticated=False)

    def _looks_like_jwt(self, token: str) -> bool:
        """Check if token looks like a JWT.

        Args:
            token: Token string

        Returns:
            True if token has JWT structure (3 parts separated by dots)
        """
        parts = token.split(".")
        if len(parts) != 3:
            return False

        # Try to decode header
        try:
            jwt.decode_header(token)
            return True
        except Exception:
            return False

    async def validate_token(self, token: str) -> bool:
        """Validate token (route to appropriate provider).

        Args:
            token: Token string

        Returns:
            True if valid
        """
        result = await self.authenticate(token)
        return result.authenticated


def create_auth_provider(
    auth_type: str | None, auth_config: dict[str, Any] | None = None, **kwargs: Any
) -> AuthProvider | None:
    """Create authentication provider from configuration.

    Args:
        auth_type: Authentication type ('static', 'database', 'local', 'oidc', 'multi-oidc', or None)
        auth_config: Authentication configuration (depends on auth_type)
        **kwargs: Additional arguments passed to auth provider (e.g., session_factory)

    Returns:
        AuthProvider instance or None if no authentication

    Example (static keys):
        auth_config = {
            "api_keys": {
                "sk-alice-xxx": {"user_id": "alice", "is_admin": True},
                "sk-bob-xxx": {"user_id": "bob", "is_admin": False}
            }
        }
        provider = create_auth_provider("static", auth_config)

    Example (database keys):
        from sqlalchemy.orm import sessionmaker
        session_factory = sessionmaker(bind=engine)
        provider = create_auth_provider(
            "database",
            session_factory=session_factory
        )

    Example (local auth with JWT):
        auth_config = {
            "jwt_secret": "your-secret-key",
            "users": {
                "alice@example.com": {
                    "password_hash": "bcrypt-hash",
                    "subject_id": "alice"
                }
            }
        }
        provider = create_auth_provider("local", auth_config)

    Example (OIDC):
        auth_config = {
            "issuer": "https://accounts.google.com",
            "audience": "your-client-id"
        }
        provider = create_auth_provider("oidc", auth_config)

    Example (multi-OIDC):
        auth_config = {
            "providers": {
                "google": {"issuer": "...", "audience": "..."},
                "github": {"issuer": "...", "audience": "..."}
            }
        }
        provider = create_auth_provider("multi-oidc", auth_config)

    Example (no authentication):
        provider = create_auth_provider(None)  # Returns None
    """
    if not auth_type:
        logger.info("No authentication configured")
        return None

    if auth_type == "static":
        if not auth_config:
            raise ValueError("auth_config is required for static authentication")
        logger.info("Creating StaticAPIKeyAuth provider")
        return StaticAPIKeyAuth.from_config(auth_config)

    elif auth_type == "database":
        session_factory = kwargs.get("session_factory")
        if not session_factory:
            raise ValueError("session_factory is required for database authentication")
        logger.info("Creating DatabaseAPIKeyAuth provider")
        return DatabaseAPIKeyAuth(session_factory)

    elif auth_type == "local":
        if not auth_config:
            raise ValueError("auth_config is required for local authentication")
        logger.info("Creating LocalAuth provider")
        return LocalAuth.from_config(auth_config)

    elif auth_type == "oidc":
        if not auth_config:
            raise ValueError("auth_config is required for OIDC authentication")
        logger.info("Creating OIDCAuth provider")
        return OIDCAuth.from_config(auth_config)

    elif auth_type == "multi-oidc":
        if not auth_config:
            raise ValueError("auth_config is required for multi-OIDC authentication")
        logger.info("Creating MultiOIDCAuth provider")
        return MultiOIDCAuth.from_config(auth_config)

    else:
        raise ValueError(f"Unknown auth_type: {auth_type}")
