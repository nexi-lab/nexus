"""Environment variable secrets provider.

This is the default provider that reads secrets from environment variables.
It maintains backwards compatibility with the existing Nexus configuration.
"""

import logging
import os
from typing import Any

from nexus.secrets.base import SecretsProvider

logger = logging.getLogger(__name__)

# Mapping of logical secret names to environment variable names
SECRET_ENV_MAPPING = {
    # LLM API Keys
    "openai_api_key": "OPENAI_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "tavily_api_key": "TAVILY_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "voyage_api_key": "VOYAGE_API_KEY",
    # Sandbox/Execution
    "e2b_api_key": "E2B_API_KEY",
    # Document Processing
    "unstructured_api_key": "UNSTRUCTURED_API_KEY",
    "llama_cloud_api_key": "LLAMA_CLOUD_API_KEY",
    "firecrawl_api_key": "FIRECRAWL_API_KEY",
    # MCP/Integration
    "klavis_api_key": "KLAVIS_API_KEY",
    # OAuth/Auth
    "oauth_encryption_key": "NEXUS_OAUTH_ENCRYPTION_KEY",
    "jwt_secret": "JWT_SECRET",
    # Database
    "database_url": "NEXUS_DATABASE_URL",
    # Google Cloud
    "gcs_bucket_name": "NEXUS_GCS_BUCKET_NAME",
    "gcs_project_id": "NEXUS_GCS_PROJECT_ID",
}


class EnvSecretsProvider(SecretsProvider):
    """Secrets provider that reads from environment variables.

    This is the default provider for backwards compatibility.
    It reads secrets from environment variables using a configurable
    mapping from logical secret names to env var names.

    Example:
        provider = EnvSecretsProvider()
        api_key = provider.get_secret("openai_api_key")
        # Reads from OPENAI_API_KEY environment variable
    """

    def __init__(
        self,
        prefix: str = "",
        mapping: dict[str, str] | None = None,
    ):
        """Initialize the environment secrets provider.

        Args:
            prefix: Optional prefix for all environment variable lookups
            mapping: Custom mapping of secret names to env var names
        """
        self._prefix = prefix
        self._mapping = {**SECRET_ENV_MAPPING, **(mapping or {})}

    def _get_env_name(self, key: str) -> str:
        """Get the environment variable name for a secret key.

        Args:
            key: The logical secret name

        Returns:
            The environment variable name
        """
        # Check if there's a mapping for this key
        if key in self._mapping:
            env_name = self._mapping[key]
        else:
            # Convert to uppercase with underscores
            env_name = key.upper()

        # Apply prefix if configured
        if self._prefix:
            return f"{self._prefix}_{env_name}"
        return env_name

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Get a secret from environment variables.

        Args:
            key: The logical secret name
            default: Default value if not found

        Returns:
            The secret value from the environment, or default
        """
        env_name = self._get_env_name(key)
        value = os.environ.get(env_name, "").strip()

        if value:
            logger.debug(f"EnvSecretsProvider: Loaded secret '{key}' from ${env_name}")
            return value

        if default is not None:
            logger.debug(
                f"EnvSecretsProvider: Secret '{key}' not found, using default"
            )
            return default

        logger.debug(f"EnvSecretsProvider: Secret '{key}' not found (${env_name})")
        return None

    def get_secrets(self, keys: list[str]) -> dict[str, str | None]:
        """Get multiple secrets from environment variables.

        Args:
            keys: List of logical secret names

        Returns:
            Dictionary mapping keys to their values
        """
        return {key: self.get_secret(key) for key in keys}

    def get_secret_dict(self, path: str) -> dict[str, Any]:
        """Get all secrets matching a path prefix.

        For environment variables, this returns all mapped secrets
        that start with the given prefix.

        Args:
            path: The prefix to match (e.g., "nexus/api-keys" matches api keys)

        Returns:
            Dictionary of matching secrets
        """
        # For env provider, we interpret path segments as key prefixes
        # "nexus/api-keys" -> match keys containing "api_key"
        prefix = path.replace("/", "_").replace("-", "_").lower()

        result = {}
        for key in self._mapping:
            if prefix in key or key.startswith(prefix):
                value = self.get_secret(key)
                if value:
                    result[key] = value

        return result

    def is_available(self) -> bool:
        """Check if the provider is available.

        Always returns True for environment provider.
        """
        return True

    def health_check(self) -> dict[str, Any]:
        """Perform a health check.

        Returns count of available secrets.
        """
        available_secrets = sum(
            1 for key in self._mapping if self.get_secret(key) is not None
        )

        return {
            "provider": "EnvSecretsProvider",
            "available": True,
            "secrets_configured": available_secrets,
            "total_mapped_secrets": len(self._mapping),
        }
