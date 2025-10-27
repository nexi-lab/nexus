"""Static API key authentication provider."""

import logging
from typing import Any

from nexus.server.auth.base import AuthProvider, AuthResult

logger = logging.getLogger(__name__)

# P0-1: Token type discrimination
API_KEY_PREFIX = "sk-"  # All keys must start with this


class StaticAPIKeyAuth(AuthProvider):
    """Static API key authentication using configuration file.

    This is the simplest authentication method, suitable for self-hosted deployments
    with a small number of users. API keys are configured in a dictionary mapping
    keys to user information.

    Example config:
        api_keys:
          "sk-alice-secret-key":
            subject_type: "user"
            subject_id: "alice"
            tenant_id: "org_acme"  # Organization (metadata only)
            is_admin: true
          "sk-agent-secret-key":
            subject_type: "agent"
            subject_id: "agent_claude_001"
            tenant_id: "org_acme"
            is_admin: false
          "sk-service-backup-key":
            subject_type: "service"
            subject_id: "backup_service"
            tenant_id: null  # System-level service
            is_admin: true

    Security considerations:
    - Store keys in environment variables or secure config files
    - Use long, random keys (e.g., sk-<name>-<random-32-chars>)
    - Rotate keys periodically
    - For production, consider DatabaseAPIKeyAuth with expiry
    """

    def __init__(self, api_keys: dict[str, dict[str, Any]]):
        """Initialize static API key auth.

        Args:
            api_keys: Dictionary mapping API keys to user info:
                {
                    "api-key-string": {
                        "user_id": "username",
                        "tenant_id": "tenant" or None,
                        "is_admin": bool,
                        "metadata": {}  # Optional
                    }
                }
        """
        self.api_keys = api_keys
        logger.info(f"Initialized StaticAPIKeyAuth with {len(api_keys)} keys")

    async def authenticate(self, token: str) -> AuthResult:
        """Authenticate using static API key.

        P0-1: Validates token prefix for type discrimination

        Args:
            token: API key from Authorization header

        Returns:
            AuthResult with subject identity if valid
        """
        if not token:
            return AuthResult(authenticated=False)

        # P0-1: Validate key format (should start with sk-)
        if not token.startswith(API_KEY_PREFIX):
            logger.warning(f"UNAUTHORIZED: Static API key must start with {API_KEY_PREFIX}")
            return AuthResult(authenticated=False)

        if token not in self.api_keys:
            return AuthResult(authenticated=False)

        # Get user info from config
        user_info = self.api_keys[token]

        # Determine subject type (default to "user" for backward compatibility)
        subject_type = user_info.get("subject_type", "user")
        subject_id = user_info.get("subject_id") or user_info.get("user_id")  # Fallback to user_id

        return AuthResult(
            authenticated=True,
            subject_type=subject_type,
            subject_id=subject_id,
            tenant_id=user_info.get("tenant_id"),
            is_admin=user_info.get("is_admin", False),
            metadata=user_info.get("metadata"),
        )

    async def validate_token(self, token: str) -> bool:
        """Check if token exists in config.

        Args:
            token: API key

        Returns:
            True if key is valid
        """
        return token in self.api_keys

    def close(self) -> None:
        """Cleanup resources (no-op for static keys)."""
        pass

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "StaticAPIKeyAuth":
        """Create from configuration dictionary.

        Args:
            config: Configuration with "api_keys" field

        Returns:
            StaticAPIKeyAuth instance

        Example:
            config = {
                "api_keys": {
                    "sk-alice-xxx": {
                        "subject_type": "user",
                        "subject_id": "alice",
                        "is_admin": True
                    }
                }
            }
            auth = StaticAPIKeyAuth.from_config(config)
        """
        api_keys = config.get("api_keys", {})
        if not api_keys:
            logger.warning("No API keys configured in StaticAPIKeyAuth")
        return cls(api_keys)
