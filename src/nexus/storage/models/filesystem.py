"""Filesystem storage models — directory entries, file metadata, content chunks, snapshots, caching.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

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

from nexus.core.exceptions import ValidationError
from nexus.storage.models._base import Base, uuid_pk

if TYPE_CHECKING:
    from nexus.storage.models.file_path import FilePathModel


class DirectoryEntryModel(Base):
    """Sparse directory index for O(1) non-recursive listings (Issue #924).

    Stores parent-child relationships at the directory level rather than file level.
    This enables fast non-recursive directory listings without scanning all descendants.
    """

    __tablename__ = "directory_entries"

    zone_id: Mapped[str] = mapped_column(
        String(255), primary_key=True, nullable=False, default="default"
    )
    parent_path: Mapped[str] = mapped_column(String(4096), primary_key=True, nullable=False)
    entry_name: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)

    entry_type: Mapped[str] = mapped_column(String(10), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("idx_directory_entries_lookup", "zone_id", "parent_path"),
        Index(
            "idx_directory_entries_parent_prefix",
            "parent_path",
            postgresql_ops={"parent_path": "text_pattern_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<DirectoryEntryModel(zone={self.zone_id}, parent={self.parent_path}, name={self.entry_name}, type={self.entry_type})>"

    def validate(self) -> None:
        """Validate directory entry model before database operations."""
        if not self.parent_path:
            raise ValidationError("parent_path is required")
        if not self.parent_path.startswith("/"):
            raise ValidationError(f"parent_path must start with '/', got {self.parent_path!r}")
        if not self.parent_path.endswith("/"):
            raise ValidationError(f"parent_path must end with '/', got {self.parent_path!r}")
        if not self.entry_name:
            raise ValidationError("entry_name is required")
        if "/" in self.entry_name:
            raise ValidationError(f"entry_name cannot contain '/', got {self.entry_name!r}")
        if self.entry_type not in ("file", "directory"):
            raise ValidationError(
                f"entry_type must be 'file' or 'directory', got {self.entry_type!r}"
            )


class FileMetadataModel(Base):
    """File metadata storage — stores arbitrary key-value metadata for files."""

    __tablename__ = "file_metadata"

    metadata_id: Mapped[str] = uuid_pk()

    path_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_paths.path_id", ondelete="CASCADE"), nullable=False
    )

    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    file_path: Mapped[FilePathModel] = relationship(
        "FilePathModel", back_populates="metadata_entries"
    )

    __table_args__ = (
        Index("idx_file_metadata_path_id", "path_id"),
        Index("idx_file_metadata_key", "key"),
    )

    def __repr__(self) -> str:
        return f"<FileMetadataModel(metadata_id={self.metadata_id}, key={self.key})>"

    def validate(self) -> None:
        """Validate file metadata model before database operations."""
        if not self.path_id:
            raise ValidationError("path_id is required")
        if not self.key:
            raise ValidationError("metadata key is required")
        if len(self.key) > 255:
            raise ValidationError(
                f"metadata key must be 255 characters or less, got {len(self.key)}"
            )


class ContentChunkModel(Base):
    """Content chunks for deduplication.

    Stores unique content chunks identified by hash, with reference counting
    for garbage collection.
    """

    __tablename__ = "content_chunks"

    chunk_id: Mapped[str] = uuid_pk()

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)

    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    protected_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_content_chunks_ref_count", "ref_count"),
        Index("idx_content_chunks_last_accessed", "last_accessed_at"),
    )

    def __repr__(self) -> str:
        return f"<ContentChunkModel(chunk_id={self.chunk_id}, content_hash={self.content_hash}, ref_count={self.ref_count})>"

    def validate(self) -> None:
        """Validate content chunk model before database operations."""
        if not self.content_hash:
            raise ValidationError("content_hash is required")
        if len(self.content_hash) != 64:
            raise ValidationError(
                f"content_hash must be 64 characters (SHA-256), got {len(self.content_hash)}"
            )
        try:
            int(self.content_hash, 16)
        except ValueError:
            raise ValidationError("content_hash must contain only hexadecimal characters") from None
        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")
        if not self.storage_path:
            raise ValidationError("storage_path is required")
        if self.ref_count is not None and self.ref_count < 0:
            raise ValidationError(f"ref_count cannot be negative, got {self.ref_count}")


class WorkspaceSnapshotModel(Base):
    """Workspace snapshot tracking for registered workspaces.

    Enables time-travel debugging and workspace rollback by capturing
    complete workspace state at specific points in time.
    """

    __tablename__ = "workspace_snapshots"

    snapshot_id: Mapped[str] = uuid_pk()

    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)

    snapshot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    manifest_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        UniqueConstraint("workspace_path", "snapshot_number", name="uq_workspace_snapshot"),
        Index("idx_workspace_snapshots_workspace_path", "workspace_path"),
        Index("idx_workspace_snapshots_manifest", "manifest_hash"),
        Index("idx_workspace_snapshots_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceSnapshotModel(snapshot_id={self.snapshot_id}, workspace={self.workspace_path}, version={self.snapshot_number})>"


class DocumentChunkModel(Base):
    """Document chunks for semantic search.

    Stores document chunks with embeddings for semantic search.
    Supports both SQLite (with sqlite-vec) and PostgreSQL (with pgvector).
    """

    __tablename__ = "document_chunks"

    chunk_id: Mapped[str] = uuid_pk()

    path_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_paths.path_id", ondelete="CASCADE"), nullable=False
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)

    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Contextual chunking fields (Issue #1192)
    chunk_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_chunks_path", "path_id"),
        Index("idx_chunks_model", "embedding_model"),
        Index("idx_chunks_source_doc", "source_document_id"),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunkModel(chunk_id={self.chunk_id}, path_id={self.path_id}, chunk_index={self.chunk_index})>"

    def validate(self) -> None:
        """Validate document chunk model before database operations."""
        if not self.path_id:
            raise ValidationError("path_id is required")
        if self.chunk_index < 0:
            raise ValidationError(f"chunk_index must be non-negative, got {self.chunk_index}")
        if not self.chunk_text:
            raise ValidationError("chunk_text is required")
        if self.chunk_tokens < 0:
            raise ValidationError(f"chunk_tokens must be non-negative, got {self.chunk_tokens}")
