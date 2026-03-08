"""Fernet encryption for user secrets.

Standalone crypto service for the secrets subsystem — does NOT share
keys with OAuthCrypto.  Each subsystem gets its own encryption key
stored under a distinct SystemSettings key.
"""

import logging
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken

if TYPE_CHECKING:
    from nexus.storage.record_store import RecordStoreABC

logger = logging.getLogger(__name__)

SECRETS_ENCRYPTION_KEY_NAME = "user_secrets_encryption_key"


class SecretsCrypto:
    """Fernet encryption service for user secrets.

    Key management mirrors OAuthCrypto's pattern but uses a separate
    ``SystemSettingsModel`` row (``user_secrets_encryption_key``) so
    rotating one subsystem's key never affects the other.
    """

    def __init__(
        self,
        encryption_key: str | None = None,
        *,
        record_store: "RecordStoreABC | None" = None,
    ) -> None:
        self._session_factory = record_store.session_factory if record_store else None

        if encryption_key is not None:
            self._init_fernet(encryption_key)
            return

        if record_store:
            db_key = self._load_or_create_key_from_db()
            if db_key:
                self._init_fernet(db_key)
                return

        logger.warning(
            "Generating random secrets encryption key. This key will NOT persist "
            "across restarts! Pass encryption_key or record_store for production use."
        )
        self._init_fernet(Fernet.generate_key().decode("utf-8"))

    def _init_fernet(self, encryption_key: str) -> None:
        try:
            self._fernet = Fernet(encryption_key.encode("utf-8"))
        except Exception as e:
            raise ValueError(f"Invalid encryption key: {e}") from e

    def _load_or_create_key_from_db(self) -> str | None:
        try:
            from sqlalchemy import select

            from nexus.storage.models import SystemSettingsModel

            if self._session_factory is None:
                return None

            with self._session_factory() as session:
                stmt = select(SystemSettingsModel).where(
                    SystemSettingsModel.key == SECRETS_ENCRYPTION_KEY_NAME
                )
                setting = session.execute(stmt).scalar_one_or_none()

                if setting:
                    return str(setting.value)

                new_key = Fernet.generate_key().decode("utf-8")
                new_setting = SystemSettingsModel(
                    key=SECRETS_ENCRYPTION_KEY_NAME,
                    value=new_key,
                    description="Fernet encryption key for user secrets",
                    is_sensitive=1,
                )
                session.add(new_setting)
                session.commit()
                logger.info("Generated and stored new secrets encryption key in database")
                return new_key

        except Exception:
            logger.warning("Failed to load/store secrets encryption key", exc_info=True)
            return None

    @staticmethod
    def generate_key() -> str:
        return Fernet.generate_key().decode("utf-8")

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            raise ValueError("Plaintext cannot be empty")
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        if not ciphertext:
            raise ValueError("Ciphertext cannot be empty")
        try:
            return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")
        except InvalidToken as e:
            raise InvalidToken(
                "Failed to decrypt secret. Value may be corrupted or key may have changed."
            ) from e
