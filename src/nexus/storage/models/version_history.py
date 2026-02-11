"""VersionHistoryModel — unified version tracking for files and memories.

Issue #1246 Phase 4: Extracted from monolithic models.py.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class VersionHistoryModel(Base):
    """Version history tracking for files and memories.

    Unified version tracking system that works for:
    - File versions (SKILL.md, documents, etc.)
    - Memory versions (agent memories, facts, etc.)

    CAS-backed: Each version points to immutable content via content_hash.
    """

    __tablename__ = "version_history"

    # Primary key
    version_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Resource identification
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Version information
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Content metadata
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Lineage tracking
    parent_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("version_history.version_id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Change tracking
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Additional metadata (JSON)
    extra_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    parent_version: Mapped["VersionHistoryModel | None"] = relationship(  # noqa: UP037
        "VersionHistoryModel", remote_side=[version_id], foreign_keys=[parent_version_id]
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", "version_number", name="uq_version"),
        Index("idx_version_history_content_hash", "content_hash"),
        Index("idx_version_history_created_at", "created_at"),
        Index("idx_version_history_parent", "parent_version_id"),
        Index(
            "idx_version_history_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return f"<VersionHistoryModel(version_id={self.version_id}, resource_type={self.resource_type}, version={self.version_number})>"

    def validate(self) -> None:
        """Validate version history model before database operations."""
        from nexus.core.exceptions import ValidationError

        valid_types = ["file", "memory", "skill"]
        if self.resource_type not in valid_types:
            raise ValidationError(
                f"resource_type must be one of {valid_types}, got {self.resource_type}"
            )

        if not self.resource_id:
            raise ValidationError("resource_id is required")

        if self.version_number < 1:
            raise ValidationError(f"version_number must be >= 1, got {self.version_number}")

        if not self.content_hash:
            raise ValidationError("content_hash is required")

        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")
