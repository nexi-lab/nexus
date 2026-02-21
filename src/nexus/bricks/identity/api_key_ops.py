"""API key creation and revocation utilities — re-export from storage tier.

Canonical implementation lives in ``nexus.storage.api_key_ops``.
This module re-exports for backward compatibility with identity-brick consumers.
"""

from nexus.storage.api_key_ops import (
    API_KEY_MIN_LENGTH,
    API_KEY_PREFIX,
    HMAC_SALT,
    create_api_key,
    hash_api_key,
    revoke_api_key,
    validate_key_format,
)

__all__ = [
    "API_KEY_MIN_LENGTH",
    "API_KEY_PREFIX",
    "HMAC_SALT",
    "create_api_key",
    "hash_api_key",
    "revoke_api_key",
    "validate_key_format",
]
