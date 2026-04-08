"""Protocol interface for Fernet token encryption/decryption.

Satisfied by OAuthCrypto and any compatible implementation.
Used by bricks that need encryption without cross-brick imports.
"""

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenEncryptor(Protocol):
    """Protocol for Fernet token encryption/decryption."""

    def encrypt_token(self, token: str) -> str: ...

    def decrypt_token(self, encrypted: str) -> str: ...
