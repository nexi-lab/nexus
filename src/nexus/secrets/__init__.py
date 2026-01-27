"""Secrets management for Nexus.

This module provides a unified interface for secrets management with pluggable backends:
- Environment variables (default, backwards-compatible)
- OpenBao/Vault for enterprise deployments
- Google Secret Manager for GCP deployments

Usage:
    from nexus.secrets import get_secrets_provider

    # Get the configured provider
    provider = get_secrets_provider()

    # Read a secret
    api_key = provider.get_secret("openai_api_key")

    # Read multiple secrets
    secrets = provider.get_secrets(["openai_api_key", "anthropic_api_key"])
"""

from nexus.secrets.base import SecretsProvider
from nexus.secrets.env import EnvSecretsProvider
from nexus.secrets.registry import get_secrets_provider, register_provider

__all__ = [
    "SecretsProvider",
    "EnvSecretsProvider",
    "get_secrets_provider",
    "register_provider",
]

# Lazy imports for optional providers
def __getattr__(name: str):
    if name == "OpenBaoClient":
        from nexus.secrets.openbao import OpenBaoClient
        return OpenBaoClient
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
