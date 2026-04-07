"""Secret Store - Generic key-value secrets storage with versioning.

Provides encrypted storage for credentials with version history,
soft delete, and enable/disable state management.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default

if TYPE_CHECKING:
    pass


class SecretStoreModel(Base):
    """Main table for storing secret metadata.

    Stores the namespace, key name, current version, and soft-delete state.
    The actual encrypted secret value is stored in SecretStoreVersionModel.
    """

    __tablename__ = "secret_store"
    __table_args__ = (
        UniqueConstraint("namespace", "key", "subject_id", "subject_type", name="uq_secret_store_ns_key_subject"),
        Index("idx_secret_store_namespace", "namespace"),
        Index("idx_secret_store_deleted_at", "deleted_at"),
        Index("idx_secret_store_subject", "subject_id"),
        Index("idx_secret_store_ns_key_subject", "namespace", "key", "subject_id", "subject_type"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    namespace: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)  # SQLite doesn't have bool
    deleted_at: Mapped[int | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # Subject association (who owns this secret)
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(20), nullable=True, default="user")

    # Relationship to versions
    versions: Mapped[list["SecretStoreVersionModel"]] = relationship(
        "SecretStoreVersionModel",
        back_populates="secret",
        cascade="all, delete-orphan",
        order_by="desc(SecretStoreVersionModel.version)",
    )

    def __repr__(self) -> str:
        return f"<SecretStore(id={self.id}, namespace={self.namespace}, key={self.key}, enabled={self.enabled})>"


class SecretStoreVersionModel(Base):
    """Stores encrypted secret values with version history.

    Each put_secret operation creates a new version record.
    Old versions are retained for rollback and audit purposes.
    """

    __tablename__ = "secret_store_versions"
    __table_args__ = (
        UniqueConstraint("secret_id", "version", name="uq_secret_store_version_secret_version"),
        Index("idx_secret_store_versions_secret_id", "secret_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    secret_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("secret_store.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Relationship to parent
    secret: Mapped["SecretStoreModel"] = relationship("SecretStoreModel", back_populates="versions")

    def __repr__(self) -> str:
        return f"<SecretStoreVersion(id={self.id}, secret_id={self.secret_id}, version={self.version})>"
