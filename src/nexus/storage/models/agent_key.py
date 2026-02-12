"""AgentKeyModel — Ed25519 cryptographic key storage for agent identity.

Issue #1355 Phase 1: Every agent provisioned with generate_keys=True gets
an Ed25519 key pair. Public keys are stored as JWK JSON, private keys are
Fernet-encrypted before storage.

Design decisions:
- JWK Thumbprint (RFC 7638) as primary key (key_id).
- Fernet-encrypted private keys reuse OAuthCrypto infrastructure.
- 3 strategic indexes for agent lookups and active-key queries.
- String columns for algorithm (no PG ENUM) for forward compatibility.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class AgentKeyModel(Base):
    """Ed25519 cryptographic key for agent identity.

    Each agent can have multiple keys (rotation), but only one
    active (non-revoked, non-expired) key at a time.
    """

    __tablename__ = "agent_keys"

    key_id: Mapped[str] = mapped_column(String(64), primary_key=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    algorithm: Mapped[str] = mapped_column(String(20), nullable=False, default="Ed25519")
    public_key_jwk: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_private_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("idx_agent_keys_agent_id", "agent_id"),
        Index("idx_agent_keys_agent_active", "agent_id", "revoked_at"),
        Index("idx_agent_keys_zone", "zone_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentKeyModel(key_id={self.key_id!r}, agent_id={self.agent_id!r}, "
            f"algorithm={self.algorithm!r})>"
        )
