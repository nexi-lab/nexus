"""Factory for creating authentication providers from configuration.

This module now delegates to nexus.auth brick providers (Issue #1399).
"""

import logging
from typing import Any

from nexus.bricks.auth.providers.base import AuthProvider
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
        logger.info("Creating StaticAPIKeyAuth provider")
        return StaticAPIKeyAuth.from_config(auth_config)

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
