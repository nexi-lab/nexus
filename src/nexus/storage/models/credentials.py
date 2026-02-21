"""SQLAlchemy model for agent verifiable credentials (Issue #1753).

Stores W3C JWT-VC credentials for agent capability attestation.
Credentials are separate from agent_keys (signing keys) — they represent
issued capability assertions rather than cryptographic identity.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid


class AgentCredentialModel(Base):
    """W3C Verifiable Credential for agent capabilities (Issue #1753).

    Each credential attests to a set of capabilities issued by one agent
    (issuer) to another (subject). The full JWT-VC compact form is stored
    for verification without recomputation.
    """

    __tablename__ = "agent_credentials"

    credential_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
    )

    issuer_did: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    subject_did: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    subject_agent_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    credential_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="AgentCapabilityCredential",
    )

    capabilities_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    constraints_json: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    jws_compact: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
    )

    valid_from: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )

    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    zone_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="root",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("idx_agent_creds_subject_status", "subject_agent_id", "status"),
        Index("idx_agent_creds_zone_status", "zone_id", "status"),
        Index("idx_agent_creds_issuer", "issuer_did"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentCredentialModel(credential_id={self.credential_id}, "
            f"subject={self.subject_agent_id}, status={self.status})>"
        )
