"""OAuth token encryption utilities (moved from server/auth/oauth_crypto.py).

Fernet symmetric encryption (AES-128 CBC + HMAC-SHA256) for OAuth tokens.

Key resolution order (first match wins):
    1. Explicit ``encryption_key`` parameter
    2. Database-backed ``settings_store``
    3. ``NEXUS_OAUTH_ENCRYPTION_KEY`` environment variable
    4. Random ephemeral key (development only — warns loudly)
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from nexus.contracts.auth_store_protocols import SystemSettingsStoreProtocol

logger = logging.getLogger(__name__)

OAUTH_ENCRYPTION_KEY_NAME = "oauth_encryption_key"
OAUTH_ENCRYPTION_KEY_ENV = "NEXUS_OAUTH_ENCRYPTION_KEY"


class OAuthCrypto:
    """OAuth token encryption service using Fernet."""

    def __init__(
        self,
        encryption_key: str | None = None,
        *,
        settings_store: "SystemSettingsStoreProtocol | None" = None,
    ) -> None:
        self._settings_store = settings_store

        # 1. Explicit key (highest priority)
        if encryption_key is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: Using explicit encryption key")
            self._init_fernet(encryption_key)
            return

        # 2. Database-backed settings store
        if settings_store:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: Trying to load key from settings store")
            db_key = self._load_or_create_key()
            if db_key:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "OAuthCrypto: Loaded key from settings store (starts with: %s...)",
                        db_key[:10],
                    )
                self._init_fernet(db_key)
                return

        # 3. Environment variable — shared key between CLI and server processes
        env_key = os.environ.get(OAUTH_ENCRYPTION_KEY_ENV)
        if env_key:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: Using key from %s", OAUTH_ENCRYPTION_KEY_ENV)
            self._init_fernet(env_key)
            return

        # 4. Random ephemeral key (development fallback)
        logger.warning(
            "Generating random OAuth encryption key. This key will NOT persist "
            "across restarts! Set %s or pass encryption_key for production use.",
            OAUTH_ENCRYPTION_KEY_ENV,
        )
        key_bytes: bytes = Fernet.generate_key()
        self._init_fernet(key_bytes.decode("utf-8"))

    def _init_fernet(self, encryption_key: str) -> None:
        if isinstance(encryption_key, str):
            encryption_key_bytes = encryption_key.encode("utf-8")
        else:
            encryption_key_bytes = encryption_key

        try:
            self._fernet = Fernet(encryption_key_bytes)
        except Exception as e:
            raise ValueError(f"Invalid encryption key: {e}") from e

    def _load_or_create_key(self) -> str | None:
        try:
            if self._settings_store is None:
                return None

            setting = self._settings_store.get_setting(OAUTH_ENCRYPTION_KEY_NAME)

            if setting:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "OAuthCrypto: Loaded key from store (key=%s, value starts with: %s...)",
                        OAUTH_ENCRYPTION_KEY_NAME,
                        setting.value[:10],
                    )
                return setting.value

            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: No key found in store, generating new one")
            new_key = Fernet.generate_key().decode("utf-8")

            self._settings_store.set_setting(
                OAUTH_ENCRYPTION_KEY_NAME,
                new_key,
                description="Fernet encryption key for OAuth token encryption",
            )

            logger.info("Generated and stored new OAuth encryption key")
            return new_key

        except Exception:
            logger.warning("Failed to load/store encryption key", exc_info=True)
            return None

    @staticmethod
    def generate_key() -> str:
        key_bytes: bytes = Fernet.generate_key()
        return key_bytes.decode("utf-8")

    def encrypt_token(self, token: str) -> str:
        if not token:
            raise ValueError("Token cannot be empty")
        token_bytes = token.encode("utf-8")
        encrypted_bytes: bytes = self._fernet.encrypt(token_bytes)
        return encrypted_bytes.decode("utf-8")

    def decrypt_token(self, encrypted_token: str) -> str:
        if not encrypted_token:
            raise ValueError("Encrypted token cannot be empty")
        try:
            encrypted_bytes = encrypted_token.encode("utf-8")
            decrypted_bytes: bytes = self._fernet.decrypt(encrypted_bytes)
            return decrypted_bytes.decode("utf-8")
        except InvalidToken as e:
            raise InvalidToken("Failed to decrypt token. Token may be corrupted or expired.") from e

    async def decrypt_token_async(self, encrypted_token: str) -> str:
        """Non-blocking decrypt via ``asyncio.to_thread()``."""
        return await asyncio.to_thread(self.decrypt_token, encrypted_token)

    def encrypt_dict(self, data: dict[str, Any]) -> str:
        if not data:
            raise ValueError("Data cannot be empty")
        import json

        try:
            json_str = json.dumps(data)
            return self.encrypt_token(json_str)
        except (TypeError, ValueError) as e:
            raise ValueError(f"Failed to serialize data: {e}") from e

    def decrypt_dict(self, encrypted_data: str) -> dict[str, Any]:
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
        old_crypto = OAuthCrypto(encryption_key=old_key)
        decrypted = old_crypto.decrypt_token(encrypted_token)
        new_crypto = OAuthCrypto(encryption_key=new_key)
        return new_crypto.encrypt_token(decrypted)
