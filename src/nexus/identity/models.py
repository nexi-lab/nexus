"""SQLAlchemy models for agent identity (KYA — Issue #1355).

AgentKeyModel stores Ed25519 signing keys for agent identity.
Keys are separate from AgentRecordModel (Decision #2B) to support:
- Key rotation with grace period (multiple active keys per agent)
- Clean separation of concerns (identity keys vs lifecycle state)
- Matching the existing APIKeyModel pattern

Private keys are encrypted at rest using Fernet (AES-128 + HMAC-SHA256),
reusing the existing OAuthCrypto infrastructure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models import Base


def _generate_uuid() -> str:
    """Generate a UUID4 string for key_id."""
    return str(uuid.uuid4())


class AgentKeyModel(Base):
    """Ed25519 signing keys for agent identity (Issue #1355).

    Each agent can have multiple keys (for rotation with grace period).
    Active keys (is_active=True) are used for signing and verification.
    Old keys have expires_at set during rotation; after expiry they are
    no longer valid for verification.

    Private keys are Fernet-encrypted at rest. Public keys are stored as
    raw 32-byte Ed25519 public key bytes for fast DID derivation.
    """

    __tablename__ = "agent_keys"

    # Primary key — UUID referenced in RFC 9421 keyid parameter
    key_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
    )

    # Owner agent — references agent_records.agent_id
    agent_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Algorithm identifier (extensible for future algorithms)
    algorithm: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="Ed25519",
    )

    # Raw 32-byte Ed25519 public key (for fast DID derivation and verification)
    public_key_bytes: Mapped[bytes] = mapped_column(
        LargeBinary(32),
        nullable=False,
    )

    # Fernet-encrypted private key (base64 string from OAuthCrypto.encrypt_token)
    encrypted_private_key: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # DID derived from public key (e.g., "did:key:z6Mk...")
    did: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,
    )

    # Lifecycle
    is_active: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,  # SQLite-compatible bool: 1=True, 0=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,  # Null = no expiry. Set during key rotation.
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,  # Null = not revoked.
    )

    __table_args__ = (
        # Fast lookup: active keys for an agent (newest first for rotation)
        Index("idx_agent_keys_agent_active", "agent_id", "is_active"),
        # Unique DID per key (enforced at DB level)
        Index("idx_agent_keys_did", "did", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentKeyModel(key_id={self.key_id}, agent_id={self.agent_id}, "
            f"algorithm={self.algorithm}, is_active={self.is_active})>"
        )
