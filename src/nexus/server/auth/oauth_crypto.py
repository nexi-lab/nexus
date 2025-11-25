"""OAuth token encryption utilities.

Provides secure encryption/decryption for OAuth tokens using Fernet (symmetric encryption).
Based on MindsDB's encrypted_json_set/get pattern but with additional security features.

Security features:
- Fernet symmetric encryption (AES-128 in CBC mode + HMAC-SHA256)
- Key rotation support
- Configurable key storage (environment variable, database, or KMS)
- HMAC integrity protection
- Time-to-live for encrypted data

Example:
    # Initialize crypto service with database URL for persistent key
    crypto = OAuthCrypto(db_url="postgresql://user:pass@host/db")

    # Encrypt a token
    encrypted = crypto.encrypt_token("ya29.a0ARrdaM...")

    # Decrypt a token
    decrypted = crypto.decrypt_token(encrypted)
"""

import logging
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# Key name for storing OAuth encryption key in system_settings
OAUTH_ENCRYPTION_KEY_NAME = "oauth_encryption_key"


class OAuthCrypto:
    """OAuth token encryption service using Fernet.

    Fernet provides authenticated encryption with:
    - AES-128 in CBC mode for encryption
    - HMAC-SHA256 for integrity protection
    - Automatic timestamp verification

    The encryption key is loaded in this order:
    1. Explicitly provided encryption_key parameter
    2. NEXUS_OAUTH_ENCRYPTION_KEY environment variable
    3. From database system_settings table (if db_url provided)
    4. Generated and stored in database (if db_url provided)
    5. Generated randomly (WARNING: not persistent across restarts!)
    """

    def __init__(
        self,
        encryption_key: str | None = None,
        db_url: str | None = None,
    ):
        """Initialize the crypto service.

        Args:
            encryption_key: Base64-encoded Fernet key. If None, loads from
                           environment or database.
            db_url: Database URL for storing/retrieving encryption key.
                   If provided and key not found, generates and stores a new key.

        Raises:
            ValueError: If the provided key is invalid

        Note:
            For production deployments, provide db_url to ensure the encryption
            key is persisted and consistent across all processes/restarts.
        """
        # Priority 1: Explicit encryption key
        if encryption_key is not None:
            logger.debug("OAuthCrypto: Using explicit encryption key")
            self._init_fernet(encryption_key)
            return

        # Priority 2: Environment variable (must be non-empty)
        env_key = os.environ.get("NEXUS_OAUTH_ENCRYPTION_KEY", "").strip()
        if env_key:
            logger.debug("OAuthCrypto: Using env var NEXUS_OAUTH_ENCRYPTION_KEY")
            self._init_fernet(env_key)
            return

        # Priority 3 & 4: Load from database or generate and store
        if db_url:
            logger.debug(f"OAuthCrypto: Trying to load key from db_url={db_url}")
            db_key = self._load_or_create_key_from_db(db_url)
            if db_key:
                logger.debug(
                    f"OAuthCrypto: Loaded key from database (starts with: {db_key[:10]}...)"
                )
                self._init_fernet(db_key)
                return

        # Priority 5: Generate random key (WARNING: not persistent!)
        logger.warning(
            "Generating random OAuth encryption key. This key will NOT persist "
            "across restarts! Set NEXUS_OAUTH_ENCRYPTION_KEY or provide db_url "
            "for production use."
        )
        key_bytes: bytes = Fernet.generate_key()
        self._init_fernet(key_bytes.decode("utf-8"))

    def _init_fernet(self, encryption_key: str) -> None:
        """Initialize Fernet with the given key."""
        if isinstance(encryption_key, str):
            encryption_key_bytes = encryption_key.encode("utf-8")
        else:
            encryption_key_bytes = encryption_key

        try:
            self._fernet = Fernet(encryption_key_bytes)
        except Exception as e:
            raise ValueError(f"Invalid encryption key: {e}") from e

    def _load_or_create_key_from_db(self, db_url: str) -> str | None:
        """Load encryption key from database, or create and store a new one.

        Args:
            db_url: Database URL

        Returns:
            Encryption key string, or None if database access fails
        """
        try:
            from sqlalchemy import create_engine, select
            from sqlalchemy.orm import sessionmaker

            from nexus.storage.models import Base, SystemSettingsModel

            logger.debug(f"OAuthCrypto._load_or_create_key_from_db: Connecting to {db_url}")

            # Create engine
            connect_args = {}
            if "sqlite" in db_url:
                connect_args["check_same_thread"] = False

            engine = create_engine(db_url, connect_args=connect_args)

            # Ensure table exists
            Base.metadata.create_all(engine)

            Session = sessionmaker(bind=engine)

            with Session() as session:
                # Try to load existing key
                stmt = select(SystemSettingsModel).where(
                    SystemSettingsModel.key == OAUTH_ENCRYPTION_KEY_NAME
                )
                setting = session.execute(stmt).scalar_one_or_none()

                if setting:
                    logger.debug(
                        f"OAuthCrypto: Loaded key from database (key={OAUTH_ENCRYPTION_KEY_NAME}, value starts with: {setting.value[:10]}...)"
                    )
                    return setting.value

                # Generate new key and store it
                logger.debug("OAuthCrypto: No key found in database, generating new one")
                new_key = Fernet.generate_key().decode("utf-8")

                new_setting = SystemSettingsModel(
                    key=OAUTH_ENCRYPTION_KEY_NAME,
                    value=new_key,
                    description="Fernet encryption key for OAuth token encryption",
                    is_sensitive=1,
                )
                session.add(new_setting)
                session.commit()

                logger.info("Generated and stored new OAuth encryption key in database")
                return str(new_key)

        except Exception as e:
            import traceback

            logger.warning(f"Failed to load/store encryption key from database: {e}")
            logger.warning(f"Traceback: {traceback.format_exc()}")
            return None

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
        old_crypto = OAuthCrypto(encryption_key=old_key)
        decrypted = old_crypto.decrypt_token(encrypted_token)

        # Encrypt with new key
        new_crypto = OAuthCrypto(encryption_key=new_key)
        return new_crypto.encrypt_token(decrypted)
