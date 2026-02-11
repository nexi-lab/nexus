"""FilePathModel — core table for virtual path mapping.

Issue #1246 Phase 4: Extracted from monolithic models.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default

if TYPE_CHECKING:
    from nexus.storage.models import FileMetadataModel


class FilePathModel(Base):
    """Core table for virtual path mapping.

    Maps virtual paths to physical backend locations.
    """

    __tablename__ = "file_paths"

    # Primary key
    path_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # P0 SECURITY: Defense-in-depth zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Path information
    virtual_path: Mapped[str] = mapped_column(Text, nullable=False)
    backend_id: Mapped[str] = mapped_column(String(36), nullable=False)
    physical_path: Mapped[str] = mapped_column(Text, nullable=False)

    # File properties
    file_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Semantic search indexing tracking (Issue #865)
    indexed_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Locking for concurrent access
    locked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Version tracking
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Issue #920: POSIX owner for O(1) permission checks
    posix_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    metadata_entries: Mapped[list[FileMetadataModel]] = relationship(
        "FileMetadataModel", back_populates="file_path", cascade="all, delete-orphan"
    )

    # Indexes and constraints
    __table_args__ = (
        Index(
            "uq_virtual_path",
            "virtual_path",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_file_paths_zone_path", "zone_id", "virtual_path"),
        Index("idx_file_paths_backend_id", "backend_id"),
        Index("idx_file_paths_content_hash", "content_hash"),
        Index("idx_file_paths_virtual_path", "virtual_path"),
        Index("idx_file_paths_accessed_at", "accessed_at"),
        Index("idx_file_paths_locked_by", "locked_by"),
        Index("idx_content_hash_zone", "content_hash", "zone_id"),
        Index("idx_file_paths_posix_uid", "posix_uid"),
        Index(
            "idx_file_paths_zone_path_covering",
            "zone_id",
            "virtual_path",
            postgresql_include=["path_id", "content_hash", "size_bytes", "updated_at", "file_type"],
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<FilePathModel(path_id={self.path_id}, virtual_path={self.virtual_path})>"

    def validate(self) -> None:
        """Validate file path model before database operations."""
        from nexus.core.exceptions import ValidationError

        if not self.virtual_path:
            raise ValidationError("virtual_path is required")

        if not self.virtual_path.startswith("/"):
            raise ValidationError(f"virtual_path must start with '/', got {self.virtual_path!r}")

        if "\x00" in self.virtual_path:
            raise ValidationError("virtual_path contains null bytes")

        if not self.backend_id:
            raise ValidationError("backend_id is required")

        if not self.physical_path:
            raise ValidationError("physical_path is required")

        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")
