"""SQLAlchemy models for agent identity (KYA -- Issue #1355).

AgentKeyModel stores Ed25519 signing keys for agent identity.
Keys are separate from agent lifecycle state (Decision #2B) to support:
- Key rotation with grace period (multiple active keys per agent)
- Clean separation of concerns (identity keys vs lifecycle state)
- Matching the existing APIKeyModel pattern

Private keys are encrypted at rest using Fernet (AES-128 + HMAC-SHA256),
reusing the existing OAuthCrypto infrastructure.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid


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

    # Primary key -- UUID referenced in RFC 9421 keyid parameter
    key_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
    )

    # Owner agent -- references agent_records.agent_id
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


class AgentCredentialModel(Base):
    """JWT-VC capability credentials for agent attestation (Issue #1753).

    Each row represents an issued verifiable credential that attests to
    an agent's capabilities.  Credentials are signed by the kernel's
    Ed25519 key and stored as JWT strings.

    Credentials can form delegation chains via ``parent_credential_id``.
    Revoking a credential does NOT automatically cascade to children —
    child credentials should be revoked explicitly or via
    ``revoke_by_signing_key``.
    """

    __tablename__ = "agent_credentials"

    # Primary key — URN UUID (e.g. "urn:uuid:abc123...")
    credential_id: Mapped[str] = mapped_column(
        String(80),
        primary_key=True,
    )

    # Issuer DID (kernel or delegating agent)
    issuer_did: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Subject DID (the agent receiving capabilities)
    subject_did: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # Subject agent_id (for queries by agent)
    subject_agent_id: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        index=True,
    )

    # Signing key ID — references agent_keys.key_id
    signing_key_id: Mapped[str] = mapped_column(
        String(36),
        nullable=False,
    )

    # The full JWT-VC string (for re-verification)
    jwt_token: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # JSON-serialized capabilities array (for queries without JWT decode)
    capabilities_json: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    # Delegation chain
    parent_credential_id: Mapped[str | None] = mapped_column(
        String(80),
        nullable=True,
    )
    delegation_depth: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
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
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )

    __table_args__ = (
        # Fast lookup: active credentials for an agent
        Index("idx_agent_creds_agent_active", "subject_agent_id", "is_active"),
        # Lookup by issuer
        Index("idx_agent_creds_issuer", "issuer_did"),
        # Cascade revocation by signing key
        Index("idx_agent_creds_signing_key", "signing_key_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentCredentialModel(credential_id={self.credential_id}, "
            f"subject_agent_id={self.subject_agent_id}, "
            f"is_active={self.is_active}, depth={self.delegation_depth})>"
        )
