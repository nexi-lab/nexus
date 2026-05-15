"""Password vault service — domain wrapper over SecretsService.

On-demand service (not a BackgroundService). Storage, encryption, audit,
versioning and soft-delete are delegated to SecretsService — this module
only provides a domain-typed API (VaultEntry in, VaultEntry out).
"""

from nexus.services.password_vault.schema import VaultEntry
from nexus.services.password_vault.service import (
    PasswordVaultService,
    VaultEntryNotFoundError,
)

__all__ = [
    "PasswordVaultService",
    "VaultEntry",
    "VaultEntryNotFoundError",
]
