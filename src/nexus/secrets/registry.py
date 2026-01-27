"""Secrets provider registry.

This module manages the secrets provider lifecycle and provides
a factory function to get the configured provider.
"""

import logging
import os
from typing import Type

from nexus.secrets.base import SecretsProvider
from nexus.secrets.env import EnvSecretsProvider

logger = logging.getLogger(__name__)

# Global provider instance (singleton)
_provider: SecretsProvider | None = None

# Registry of available providers
_providers: dict[str, Type[SecretsProvider]] = {
    "env": EnvSecretsProvider,
}


def register_provider(name: str, provider_class: Type[SecretsProvider]) -> None:
    """Register a secrets provider.

    Args:
        name: Provider name (e.g., "openbao", "gcp")
        provider_class: Provider class implementing SecretsProvider
    """
    _providers[name] = provider_class
    logger.debug(f"Registered secrets provider: {name}")


def get_secrets_provider(
    backend: str | None = None,
    **kwargs,
) -> SecretsProvider:
    """Get or create the secrets provider.

    The provider is determined by:
    1. Explicit backend parameter
    2. NEXUS_SECRETS_BACKEND environment variable
    3. Auto-detection (OpenBao if configured, otherwise env)

    Args:
        backend: Provider backend name ("env", "openbao", "gcp")
        **kwargs: Provider-specific configuration

    Returns:
        Configured SecretsProvider instance
    """
    global _provider

    # Determine backend
    if backend is None:
        backend = os.environ.get("NEXUS_SECRETS_BACKEND", "").lower()

    # Auto-detect if not specified
    if not backend:
        backend = _auto_detect_backend()

    # Return cached provider if same backend
    if _provider is not None:
        current_backend = type(_provider).__name__.lower().replace("secretsprovider", "")
        if backend in current_backend or current_backend in backend:
            return _provider

    # Create new provider
    _provider = _create_provider(backend, **kwargs)
    return _provider


def _auto_detect_backend() -> str:
    """Auto-detect the best available backend.

    Returns:
        Backend name ("openbao" or "env")
    """
    # Check for OpenBao configuration
    if os.environ.get("NEXUS_OPENBAO_ADDR") or os.environ.get("VAULT_ADDR"):
        if os.environ.get("NEXUS_OPENBAO_TOKEN") or os.environ.get("VAULT_TOKEN"):
            logger.info("Auto-detected OpenBao with token auth")
            return "openbao"
        if os.environ.get("NEXUS_OPENBAO_ROLE_ID"):
            logger.info("Auto-detected OpenBao with AppRole auth")
            return "openbao"

    # Default to environment variables
    return "env"


def _create_provider(backend: str, **kwargs) -> SecretsProvider:
    """Create a secrets provider instance.

    Args:
        backend: Backend name
        **kwargs: Provider configuration

    Returns:
        SecretsProvider instance
    """
    # Lazy load OpenBao provider to avoid import errors if httpx not installed
    if backend == "openbao":
        if "openbao" not in _providers:
            from nexus.secrets.openbao import OpenBaoClient
            _providers["openbao"] = OpenBaoClient

    if backend not in _providers:
        logger.warning(
            f"Unknown secrets backend '{backend}', falling back to 'env'"
        )
        backend = "env"

    provider_class = _providers[backend]
    provider = provider_class(**kwargs)

    logger.info(f"Initialized secrets provider: {provider_class.__name__}")
    return provider


def reset_provider() -> None:
    """Reset the cached provider.

    Useful for testing or reconfiguration.
    """
    global _provider
    _provider = None
