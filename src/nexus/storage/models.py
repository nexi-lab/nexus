"""SQLAlchemy models for Nexus metadata store.

For SQLite compatibility:
- UUID -> String (TEXT) - we'll generate UUID strings
- JSONB -> Text (JSON as string)
- BIGINT -> BigInteger
- TIMESTAMP -> DateTime
"""

import uuid
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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all SQLAlchemy models."""

    pass


class FilePathModel(Base):
    """Core table for virtual path mapping.

    Maps virtual paths to physical backend locations.
    """

    __tablename__ = "file_paths"

    # Primary key
    path_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Multi-tenancy (simplified for embedded - single tenant)
    tenant_id: Mapped[str] = mapped_column(
        String(36), nullable=False, default=lambda: str(uuid.uuid4())
    )

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
    accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # For cache eviction decisions
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Locking for concurrent access
    locked_by: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Worker/process ID that locked this file

    # UNIX-style permissions (v0.3.0)
    owner: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Owner user ID
    group: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Group ID
    mode: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Permission bits (0o644, etc.)

    # Relationships
    metadata_entries: Mapped[list["FileMetadataModel"]] = relationship(
        "FileMetadataModel", back_populates="file_path", cascade="all, delete-orphan"
    )
    acl_entries: Mapped[list["ACLEntryModel"]] = relationship(
        "ACLEntryModel", back_populates="file_path", cascade="all, delete-orphan"
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("tenant_id", "virtual_path", name="uq_tenant_virtual_path"),
        Index("idx_file_paths_tenant_id", "tenant_id"),
        Index("idx_file_paths_backend_id", "backend_id"),
        Index("idx_file_paths_content_hash", "content_hash"),
        Index("idx_file_paths_virtual_path", "virtual_path"),
        Index("idx_file_paths_accessed_at", "accessed_at"),
        Index("idx_file_paths_locked_by", "locked_by"),
        Index("idx_file_paths_owner", "owner"),
        Index("idx_file_paths_group", "group"),
    )

    def __repr__(self) -> str:
        return f"<FilePathModel(path_id={self.path_id}, virtual_path={self.virtual_path})>"

    def validate(self) -> None:
        """Validate file path model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate virtual_path
        if not self.virtual_path:
            raise ValidationError("virtual_path is required")

        if not self.virtual_path.startswith("/"):
            raise ValidationError(f"virtual_path must start with '/', got {self.virtual_path!r}")

        # Check for null bytes and control characters
        if "\x00" in self.virtual_path:
            raise ValidationError("virtual_path contains null bytes")

        # Validate backend_id
        if not self.backend_id:
            raise ValidationError("backend_id is required")

        # Validate physical_path
        if not self.physical_path:
            raise ValidationError("physical_path is required")

        # Validate size_bytes
        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")

        # Validate tenant_id
        if not self.tenant_id:
            raise ValidationError("tenant_id is required")


class FileMetadataModel(Base):
    """File metadata storage.

    Stores arbitrary key-value metadata for files.
    """

    __tablename__ = "file_metadata"

    # Primary key
    metadata_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to file_paths
    path_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_paths.path_id", ondelete="CASCADE"), nullable=False
    )

    # Metadata key-value
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as string

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Relationships
    file_path: Mapped["FilePathModel"] = relationship(
        "FilePathModel", back_populates="metadata_entries"
    )

    # Indexes
    __table_args__ = (
        Index("idx_file_metadata_path_id", "path_id"),
        Index("idx_file_metadata_key", "key"),
    )

    def __repr__(self) -> str:
        return f"<FileMetadataModel(metadata_id={self.metadata_id}, key={self.key})>"

    def validate(self) -> None:
        """Validate file metadata model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate path_id
        if not self.path_id:
            raise ValidationError("path_id is required")

        # Validate key
        if not self.key:
            raise ValidationError("metadata key is required")

        if len(self.key) > 255:
            raise ValidationError(
                f"metadata key must be 255 characters or less, got {len(self.key)}"
            )


class ACLEntryModel(Base):
    """Access Control List entries for files.

    Stores ACL entries for fine-grained permission control beyond
    traditional UNIX permissions.
    """

    __tablename__ = "acl_entries"

    # Primary key
    acl_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to file_paths
    path_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_paths.path_id", ondelete="CASCADE"), nullable=False
    )

    # ACL entry details
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)  # user, group, mask, other
    identifier: Mapped[str | None] = mapped_column(String(255), nullable=True)  # username/groupname
    permissions: Mapped[str] = mapped_column(String(10), nullable=False)  # rwx format
    deny: Mapped[bool] = mapped_column(default=False, nullable=False)  # Deny entry flag

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Relationships
    file_path: Mapped["FilePathModel"] = relationship("FilePathModel", back_populates="acl_entries")

    # Indexes
    __table_args__ = (
        Index("idx_acl_entries_path_id", "path_id"),
        Index("idx_acl_entries_type_id", "entry_type", "identifier"),
    )

    def __repr__(self) -> str:
        return f"<ACLEntryModel(acl_id={self.acl_id}, entry_type={self.entry_type}, identifier={self.identifier})>"

    def validate(self) -> None:
        """Validate ACL entry model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate path_id
        if not self.path_id:
            raise ValidationError("path_id is required")

        # Validate entry_type
        valid_types = ["user", "group", "mask", "other"]
        if self.entry_type not in valid_types:
            raise ValidationError(f"entry_type must be one of {valid_types}, got {self.entry_type}")

        # Validate identifier for user/group entries
        if self.entry_type in ("user", "group"):
            if not self.identifier:
                raise ValidationError(f"{self.entry_type} entry requires identifier")
        elif self.entry_type in ("mask", "other") and self.identifier is not None:
            raise ValidationError(f"{self.entry_type} entry cannot have identifier")

        # Validate permissions format
        if not self.permissions:
            raise ValidationError("permissions is required")
        if len(self.permissions) != 3:
            raise ValidationError(
                f"permissions must be 3 characters (rwx format), got {self.permissions}"
            )
        for i, c in enumerate(self.permissions):
            expected = ["r", "w", "x"][i]
            if c not in (expected, "-"):
                raise ValidationError(f"invalid permission character at position {i}: {c}")


class ContentChunkModel(Base):
    """Content chunks for deduplication.

    Stores unique content chunks identified by hash, with reference counting
    for garbage collection.
    """

    __tablename__ = "content_chunks"

    # Primary key
    chunk_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Content identification
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Reference counting for garbage collection
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    protected_until: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Grace period before garbage collection

    # Indexes
    __table_args__ = (
        Index("idx_content_chunks_hash", "content_hash"),
        Index("idx_content_chunks_ref_count", "ref_count"),
        Index("idx_content_chunks_last_accessed", "last_accessed_at"),
    )

    def __repr__(self) -> str:
        return f"<ContentChunkModel(chunk_id={self.chunk_id}, content_hash={self.content_hash}, ref_count={self.ref_count})>"

    def validate(self) -> None:
        """Validate content chunk model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate content_hash
        if not self.content_hash:
            raise ValidationError("content_hash is required")

        # SHA-256 hashes are 64 hex characters
        if len(self.content_hash) != 64:
            raise ValidationError(
                f"content_hash must be 64 characters (SHA-256), got {len(self.content_hash)}"
            )

        # Check if hash contains only valid hex characters
        try:
            int(self.content_hash, 16)
        except ValueError:
            raise ValidationError("content_hash must contain only hexadecimal characters") from None

        # Validate size_bytes
        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")

        # Validate storage_path
        if not self.storage_path:
            raise ValidationError("storage_path is required")

        # Validate ref_count
        if self.ref_count < 0:
            raise ValidationError(f"ref_count cannot be negative, got {self.ref_count}")
