"""Base interface for secrets providers.

This module defines the abstract base class for all secrets providers.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class SecretsProvider(ABC):
    """Abstract base class for secrets providers.

    All secrets providers must implement this interface to provide
    a consistent API for secrets management across different backends.
    """

    @abstractmethod
    def get_secret(self, key: str, default: str | None = None) -> str | None:
        """Get a single secret by key.

        Args:
            key: The secret key/name to retrieve
            default: Default value if secret not found

        Returns:
            The secret value, or default if not found
        """
        pass

    @abstractmethod
    def get_secrets(self, keys: list[str]) -> dict[str, str | None]:
        """Get multiple secrets by keys.

        Args:
            keys: List of secret keys to retrieve

        Returns:
            Dictionary mapping keys to their values (None if not found)
        """
        pass

    def get_secret_dict(self, path: str) -> dict[str, Any]:
        """Get a dictionary of secrets at a path.

        This is useful for providers that support hierarchical secrets
        (e.g., OpenBao KV, GCP Secret Manager).

        Args:
            path: The path to the secrets (e.g., "nexus/api-keys")

        Returns:
            Dictionary of secrets at the path

        Raises:
            NotImplementedError: If the provider doesn't support this
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support hierarchical secrets"
        )

    def set_secret(self, key: str, value: str) -> None:
        """Set a secret value.

        Args:
            key: The secret key/name
            value: The secret value

        Raises:
            NotImplementedError: If the provider doesn't support writing
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support writing secrets"
        )

    def delete_secret(self, key: str) -> None:
        """Delete a secret.

        Args:
            key: The secret key/name to delete

        Raises:
            NotImplementedError: If the provider doesn't support deletion
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support deleting secrets"
        )

    def encrypt(self, plaintext: str, key_name: str = "default") -> str:
        """Encrypt data using the provider's encryption service.

        Args:
            plaintext: Data to encrypt
            key_name: Name of the encryption key to use

        Returns:
            Encrypted ciphertext

        Raises:
            NotImplementedError: If the provider doesn't support encryption
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support encryption"
        )

    def decrypt(self, ciphertext: str, key_name: str = "default") -> str:
        """Decrypt data using the provider's encryption service.

        Args:
            ciphertext: Data to decrypt
            key_name: Name of the encryption key to use

        Returns:
            Decrypted plaintext

        Raises:
            NotImplementedError: If the provider doesn't support decryption
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support decryption"
        )

    def is_available(self) -> bool:
        """Check if the secrets provider is available and configured.

        Returns:
            True if the provider is ready to use
        """
        return True

    def health_check(self) -> dict[str, Any]:
        """Perform a health check on the secrets provider.

        Returns:
            Dictionary with health status information
        """
        return {
            "provider": self.__class__.__name__,
            "available": self.is_available(),
        }
