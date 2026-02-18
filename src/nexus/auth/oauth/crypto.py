"""OAuth token encryption utilities (moved from server/auth/oauth_crypto.py).

Fernet symmetric encryption (AES-128 CBC + HMAC-SHA256) for OAuth tokens.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

OAUTH_ENCRYPTION_KEY_NAME = "oauth_encryption_key"


class OAuthCrypto:
    """OAuth token encryption service using Fernet."""

    def __init__(
        self,
        encryption_key: str | None = None,
        *,
        session_factory: Any = None,
    ) -> None:
        self._session_factory = session_factory

        if encryption_key is not None:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: Using explicit encryption key")
            self._init_fernet(encryption_key)
            return

        if session_factory:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("OAuthCrypto: Trying to load key from database")
            db_key = self._load_or_create_key_from_db()
            if db_key:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "OAuthCrypto: Loaded key from database (starts with: %s...)",
                        db_key[:10],
                    )
                self._init_fernet(db_key)
                return

        logger.warning(
            "Generating random OAuth encryption key. This key will NOT persist "
            "across restarts! Pass encryption_key or session_factory "
            "for production use."
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

    def _load_or_create_key_from_db(self) -> str | None:
        try:
            from sqlalchemy import select

            from nexus.storage.models import SystemSettingsModel

            if self._session_factory is None:
                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug("OAuthCrypto: No session_factory — cannot load key")
                return None

            Session = self._session_factory

            with Session() as session:
                stmt = select(SystemSettingsModel).where(
                    SystemSettingsModel.key == OAUTH_ENCRYPTION_KEY_NAME
                )
                setting = session.execute(stmt).scalar_one_or_none()

                if setting:
                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug(
                            "OAuthCrypto: Loaded key from database (key=%s, value starts with: %s...)",
                            OAUTH_ENCRYPTION_KEY_NAME,
                            setting.value[:10],
                        )
                    return str(setting.value)

                if logger.isEnabledFor(logging.DEBUG):
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

        except Exception:
            logger.warning("Failed to load/store encryption key from database", exc_info=True)
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
