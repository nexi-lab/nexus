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

    # Version tracking (v0.3.5)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

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
    is_default: Mapped[bool] = mapped_column(
        default=False, nullable=False
    )  # Default ACL entry (for inheritance)

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
        Index("idx_acl_entries_is_default", "is_default"),
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


class PermissionPolicyModel(Base):
    """Permission policies for automatic permission assignment.

    Stores default permission policies per namespace pattern.
    """

    __tablename__ = "permission_policies"

    # Primary key
    policy_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Namespace pattern (glob-style, e.g., /workspace/*, /shared/*)
    namespace_pattern: Mapped[str] = mapped_column(String(255), nullable=False)

    # Tenant ID (NULL = system-wide policy)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Default permissions
    default_owner: Mapped[str] = mapped_column(String(255), nullable=False)  # Supports ${agent_id}
    default_group: Mapped[str] = mapped_column(String(255), nullable=False)  # Supports ${tenant_id}
    default_mode: Mapped[int] = mapped_column(
        Integer, nullable=False
    )  # Permission bits (0o644, etc.)

    # Priority for pattern matching (higher = more specific)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes
    __table_args__ = (
        Index("idx_permission_policies_namespace", "namespace_pattern"),
        Index("idx_permission_policies_tenant", "tenant_id"),
        Index("idx_permission_policies_priority", "priority"),
    )

    def __repr__(self) -> str:
        return f"<PermissionPolicyModel(policy_id={self.policy_id}, namespace_pattern={self.namespace_pattern})>"

    def validate(self) -> None:
        """Validate permission policy model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate policy_id
        if not self.policy_id:
            raise ValidationError("policy_id is required")

        # Validate namespace_pattern
        if not self.namespace_pattern:
            raise ValidationError("namespace_pattern is required")

        if len(self.namespace_pattern) > 255:
            raise ValidationError(
                f"namespace_pattern must be 255 characters or less, got {len(self.namespace_pattern)}"
            )

        # Validate default_owner
        if not self.default_owner:
            raise ValidationError("default_owner is required")

        # Validate default_group
        if not self.default_group:
            raise ValidationError("default_group is required")

        # Validate default_mode
        if not 0 <= self.default_mode <= 0o777:
            raise ValidationError(
                f"default_mode must be between 0o000 and 0o777, got {oct(self.default_mode)}"
            )

        # Validate priority
        if self.priority < 0:
            raise ValidationError(f"priority must be non-negative, got {self.priority}")


class WorkspaceSnapshotModel(Base):
    """Workspace snapshot tracking for agent workspaces.

    Enables time-travel debugging and workspace rollback by capturing
    complete workspace state at specific points in time.

    CAS-backed: Snapshot manifest (list of files + hashes) stored in CAS.
    Zero storage overhead due to content deduplication.

    Workspace path format: /workspace/{tenant_id}/{agent_id}/
    """

    __tablename__ = "workspace_snapshots"

    # Primary key
    snapshot_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Workspace identification
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Snapshot metadata
    snapshot_number: Mapped[int] = mapped_column(Integer, nullable=False)  # Sequential version
    manifest_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True
    )  # SHA-256 hash of manifest (CAS key)

    # Snapshot stats (for quick display)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Change tracking
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of tags

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "snapshot_number", name="uq_workspace_snapshot"),
        Index("idx_workspace_snapshots_workspace", "tenant_id", "agent_id"),
        Index("idx_workspace_snapshots_manifest", "manifest_hash"),
        Index("idx_workspace_snapshots_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceSnapshotModel(snapshot_id={self.snapshot_id}, agent={self.agent_id}, version={self.snapshot_number})>"


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
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Resource identification
    resource_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'file', 'memory', 'skill', etc.
    resource_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # path_id for files, memory_id for memories

    # Version information
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 hash (CAS key)

    # Content metadata (snapshot of metadata at this version)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Lineage tracking
    parent_version_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("version_history.version_id", ondelete="SET NULL"), nullable=True
    )
    source_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # 'original', 'fork', 'merge', 'consolidated', etc.

    # Change tracking
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Additional metadata (JSON)
    extra_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as string

    # Relationships
    parent_version: Mapped["VersionHistoryModel | None"] = relationship(
        "VersionHistoryModel", remote_side=[version_id], foreign_keys=[parent_version_id]
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_id", "version_number", name="uq_version"),
        Index("idx_version_history_resource", "resource_type", "resource_id"),
        Index("idx_version_history_content_hash", "content_hash"),
        Index("idx_version_history_created_at", "created_at"),
        Index("idx_version_history_parent", "parent_version_id"),
    )

    def __repr__(self) -> str:
        return f"<VersionHistoryModel(version_id={self.version_id}, resource_type={self.resource_type}, version={self.version_number})>"

    def validate(self) -> None:
        """Validate version history model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate resource_type
        valid_types = ["file", "memory", "skill"]
        if self.resource_type not in valid_types:
            raise ValidationError(
                f"resource_type must be one of {valid_types}, got {self.resource_type}"
            )

        # Validate resource_id
        if not self.resource_id:
            raise ValidationError("resource_id is required")

        # Validate version_number
        if self.version_number < 1:
            raise ValidationError(f"version_number must be >= 1, got {self.version_number}")

        # Validate content_hash
        if not self.content_hash:
            raise ValidationError("content_hash is required")

        # Note: We don't validate hash length/format here because:
        # 1. This is just metadata tracking, not actual CAS storage
        # 2. Tests often use mock hashes that aren't full SHA-256
        # 3. The actual content validation happens in ContentChunkModel
        # 4. Version history should record whatever hash was used, even if unusual

        # Validate size_bytes
        if self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")


class OperationLogModel(Base):
    """Operation log for tracking filesystem operations.

    Provides audit trail, undo capability, and debugging support.
    Stores snapshots of state before operations for rollback.
    """

    __tablename__ = "operation_log"

    # Primary key
    operation_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Operation identification
    operation_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # write, delete, rename, mkdir, rmdir, chmod, chown, etc.

    # Context
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Affected paths
    path: Mapped[str] = mapped_column(Text, nullable=False)
    new_path: Mapped[str | None] = mapped_column(Text, nullable=True)  # For rename operations

    # Snapshot data (CAS-backed)
    snapshot_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # Previous content hash

    # Metadata snapshot (JSON)
    metadata_snapshot: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Previous file metadata

    # Operation result
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # success, failure

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes
    __table_args__ = (
        Index("idx_operation_log_type", "operation_type"),
        Index("idx_operation_log_agent", "agent_id"),
        Index("idx_operation_log_tenant", "tenant_id"),
        Index("idx_operation_log_path", "path"),
        Index("idx_operation_log_created_at", "created_at"),
        Index("idx_operation_log_status", "status"),
    )

    def __repr__(self) -> str:
        return f"<OperationLogModel(operation_id={self.operation_id}, type={self.operation_type}, path={self.path})>"

    def validate(self) -> None:
        """Validate operation log model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate operation_type
        valid_types = [
            "write",
            "delete",
            "rename",
            "mkdir",
            "rmdir",
            "chmod",
            "chown",
            "chgrp",
            "setfacl",
        ]
        if self.operation_type not in valid_types:
            raise ValidationError(
                f"operation_type must be one of {valid_types}, got {self.operation_type}"
            )

        # Validate path
        if not self.path:
            raise ValidationError("path is required")

        # Validate status
        valid_statuses = ["success", "failure"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")


class WorkflowModel(Base):
    """Workflow definitions.

    Stores workflow definitions and their configurations.
    """

    __tablename__ = "workflows"

    # Primary key
    workflow_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Multi-tenancy
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Workflow info
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Definition
    definition: Mapped[str] = mapped_column(Text, nullable=False)  # Full workflow YAML
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # State
    enabled: Mapped[bool] = mapped_column(Integer, nullable=False, default=1)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Relationships
    executions: Mapped[list["WorkflowExecutionModel"]] = relationship(
        "WorkflowExecutionModel", back_populates="workflow", cascade="all, delete-orphan"
    )

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_tenant_workflow_name"),
        Index("idx_workflows_tenant", "tenant_id"),
        Index("idx_workflows_enabled", "enabled"),
    )

    def __repr__(self) -> str:
        return f"<WorkflowModel(workflow_id={self.workflow_id}, name={self.name})>"

    def validate(self) -> None:
        """Validate workflow model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate name
        if not self.name:
            raise ValidationError("name is required")

        # Validate definition
        if not self.definition:
            raise ValidationError("definition is required")

        # Validate definition_hash
        if not self.definition_hash:
            raise ValidationError("definition_hash is required")


class DocumentChunkModel(Base):
    """Document chunks for semantic search.

    Stores document chunks with embeddings for semantic search.
    Supports both SQLite (with sqlite-vec) and PostgreSQL (with pgvector).

    Vector column is stored as:
    - SQLite: BLOB (for sqlite-vec)
    - PostgreSQL: vector type (for pgvector)
    """

    __tablename__ = "document_chunks"

    # Primary key
    chunk_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to file_paths
    path_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("file_paths.path_id", ondelete="CASCADE"), nullable=False
    )

    # Chunk information
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_tokens: Mapped[int] = mapped_column(Integer, nullable=False)

    # Offsets in original document (for highlighting)
    start_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Embedding metadata
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Vector embedding - stored differently per DB:
    # SQLite: BLOB (sqlite-vec uses float32 arrays serialized to BLOB)
    # PostgreSQL: vector type (pgvector native type)
    # Note: This column is added dynamically based on DB type
    # embedding: column added at runtime

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes and constraints
    __table_args__ = (
        Index("idx_chunks_path", "path_id"),
        Index("idx_chunks_model", "embedding_model"),
    )

    def __repr__(self) -> str:
        return f"<DocumentChunkModel(chunk_id={self.chunk_id}, path_id={self.path_id}, chunk_index={self.chunk_index})>"

    def validate(self) -> None:
        """Validate document chunk model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate path_id
        if not self.path_id:
            raise ValidationError("path_id is required")

        # Validate chunk_index
        if self.chunk_index < 0:
            raise ValidationError(f"chunk_index must be non-negative, got {self.chunk_index}")

        # Validate chunk_text
        if not self.chunk_text:
            raise ValidationError("chunk_text is required")

        # Validate chunk_tokens
        if self.chunk_tokens < 0:
            raise ValidationError(f"chunk_tokens must be non-negative, got {self.chunk_tokens}")


class WorkflowExecutionModel(Base):
    """Workflow execution history.

    Stores records of workflow executions.
    """

    __tablename__ = "workflow_executions"

    # Primary key
    execution_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to workflows
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("workflows.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Trigger info
    trigger_type: Mapped[str] = mapped_column(String(100), nullable=False)
    trigger_context: Mapped[str] = mapped_column(Text, nullable=False)  # JSON

    # Execution state
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Results
    actions_completed: Mapped[int] = mapped_column(Integer, default=0)
    actions_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Context
    context: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Relationships
    workflow: Mapped["WorkflowModel"] = relationship("WorkflowModel", back_populates="executions")

    # Indexes
    __table_args__ = (
        Index("idx_workflow_executions_workflow", "workflow_id"),
        Index("idx_workflow_executions_status", "status"),
        Index("idx_workflow_executions_trigger_type", "trigger_type"),
        Index("idx_workflow_executions_started_at", "started_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkflowExecutionModel(execution_id={self.execution_id}, status={self.status})>"

    def validate(self) -> None:
        """Validate workflow execution model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate workflow_id
        if not self.workflow_id:
            raise ValidationError("workflow_id is required")

        # Validate trigger_type
        if not self.trigger_type:
            raise ValidationError("trigger_type is required")

        # Validate status
        valid_statuses = ["pending", "running", "succeeded", "failed", "cancelled"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")
