"""Immutable audit log for secrets and OAuth credential operations.

Issue #997: Append-only audit trail for all token and credential
lifecycle events.  Follows the ExchangeAuditLogModel pattern —
SHA-256 self-hash per record for tamper detection, with SQLAlchemy
event guards to reject UPDATE/DELETE at the ORM level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class SecretsAuditEventType(StrEnum):
    """Types of auditable secrets/credential events."""

    CREDENTIAL_CREATED = "credential_created"
    CREDENTIAL_UPDATED = "credential_updated"
    CREDENTIAL_REVOKED = "credential_revoked"
    TOKEN_REFRESHED = "token_refreshed"
    TOKEN_ROTATED = "token_rotated"
    TOKEN_REUSE_DETECTED = "token_reuse_detected"
    FAMILY_INVALIDATED = "family_invalidated"
    KEY_ACCESSED = "key_accessed"
    KEY_ROTATED = "key_rotated"


class SecretsAuditLogModel(Base):
    """Immutable audit log for secrets and credential operations.

    Records are append-only: updates and deletes are rejected by
    SQLAlchemy event guards (registered in secrets_audit_logger.py).
    """

    __tablename__ = "secrets_audit_log"

    # Composite primary key — partition-ready (same pattern as ExchangeAuditLogModel)
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Self-hash for tamper detection (SHA-256 hex digest)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Event classification
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Actor (who performed the action)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Subject (what was affected)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    token_family_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Context
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    # Additional details (JSON text — must NEVER contain secrets)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        # Actor lookups by time
        Index("idx_secrets_audit_actor_created", "actor_id", "created_at"),
        # Zone + time scans
        Index("idx_secrets_audit_zone_created", "zone_id", "created_at"),
        # Event type filtering
        Index("idx_secrets_audit_event_type", "event_type"),
        # Credential history
        Index("idx_secrets_audit_credential", "credential_id"),
        # Token family tracking
        Index("idx_secrets_audit_family", "token_family_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<SecretsAuditLog(id={self.id}, event_type={self.event_type}, "
            f"actor={self.actor_id}, provider={self.provider})>"
        )
