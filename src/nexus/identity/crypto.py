"""Core cryptographic operations for agent identity (Issue #1355, Decision #7B).

Cohesive crypto module handling:
- Ed25519 keypair generation and serialization
- Private key Fernet encryption/decryption (reuses OAuthCrypto)
- Ed25519 signing and verification
- Public key raw bytes extraction

Uses the `cryptography` library (already a dependency) for all operations.
No additional crypto dependencies required.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

if TYPE_CHECKING:
    from nexus.server.auth.oauth_crypto import OAuthCrypto

logger = logging.getLogger(__name__)


class IdentityCrypto:
    """Ed25519 + Fernet crypto for agent identity.

    Delegates Fernet encryption to OAuthCrypto (reuse, not reinvent).
    All methods are deterministic and side-effect free except generate_keypair
    (which uses OS CSPRNG).

    Args:
        oauth_crypto: Optional OAuthCrypto instance for Fernet encryption of
            private keys at rest. If None, private key encryption/decryption
            will raise ValueError.
    """

    def __init__(self, oauth_crypto: OAuthCrypto | None = None) -> None:
        self._oauth_crypto = oauth_crypto

    def generate_keypair(self) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
        """Generate a new Ed25519 keypair using OS CSPRNG.

        Returns:
            Tuple of (private_key, public_key).
        """
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        return private_key, public_key

    def encrypt_private_key(self, private_key: Ed25519PrivateKey) -> str:
        """Encrypt an Ed25519 private key for at-rest storage using Fernet.

        Serializes the private key to raw 32-byte format, then hex-encodes it
        before Fernet encryption (OAuthCrypto operates on strings).

        Args:
            private_key: Ed25519 private key to encrypt.

        Returns:
            Fernet-encrypted string (base64).

        Raises:
            ValueError: If OAuthCrypto is not configured.
        """
        if self._oauth_crypto is None:
            raise ValueError(
                "OAuthCrypto is required for private key encryption. "
                "Pass oauth_crypto to IdentityCrypto constructor."
            )
        raw_bytes = private_key.private_bytes(
            encoding=Encoding.Raw,
            format=PrivateFormat.Raw,
            encryption_algorithm=NoEncryption(),
        )
        hex_string = raw_bytes.hex()
        return self._oauth_crypto.encrypt_token(hex_string)

    def decrypt_private_key(self, encrypted: str) -> Ed25519PrivateKey:
        """Decrypt a Fernet-encrypted Ed25519 private key.

        Args:
            encrypted: Fernet-encrypted string from encrypt_private_key.

        Returns:
            Ed25519PrivateKey instance.

        Raises:
            ValueError: If OAuthCrypto is not configured or decryption fails.
        """
        if self._oauth_crypto is None:
            raise ValueError(
                "OAuthCrypto is required for private key decryption. "
                "Pass oauth_crypto to IdentityCrypto constructor."
            )
        hex_string = self._oauth_crypto.decrypt_token(encrypted)
        raw_bytes = bytes.fromhex(hex_string)
        return Ed25519PrivateKey.from_private_bytes(raw_bytes)

    def sign(self, message: bytes, private_key: Ed25519PrivateKey) -> bytes:
        """Sign a message with an Ed25519 private key.

        Args:
            message: Arbitrary bytes to sign.
            private_key: Signing key.

        Returns:
            64-byte Ed25519 signature.
        """
        return private_key.sign(message)

    def verify(
        self, message: bytes, signature: bytes, public_key: Ed25519PublicKey
    ) -> bool:
        """Verify an Ed25519 signature.

        Args:
            message: Original message bytes.
            signature: 64-byte signature to verify.
            public_key: Verification key.

        Returns:
            True if signature is valid, False otherwise.
        """
        try:
            public_key.verify(signature, message)
            return True
        except InvalidSignature:
            return False

    @staticmethod
    def public_key_to_bytes(public_key: Ed25519PublicKey) -> bytes:
        """Extract raw 32-byte public key bytes.

        Args:
            public_key: Ed25519 public key.

        Returns:
            Raw 32-byte public key bytes.
        """
        return public_key.public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )

    @staticmethod
    def public_key_from_bytes(raw_bytes: bytes) -> Ed25519PublicKey:
        """Reconstruct Ed25519 public key from raw bytes.

        Args:
            raw_bytes: 32-byte raw public key.

        Returns:
            Ed25519PublicKey instance.

        Raises:
            ValueError: If raw_bytes is not 32 bytes.
        """
        if len(raw_bytes) != 32:
            raise ValueError(f"Ed25519 public key must be 32 bytes, got {len(raw_bytes)}")
        return Ed25519PublicKey.from_public_bytes(raw_bytes)
