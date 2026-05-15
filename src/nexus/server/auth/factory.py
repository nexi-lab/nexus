"""Factory for creating authentication providers from configuration.

This module now delegates to nexus.auth brick providers (Issue #1399).
"""

import logging
from typing import Any

from nexus.bricks.auth.providers.base import AuthProvider, AuthResult
from nexus.bricks.auth.providers.database_key import DatabaseAPIKeyAuth
from nexus.bricks.auth.providers.discriminator import DiscriminatingAuthProvider  # noqa: F401
from nexus.bricks.auth.providers.local import LocalAuth
from nexus.bricks.auth.providers.oidc import MultiOIDCAuth, OIDCAuth
from nexus.bricks.auth.providers.static_key import StaticAPIKeyAuth

logger = logging.getLogger(__name__)


def create_auth_provider(
    auth_type: str | None, auth_config: dict[str, Any] | None = None, **kwargs: Any
) -> AuthProvider | None:
    """Create authentication provider from configuration.

    Args:
        auth_type: Authentication type ('static', 'database', 'local', 'oidc', 'multi-oidc', or None)
        auth_config: Authentication configuration (depends on auth_type)
        **kwargs: Additional arguments passed to auth provider (e.g., record_store)

    Returns:
        AuthProvider instance or None if no authentication
    """
    if not auth_type:
        logger.info("No authentication configured")
        return None

    if auth_type == "static":
        if not auth_config and "api_key" in kwargs:
            api_key = kwargs["api_key"]
            auth_config = {
                "api_keys": {
                    api_key: {
                        "subject_type": "user",
                        "subject_id": "admin",
                        "is_admin": True,
                    }
                }
            }
        if not auth_config:
            raise ValueError("auth_config is required for static authentication")

        static_provider = StaticAPIKeyAuth.from_config(auth_config)

        # Chain with DatabaseAPIKeyAuth so agent keys generated at registration
        # are also validated. Without this, agents can register and get keys but
        # those keys are never accepted by the static-only provider.
        record_store = kwargs.get("record_store")
        if record_store is not None:
            logger.info("Creating StaticAPIKeyAuth + DatabaseAPIKeyAuth (agent key fallback)")
            db_provider = DatabaseAPIKeyAuth(record_store, require_expiry=False)
            return _ChainedAPIKeyAuth(static_provider, db_provider)

        logger.info("Creating StaticAPIKeyAuth provider (no DB fallback)")
        return static_provider

    elif auth_type == "database":
        record_store = kwargs.get("record_store")
        if not record_store:
            raise ValueError("record_store is required for database authentication")
        logger.info("Creating DatabaseAPIKeyAuth provider")
        return DatabaseAPIKeyAuth(record_store)

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


class _ChainedAPIKeyAuth(AuthProvider):
    """Chains two API key providers: tries primary first, falls back to secondary.

    Used when static auth needs to also validate agent keys from the database.
    """

    def __init__(self, primary: AuthProvider, fallback: AuthProvider) -> None:
        self._primary = primary
        self._fallback = fallback

    async def authenticate(self, token: str) -> AuthResult:
        result = await self._primary.authenticate(token)
        if result.authenticated:
            return result
        return await self._fallback.authenticate(token)

    async def validate_token(self, token: str) -> bool:
        if await self._primary.validate_token(token):
            return True
        return await self._fallback.validate_token(token)

    @property
    def session_factory(self) -> Any:
        """Expose the database provider session factory for admin RPC handlers."""
        if hasattr(self._fallback, "session_factory"):
            return self._fallback.session_factory
        if hasattr(self._primary, "session_factory"):
            return self._primary.session_factory
        raise AttributeError("session_factory")

    @property
    def _record_store(self) -> Any:
        """Expose the database provider record store for admin grant handling."""
        if hasattr(self._fallback, "_record_store"):
            return self._fallback._record_store
        if hasattr(self._primary, "_record_store"):
            return self._primary._record_store
        raise AttributeError("_record_store")

    def close(self) -> None:
        self._primary.close()
        self._fallback.close()
