"""User secrets service — encrypted key-value storage per user/zone.

Provides set/get/list/delete operations for user-managed secrets.
Values are encrypted at rest via SecretsCrypto (Fernet AES-128-CBC + HMAC-SHA256).
Every secret access emits an audit event to SecretsAuditLogger.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models.auth import UserSecretModel
from nexus.storage.models.secrets_audit_log import SecretsAuditEventType

if TYPE_CHECKING:
    from nexus.bricks.auth.secrets.crypto import SecretsCrypto
    from nexus.storage.record_store import RecordStoreABC
    from nexus.storage.secrets_audit_logger import SecretsAuditLogger

logger = logging.getLogger(__name__)


class UserSecretsService:
    """Encrypted key-value secret storage scoped to (user_id, zone_id).

    Args:
        record_store: RecordStoreABC providing session factories.
        crypto: SecretsCrypto instance for Fernet encrypt/decrypt.
        audit_logger: SecretsAuditLogger for access auditing (optional).
    """

    def __init__(
        self,
        record_store: "RecordStoreABC",
        crypto: "SecretsCrypto",
        audit_logger: "SecretsAuditLogger | None" = None,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._crypto = crypto
        self._audit_logger = audit_logger

    def set_secret(
        self,
        *,
        user_id: str,
        name: str,
        value: str,
        zone_id: str = ROOT_ZONE_ID,
    ) -> str:
        """Create or update a user secret. Returns the secret_id."""
        encrypted = self._crypto.encrypt(value)

        with self._session_factory() as session:
            stmt = select(UserSecretModel).where(
                UserSecretModel.user_id == user_id,
                UserSecretModel.zone_id == zone_id,
                UserSecretModel.name == name,
            )
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                existing.encrypted_value = encrypted
                existing.updated_at = datetime.now(UTC)
                secret_id = existing.secret_id
                session.commit()
                logger.info("Updated secret %r for user=%s zone=%s", name, user_id, zone_id)
            else:
                row = UserSecretModel(
                    user_id=user_id,
                    zone_id=zone_id,
                    name=name,
                    encrypted_value=encrypted,
                )
                session.add(row)
                session.flush()
                secret_id = row.secret_id
                session.commit()
                logger.info("Created secret %r for user=%s zone=%s", name, user_id, zone_id)

        return secret_id

    def get_secret_value(
        self,
        *,
        user_id: str,
        name: str,
        zone_id: str = ROOT_ZONE_ID,
        ip_address: str | None = None,
    ) -> str | None:
        """Retrieve and decrypt a secret value. Emits audit event on access."""
        with self._session_factory() as session:
            stmt = select(UserSecretModel).where(
                UserSecretModel.user_id == user_id,
                UserSecretModel.zone_id == zone_id,
                UserSecretModel.name == name,
            )
            row = session.execute(stmt).scalar_one_or_none()

        if row is None:
            return None

        value = self._crypto.decrypt(row.encrypted_value)

        if self._audit_logger:
            try:
                self._audit_logger.log_event(
                    event_type=SecretsAuditEventType.KEY_ACCESSED,
                    actor_id=user_id,
                    credential_id=row.secret_id,
                    zone_id=zone_id,
                    ip_address=ip_address,
                    details={"secret_name": name},
                )
            except Exception:
                logger.warning("Failed to log secret access audit event", exc_info=True)

        return value

    def list_secrets(
        self,
        *,
        user_id: str,
        zone_id: str = ROOT_ZONE_ID,
    ) -> list[dict[str, Any]]:
        """List secret metadata (names only, never values) for a user/zone."""
        with self._session_factory() as session:
            stmt = (
                select(UserSecretModel)
                .where(
                    UserSecretModel.user_id == user_id,
                    UserSecretModel.zone_id == zone_id,
                )
                .order_by(UserSecretModel.name)
            )
            rows = session.execute(stmt).scalars().all()

        return [
            {
                "secret_id": row.secret_id,
                "name": row.name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
            for row in rows
        ]

    def delete_secret(
        self,
        *,
        user_id: str,
        name: str,
        zone_id: str = ROOT_ZONE_ID,
    ) -> bool:
        """Delete a user secret. Returns True if found and deleted."""
        with self._session_factory() as session:
            stmt = select(UserSecretModel).where(
                UserSecretModel.user_id == user_id,
                UserSecretModel.zone_id == zone_id,
                UserSecretModel.name == name,
            )
            row = session.execute(stmt).scalar_one_or_none()
            if row is None:
                return False
            session.delete(row)
            session.commit()

        logger.info("Deleted secret %r for user=%s zone=%s", name, user_id, zone_id)
        return True
