"""Secrets Service - Business logic for general-purpose secret storage.

Provides encrypted storage for credentials with:
- Version history (each put creates a new version)
- Soft delete and restore
- Enable/disable state
- Audit logging

Reuses:
- TokenEncryptor for Fernet encryption
- SecretsAuditLogger for audit trail
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.protocols.token_encryptor import TokenEncryptor
from nexus.contracts.secrets_access import AccessAuditContext
from nexus.storage.models.secret_store import SecretStoreModel, SecretStoreVersionModel
from nexus.storage.secrets_audit_logger import SecretsAuditLogger

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SecretDisabledError(Exception):
    """Raised when attempting to access a disabled secret."""

    pass


class SecretNotFoundError(Exception):
    """Raised when a secret is not found."""

    pass


class SecretsService:
    """Service for managing encrypted secrets with versioning and audit.

    Args:
        record_store: RecordStoreABC providing session factory
        oauth_crypto: TokenEncryptor instance for encryption/decryption
        audit_logger: SecretsAuditLogger instance for audit trail
    """

    def __init__(
        self,
        record_store: Any,  # RecordStoreABC
        oauth_crypto: TokenEncryptor,
        audit_logger: SecretsAuditLogger,
    ) -> None:
        self._session_factory = record_store.session_factory
        self._oauth_crypto = oauth_crypto
        self._audit_logger = audit_logger

    # -------------------------------------------------------------------------
    # Write Operations
    # -------------------------------------------------------------------------

    def _base_query(
        self, namespace: str, key: str, subject_id: str | None, subject_type: str | None = None
    ) -> Any:
        """Build a base query filtered by namespace, key, and optionally subject_id + subject_type."""
        stmt = select(SecretStoreModel).where(
            SecretStoreModel.namespace == namespace,
            SecretStoreModel.key == key,
        )
        if subject_id is not None:
            stmt = stmt.where(SecretStoreModel.subject_id == subject_id)
        if subject_type is not None:
            stmt = stmt.where(SecretStoreModel.subject_type == subject_type)
        return stmt

    def put_secret(
        self,
        namespace: str,
        key: str,
        value: str,
        description: str | None = None,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> dict[str, Any]:
        """Store or update a secret (creates a new version).

        Args:
            namespace: The namespace (e.g., 'channel:telegram:default')
            key: The key name (e.g., 'token')
            value: The secret value (will be encrypted)
            description: Optional description
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type (user/agent/service)

        Returns:
            Dict with id, namespace, key, version, created_at
        """
        encrypted_value = self._oauth_crypto.encrypt_token(value)

        with self._session_factory() as session:
            # Find existing secret or create new
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            existing = session.execute(stmt).scalar_one_or_none()

            if existing:
                # Update existing - create new version
                existing.description = description
                existing.current_version += 1
                existing.subject_id = subject_id
                existing.subject_type = subject_type
                new_version = existing.current_version

                version_model = SecretStoreVersionModel(
                    secret_id=existing.id,
                    version=new_version,
                    encrypted_value=encrypted_value,
                    created_at=datetime.now(UTC),
                )
                session.add(version_model)

                event_type = "credential_updated"
                secret_id = existing.id
                session.commit()
                session.refresh(existing)

                result = {
                    "id": existing.id,
                    "namespace": existing.namespace,
                    "key": existing.key,
                    "version": new_version,
                    "created_at": existing.created_at.isoformat() if existing.created_at else None,
                }
            else:
                # Create new secret
                secret_model = SecretStoreModel(
                    namespace=namespace,
                    key=key,
                    description=description,
                    enabled=1,  # SQLite bool
                    current_version=1,
                    created_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                    subject_id=subject_id,
                    subject_type=subject_type,
                )
                session.add(secret_model)
                session.flush()

                version_model = SecretStoreVersionModel(
                    secret_id=secret_model.id,
                    version=1,
                    encrypted_value=encrypted_value,
                    created_at=datetime.now(UTC),
                )
                session.add(version_model)

                event_type = "credential_created"
                secret_id = secret_model.id
                new_version = 1
                session.commit()
                session.refresh(secret_model)

                result = {
                    "id": secret_model.id,
                    "namespace": secret_model.namespace,
                    "key": secret_model.key,
                    "version": new_version,
                    "created_at": secret_model.created_at.isoformat()
                    if secret_model.created_at
                    else None,
                }

        # Audit log (outside session)
        try:
            self._audit_logger.log_event(
                event_type=event_type,
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key, "version": new_version},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return result

    def delete_secret(
        self,
        namespace: str,
        key: str,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Soft delete a secret (sets deleted_at timestamp).

        Args:
            namespace: The namespace
            key: The key name
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if deleted, False if not found
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type).where(
                SecretStoreModel.deleted_at.is_(None),
            )
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            secret.deleted_at = datetime.now(UTC)
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_revoked",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    def restore_secret(
        self,
        namespace: str,
        key: str,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Restore a soft-deleted secret.

        Args:
            namespace: The namespace
            key: The key name
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if restored, False if not found
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type).where(
                SecretStoreModel.deleted_at.isnot(None),
            )
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            secret.deleted_at = None
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_restored",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    def enable_secret(
        self,
        namespace: str,
        key: str,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Enable a secret.

        Args:
            namespace: The namespace
            key: The key name
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if enabled, False if not found
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            secret.enabled = 1
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_enabled",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    def disable_secret(
        self,
        namespace: str,
        key: str,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Disable a secret.

        Args:
            namespace: The namespace
            key: The key name
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if disabled, False if not found
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            secret.enabled = 0
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_disabled",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    def update_description(
        self,
        namespace: str,
        key: str,
        description: str,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Update the description of a secret.

        Args:
            namespace: The namespace
            key: The key name
            description: New description
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if updated, False if not found
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            secret.description = description
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_description_updated",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key, "description": description},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    # -------------------------------------------------------------------------
    # Read Operations
    # -------------------------------------------------------------------------

    def get_secret(
        self,
        namespace: str,
        key: str,
        actor_id: str = "system",
        version: int | None = None,
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
        audit_context: AccessAuditContext | None = None,
        audit_event_type: str = "key_accessed",
    ) -> dict[str, Any] | None:
        """Get a secret value (decrypted) with version info.

        Args:
            namespace: The namespace
            key: The key name
            actor_id: Who is performing the action
            version: Specific version to retrieve (default: latest)
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type
            audit_context: Optional caller context (access_context, client_id,
                agent_session) merged into the audit log ``details`` JSON.
            audit_event_type: Audit event type recorded for this read.
                Defaults to ``key_accessed``; domain wrappers that repurpose
                a read (e.g. TOTP generation) pass their own event type so
                audit queries can count purposes separately.

        Returns:
            Dict with 'value' and 'version' keys, or None if not found

        Raises:
            SecretDisabledError: If the secret is disabled
        """
        with self._session_factory() as session:
            # Find secret (exclude soft-deleted)
            stmt = self._base_query(namespace, key, subject_id, subject_type).where(
                SecretStoreModel.deleted_at.is_(None),
            )
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return None

            # Check if disabled
            if not secret.enabled:
                raise SecretDisabledError(f"Secret is disabled: {namespace}/{key}")

            # Find version
            if version is None:
                version = secret.current_version

            stmt = select(SecretStoreVersionModel).where(
                SecretStoreVersionModel.secret_id == secret.id,
                SecretStoreVersionModel.version == version,
            )
            version_model = session.execute(stmt).scalar_one_or_none()

            if not version_model:
                return None

            # Decrypt
            try:
                decrypted = self._oauth_crypto.decrypt_token(version_model.encrypted_value)
            except Exception as e:
                logger.error("Failed to decrypt secret: %s", e)
                return None

            secret_id = secret.id
            actual_version = version

        # Audit log
        try:
            details: dict[str, Any] = {
                "namespace": namespace,
                "key": key,
                "version": actual_version,
            }
            if audit_context is not None:
                details.update(audit_context.to_audit_details())
            self._audit_logger.log_event(
                event_type=audit_event_type,
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details=details,
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return {"value": decrypted, "version": actual_version}

    def list_secrets(
        self,
        namespace: str | None = None,
        include_deleted: bool = False,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List secrets (metadata only, no encrypted values).

        Args:
            namespace: Filter by namespace (optional)
            include_deleted: Include soft-deleted secrets
            subject_id: Filter by subject ID (optional)
            subject_type: Filter by subject type (optional)

        Returns:
            List of secret metadata dicts
        """
        with self._session_factory() as session:
            stmt = select(SecretStoreModel)

            if namespace:
                stmt = stmt.where(SecretStoreModel.namespace == namespace)

            if subject_id is not None:
                stmt = stmt.where(SecretStoreModel.subject_id == subject_id)

            if subject_type is not None:
                stmt = stmt.where(SecretStoreModel.subject_type == subject_type)

            if not include_deleted:
                stmt = stmt.where(SecretStoreModel.deleted_at.is_(None))

            stmt = stmt.order_by(SecretStoreModel.namespace, SecretStoreModel.key)
            secrets = session.execute(stmt).scalars().all()

            return [
                {
                    "id": s.id,
                    "namespace": s.namespace,
                    "key": s.key,
                    "description": s.description,
                    "enabled": bool(s.enabled),
                    "current_version": s.current_version,
                    "deleted_at": s.deleted_at.isoformat() if s.deleted_at else None,
                    "created_at": s.created_at.isoformat() if s.created_at else None,
                    "updated_at": s.updated_at.isoformat() if s.updated_at else None,
                    "subject_id": s.subject_id,
                    "subject_type": s.subject_type,
                }
                for s in secrets
            ]

    def list_versions(
        self,
        namespace: str,
        key: str,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """List version history for a secret.

        Args:
            namespace: The namespace
            key: The key name
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            List of version metadata (no encrypted values)
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return []

            stmt = (
                select(SecretStoreVersionModel)
                .where(SecretStoreVersionModel.secret_id == secret.id)
                .order_by(SecretStoreVersionModel.version.desc())
            )
            versions = session.execute(stmt).scalars().all()

            return [
                {
                    "version": v.version,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                }
                for v in versions
            ]

    def delete_version(
        self,
        namespace: str,
        key: str,
        version: int,
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> bool:
        """Delete a specific version (must keep at least one version).

        Args:
            namespace: The namespace
            key: The key name
            version: Version number to delete
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns this secret
            subject_type: The subject type

        Returns:
            True if deleted, False if not found or cannot delete
        """
        with self._session_factory() as session:
            stmt = self._base_query(namespace, key, subject_id, subject_type)
            secret = session.execute(stmt).scalar_one_or_none()

            if not secret:
                return False

            # Cannot delete if it's the only version
            if secret.current_version == 1 and version == 1:
                return False

            # Cannot delete current version
            if version == secret.current_version:
                return False

            stmt = select(SecretStoreVersionModel).where(
                SecretStoreVersionModel.secret_id == secret.id,
                SecretStoreVersionModel.version == version,
            )
            version_model = session.execute(stmt).scalar_one_or_none()

            if not version_model:
                return False

            session.delete(version_model)
            session.commit()

            secret_id = secret.id

        # Audit log
        try:
            self._audit_logger.log_event(
                event_type="credential_version_deleted",
                actor_id=actor_id,
                credential_id=secret_id,
                zone_id=zone_id,
                details={"namespace": namespace, "key": key, "version": version},
            )
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

        return True

    # -------------------------------------------------------------------------
    # Batch Operations
    # -------------------------------------------------------------------------

    def batch_put(
        self,
        secrets: list[dict[str, Any]],
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Batch write secrets.

        Args:
            secrets: List of dicts with namespace, key, value, description
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns these secrets
            subject_type: The subject type (user/agent/service)

        Returns:
            List of result dicts
        """
        results = []
        for s in secrets:
            result = self.put_secret(
                namespace=s["namespace"],
                key=s["key"],
                value=s["value"],
                description=s.get("description"),
                actor_id=actor_id,
                zone_id=zone_id,
                subject_id=subject_id,
                subject_type=subject_type,
            )
            results.append(result)
        return results

    def batch_get(
        self,
        queries: list[dict[str, Any]],
        actor_id: str = "system",
        zone_id: str = ROOT_ZONE_ID,
        subject_id: str | None = None,
        subject_type: str | None = None,
        audit_context: AccessAuditContext | None = None,
    ) -> dict[str, str]:
        """Batch read secrets.

        Args:
            queries: List of dicts with namespace, key, version (optional)
            actor_id: Who is performing the action
            zone_id: The zone ID
            subject_id: The subject ID who owns these secrets
            subject_type: The subject type
            audit_context: Forwarded to each ``get_secret`` call so every
                per-entry audit event carries the same caller tag.

        Returns:
            Dict mapping "namespace:key" to decrypted value
        """
        results = {}
        for q in queries:
            namespace = q["namespace"]
            key = q["key"]
            version = q.get("version")
            try:
                result = self.get_secret(
                    namespace=namespace,
                    key=key,
                    actor_id=actor_id,
                    version=version,
                    zone_id=zone_id,
                    subject_id=subject_id,
                    subject_type=subject_type,
                    audit_context=audit_context,
                )
                if result is not None:
                    results[f"{namespace}:{key}"] = result["value"]
            except SecretDisabledError:
                logger.warning("Secret disabled: %s/%s", namespace, key)
            except Exception as e:
                logger.error("Failed to get secret %s/%s: %s", namespace, key, e)
        return results
