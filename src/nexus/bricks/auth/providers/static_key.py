"""Static API key authentication provider."""

from __future__ import annotations

import logging
from typing import Any

from nexus.bricks.auth.constants import API_KEY_PREFIX
from nexus.bricks.auth.providers.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)


class StaticAPIKeyAuth(AuthProvider):
    """Static API key authentication using configuration file.

    Suitable for self-hosted deployments with a small number of users.
    API keys are configured in a dictionary mapping keys to user information.

    Example config:
        api_keys:
          "sk-alice-secret-key":
            subject_type: "user"
            subject_id: "alice"
            zone_id: "org_acme"
            is_admin: true
    """

    def __init__(self, api_keys: dict[str, dict[str, Any]]) -> None:
        self.api_keys = api_keys
        logger.info("Initialized StaticAPIKeyAuth with %d keys", len(api_keys))

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using static API key."""
        if not token:
            return AuthResult(authenticated=False)

        if not token.startswith(API_KEY_PREFIX):
            logger.warning("UNAUTHORIZED: Static API key must start with %s", API_KEY_PREFIX)
            return AuthResult(authenticated=False)

        if token not in self.api_keys:
            return AuthResult(authenticated=False)

        user_info = self.api_keys[token]
        return AuthResult(
            authenticated=True,
            subject_type=user_info.get("subject_type", "user"),
            subject_id=user_info.get("subject_id"),
            zone_id=user_info.get("zone_id"),
            is_admin=user_info.get("is_admin", False),
            metadata=user_info.get("metadata"),
        )

    async def validate_token(self, token: str) -> bool:
        return token in self.api_keys

    def close(self) -> None:
        pass

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> StaticAPIKeyAuth:
        """Create from configuration dictionary."""
        api_keys = config.get("api_keys", {})
        if not api_keys:
            logger.warning("No API keys configured in StaticAPIKeyAuth")
        return cls(api_keys)
