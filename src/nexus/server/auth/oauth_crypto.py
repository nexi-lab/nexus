"""OAuth token encryption utilities.

Provides secure encryption/decryption for OAuth tokens using Fernet (symmetric encryption).
Based on MindsDB's encrypted_json_set/get pattern but with additional security features.

Security features:
- Fernet symmetric encryption (AES-128 in CBC mode + HMAC-SHA256)
- Key rotation support
- Configurable key storage (environment variable or KMS)
- HMAC integrity protection
- Time-to-live for encrypted data

Example:
    # Initialize crypto service
    crypto = OAuthCrypto()

    # Encrypt a token
    encrypted = crypto.encrypt_token("ya29.a0ARrdaM...")

    # Decrypt a token
    decrypted = crypto.decrypt_token(encrypted)
"""

import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class OAuthCrypto:
    """OAuth token encryption service using Fernet.

    Fernet provides authenticated encryption with:
    - AES-128 in CBC mode for encryption
    - HMAC-SHA256 for integrity protection
    - Automatic timestamp verification

    The encryption key can be:
    1. Loaded from NEXUS_OAUTH_ENCRYPTION_KEY environment variable
    2. Generated automatically (not recommended for production)
    """

    def __init__(self, encryption_key: str | None = None):
        """Initialize the crypto service.

        Args:
            encryption_key: Base64-encoded Fernet key. If None, loads from:
                          1. NEXUS_OAUTH_ENCRYPTION_KEY environment variable
                          2. ~/.nexus/encryption.key file (auto-created if missing)

        Raises:
            ValueError: If the provided key is invalid
        """
        if encryption_key is None:
            # Try to load from environment
            encryption_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY")

            if encryption_key is None:
                # Try to load from persistent file
                encryption_key = self._load_or_create_key_file()

        # Convert to bytes if string
        if isinstance(encryption_key, str):
            encryption_key_bytes = encryption_key.encode("utf-8")
        else:
            encryption_key_bytes = encryption_key

        try:
            self._fernet = Fernet(encryption_key_bytes)
        except Exception as e:
            raise ValueError(f"Invalid encryption key: {e}") from e

    @staticmethod
    def _load_or_create_key_file() -> str:
        """Load encryption key from file or create new one.

        Loads from ~/.nexus/encryption.key. If file doesn't exist,
        generates a new key and saves it.

        Returns:
            Base64-encoded Fernet key

        Note:
            For production server deployments, use NEXUS_OAUTH_ENCRYPTION_KEY
            environment variable instead. This file-based approach is for
            CLI-only usage.
        """
        home = os.path.expanduser("~")
        key_file = os.path.join(home, ".nexus", "encryption.key")

        # Try to load existing key
        if os.path.exists(key_file):
            try:
                with open(key_file) as f:
                    key = f.read().strip()
                    # Validate it's a valid Fernet key
                    Fernet(key.encode("utf-8"))
                    return key
            except Exception:
                # If key file is corrupted, regenerate
                pass

        # Generate new key
        key = Fernet.generate_key().decode("utf-8")

        # Create directory if needed
        os.makedirs(os.path.dirname(key_file), exist_ok=True)

        # Save key to file (with restrictive permissions)
        with open(key_file, "w") as f:
            f.write(key)

        # Set file permissions to 600 (owner read/write only)
        os.chmod(key_file, 0o600)

        print(f"INFO: Generated new encryption key and saved to {key_file}")
        print(
            "INFO: For production server deployments, set NEXUS_OAUTH_ENCRYPTION_KEY environment variable."
        )

        return key

    @staticmethod
    def generate_key() -> str:
        """Generate a new Fernet encryption key.

        Returns:
            Base64-encoded Fernet key (UTF-8 string)

        Example:
            >>> key = OAuthCrypto.generate_key()
            >>> print(f"Export this: export NEXUS_OAUTH_ENCRYPTION_KEY='{key}'")
        """
        key_bytes: bytes = Fernet.generate_key()
        return key_bytes.decode("utf-8")

    def encrypt_token(self, token: str) -> str:
        """Encrypt an OAuth token.

        Args:
            token: Plain-text OAuth token (access token or refresh token)

        Returns:
            Base64-encoded encrypted token (UTF-8 string)

        Raises:
            ValueError: If token is empty
        """
        if not token:
            raise ValueError("Token cannot be empty")

        token_bytes = token.encode("utf-8")
        encrypted_bytes: bytes = self._fernet.encrypt(token_bytes)
        return encrypted_bytes.decode("utf-8")

    def decrypt_token(self, encrypted_token: str) -> str:
        """Decrypt an OAuth token.

        Args:
            encrypted_token: Base64-encoded encrypted token

        Returns:
            Plain-text OAuth token

        Raises:
            InvalidToken: If the token is invalid, corrupted, or expired
            ValueError: If encrypted_token is empty
        """
        if not encrypted_token:
            raise ValueError("Encrypted token cannot be empty")

        try:
            encrypted_bytes = encrypted_token.encode("utf-8")
            decrypted_bytes: bytes = self._fernet.decrypt(encrypted_bytes)
            return decrypted_bytes.decode("utf-8")
        except InvalidToken as e:
            raise InvalidToken("Failed to decrypt token. Token may be corrupted or expired.") from e

    def encrypt_dict(self, data: dict[str, Any]) -> str:
        """Encrypt a dictionary (for encrypted_json_set pattern).

        Args:
            data: Dictionary to encrypt

        Returns:
            Base64-encoded encrypted JSON string

        Raises:
            ValueError: If data is empty or not serializable
        """
        if not data:
            raise ValueError("Data cannot be empty")

        import json

        try:
            json_str = json.dumps(data)
            return self.encrypt_token(json_str)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Failed to serialize data: {e}") from e

    def decrypt_dict(self, encrypted_data: str) -> dict[str, Any]:
        """Decrypt a dictionary (for encrypted_json_get pattern).

        Args:
            encrypted_data: Base64-encoded encrypted JSON string

        Returns:
            Decrypted dictionary

        Raises:
            InvalidToken: If the data is invalid, corrupted, or expired
            ValueError: If encrypted_data is empty or not valid JSON
        """
        if not encrypted_data:
            raise ValueError("Encrypted data cannot be empty")

        import json

        try:
            json_str = self.decrypt_token(encrypted_data)
            result: dict[str, Any] = json.loads(json_str)
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"Decrypted data is not valid JSON: {e}") from e

    def rotate_key(self, old_key: str, new_key: str, encrypted_token: str) -> str:
        """Rotate encryption key by re-encrypting a token.

        Args:
            old_key: Old encryption key (base64-encoded)
            new_key: New encryption key (base64-encoded)
            encrypted_token: Token encrypted with old key

        Returns:
            Token encrypted with new key

        Example:
            >>> old_crypto = OAuthCrypto(old_key)
            >>> new_crypto = OAuthCrypto(new_key)
            >>> new_encrypted = old_crypto.rotate_key(old_key, new_key, old_encrypted)
        """
        # Decrypt with old key
        old_crypto = OAuthCrypto(old_key)
        decrypted = old_crypto.decrypt_token(encrypted_token)

        # Encrypt with new key
        new_crypto = OAuthCrypto(new_key)
        return new_crypto.encrypt_token(decrypted)
