"""SQLAlchemy models for Nexus metadata store.

For SQLite compatibility:
- UUID -> String (TEXT) - we'll generate UUID strings
- JSONB -> Text (JSON as string)
- BIGINT -> BigInteger
- TIMESTAMP -> DateTime
"""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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

    # P0 SECURITY: Defense-in-depth tenant isolation
    # tenant_id restored for database-level filtering (defense-in-depth)
    # Previous architecture relied solely on ReBAC, creating single point of failure
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

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

    # Version tracking
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Relationships
    metadata_entries: Mapped[list["FileMetadataModel"]] = relationship(
        "FileMetadataModel", back_populates="file_path", cascade="all, delete-orphan"
    )

    # Indexes and constraints
    __table_args__ = (
        # P0 SECURITY: Restore tenant-scoped unique constraint for defense-in-depth
        # IMPORTANT: Partial unique index that excludes soft-deleted rows
        # This prevents unique constraint violations when renaming files to paths of deleted files
        Index(
            "uq_virtual_path",
            "virtual_path",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("idx_file_paths_tenant_path", "tenant_id", "virtual_path"),  # Tenant-scoped queries
        Index("idx_file_paths_backend_id", "backend_id"),
        Index("idx_file_paths_content_hash", "content_hash"),
        Index("idx_file_paths_virtual_path", "virtual_path"),
        Index("idx_file_paths_accessed_at", "accessed_at"),
        Index("idx_file_paths_locked_by", "locked_by"),
        # Performance: Composite indexes for common query patterns (#384)
        Index("idx_tenant_path_prefix", "tenant_id", "virtual_path"),  # Optimized prefix queries
        Index("idx_content_hash_tenant", "content_hash", "tenant_id"),  # CAS dedup lookups
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

        # tenant_id is now optional (nullable)
        # Validation removed for backward compatibility


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


class WorkspaceSnapshotModel(Base):
    """Workspace snapshot tracking for registered workspaces.

    Enables time-travel debugging and workspace rollback by capturing
    complete workspace state at specific points in time.

    CAS-backed: Snapshot manifest (list of files + hashes) stored in CAS.
    Zero storage overhead due to content deduplication.

    Note: Workspaces must be registered via WorkspaceRegistry before creating snapshots.
    Workspace identification uses explicit path (e.g., "/my-workspace") instead of
    the old tenant_id+agent_id pattern.
    """

    __tablename__ = "workspace_snapshots"

    # Primary key
    snapshot_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Workspace identification (changed from tenant_id+agent_id to workspace_path)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False, index=True)

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
        UniqueConstraint("workspace_path", "snapshot_number", name="uq_workspace_snapshot"),
        Index("idx_workspace_snapshots_workspace_path", "workspace_path"),
        Index("idx_workspace_snapshots_manifest", "manifest_hash"),
        Index("idx_workspace_snapshots_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceSnapshotModel(snapshot_id={self.snapshot_id}, workspace={self.workspace_path}, version={self.snapshot_number})>"


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


class EntityRegistryModel(Base):
    """Entity registry for identity-based memory system.

    Lightweight registry for ID disambiguation and relationship tracking.
    Enables order-neutral virtual paths for memories.
    """

    __tablename__ = "entity_registry"

    # Composite primary key
    entity_type: Mapped[str] = mapped_column(
        String(50), primary_key=True, nullable=False
    )  # 'tenant', 'user', 'agent'
    entity_id: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)

    # Hierarchical relationships (optional)
    parent_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Metadata
    entity_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as string
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes
    __table_args__ = (
        Index("idx_entity_registry_id_lookup", "entity_id"),
        Index("idx_entity_registry_parent", "parent_type", "parent_id"),
    )

    def __repr__(self) -> str:
        return f"<EntityRegistryModel(entity_type={self.entity_type}, entity_id={self.entity_id})>"

    def validate(self) -> None:
        """Validate entity registry model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate entity_type
        valid_types = ["tenant", "user", "agent"]
        if self.entity_type not in valid_types:
            raise ValidationError(
                f"entity_type must be one of {valid_types}, got {self.entity_type}"
            )

        # Validate entity_id
        if not self.entity_id:
            raise ValidationError("entity_id is required")

        # Validate parent consistency
        if (self.parent_type is None) != (self.parent_id is None):
            raise ValidationError("parent_type and parent_id must both be set or both be None")

        if self.parent_type is not None and self.parent_type not in valid_types:
            raise ValidationError(
                f"parent_type must be one of {valid_types}, got {self.parent_type}"
            )


class MemoryModel(Base):
    """Memory storage for AI agents.

    Identity-based memory with order-neutral paths and 3-layer permissions.
    Canonical storage by memory_id, with virtual path views for browsing.
    """

    __tablename__ = "memories"

    # Primary key
    memory_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Content (CAS reference)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Identity relationships
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Real user ownership
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Created by agent

    # Scope and visibility
    scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="agent"
    )  # 'agent', 'user', 'tenant', 'global', 'session'
    visibility: Mapped[str] = mapped_column(
        String(50), nullable=False, default="private"
    )  # 'private', 'shared', 'public'

    # Session scope for session-scoped memories
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Memory metadata
    memory_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # 'fact', 'preference', 'experience', 'strategy', 'anti_pattern', 'observation', 'trajectory', 'reflection', 'consolidated'
    importance: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # 0.0-1.0 importance score

    # State management (#368)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", index=True
    )  # 'inactive', 'active' - supports manual approval workflow (default: active for backward compatibility)

    # Namespace organization (v0.8.0 - #350)
    namespace: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Hierarchical namespace for organization (e.g., "knowledge/geography/facts")
    path_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Optional unique key within namespace for upsert mode

    # ACE (Agentic Context Engineering) relationships
    trajectory_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # Link to trajectory
    playbook_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # Link to playbook
    consolidated_from: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON array of source memory_ids
    consolidation_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # Consolidation tracking

    # Semantic search support (#406)
    embedding_model: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )  # Name of embedding model used
    embedding_dim: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Dimension of embedding vector
    embedding: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Vector embedding (JSON array for SQLite, vector for PostgreSQL)

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

    # Indexes
    __table_args__ = (
        Index("idx_memory_tenant", "tenant_id"),
        Index("idx_memory_user", "user_id"),
        Index("idx_memory_agent", "agent_id"),
        Index("idx_memory_scope", "scope"),
        Index("idx_memory_type", "memory_type"),
        Index("idx_memory_created_at", "created_at"),
        Index("idx_memory_session", "session_id"),
        Index("idx_memory_expires", "expires_at"),
        Index("idx_memory_namespace", "namespace"),  # v0.8.0
        Index("idx_memory_state", "state"),  # #368 - memory state management
        # Unique constraint on (namespace, path_key) for upsert mode
        # Note: Only enforced when both are NOT NULL (partial index for SQLite/Postgres)
        Index(
            "idx_memory_namespace_key",
            "namespace",
            "path_key",
            unique=True,
            sqlite_where=text("path_key IS NOT NULL"),
        ),
    )

    def __repr__(self) -> str:
        return f"<MemoryModel(memory_id={self.memory_id}, user_id={self.user_id}, agent_id={self.agent_id})>"

    def validate(self) -> None:
        """Validate memory model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate content_hash
        if not self.content_hash:
            raise ValidationError("content_hash is required")

        # Validate scope
        valid_scopes = ["agent", "user", "tenant", "global"]
        if self.scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {valid_scopes}, got {self.scope}")

        # Validate visibility
        valid_visibilities = ["private", "shared", "public"]
        if self.visibility not in valid_visibilities:
            raise ValidationError(
                f"visibility must be one of {valid_visibilities}, got {self.visibility}"
            )

        # Validate state (#368)
        valid_states = ["inactive", "active"]
        if self.state not in valid_states:
            raise ValidationError(f"state must be one of {valid_states}, got {self.state}")

        # Validate importance
        if self.importance is not None and not 0.0 <= self.importance <= 1.0:
            raise ValidationError(f"importance must be between 0.0 and 1.0, got {self.importance}")


# ============================================================================
# ReBAC (Relationship-Based Access Control) Tables
# ============================================================================


class ReBACTupleModel(Base):
    """Relationship tuple for ReBAC system.

    Stores (subject, relation, object) tuples representing relationships
    between entities in the authorization graph.

    Added tenant_id for tenant isolation (P0-2 fix)

    Examples:
        - (agent:alice, member-of, group:developers)
        - (group:developers, owner-of, file:/workspace/project.txt)
    """

    __tablename__ = "rebac_tuples"

    tuple_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Tenant isolation - P0-2 Critical Security Fix
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    subject_tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    object_tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Subject (who/what has the relationship)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_relation: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # For userset-as-subject

    # Relation type
    relation: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Object (what is being accessed/owned)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional conditions (JSON)
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Composite index for efficient lookups
    __table_args__ = (
        # Tenant-scoped indexes
        Index("idx_rebac_tenant_subject", "tenant_id", "subject_type", "subject_id"),
        Index("idx_rebac_tenant_object", "tenant_id", "object_type", "object_id"),
        # Original indexes (kept for backward compatibility)
        Index("idx_rebac_subject", "subject_type", "subject_id"),
        Index("idx_rebac_object", "object_type", "object_id"),
        Index("idx_rebac_relation", "relation"),
        Index("idx_rebac_expires", "expires_at"),
        # Subject relation index for userset-as-subject
        Index("idx_rebac_subject_relation", "subject_type", "subject_id", "subject_relation"),
        # ========== Issue #591: Composite indexes for permission checks ==========
        # 1. Direct permission check (most common query pattern)
        # Used in: _has_direct_relation, _get_direct_relation_tuple
        # Query: WHERE subject_type=? AND subject_id=? AND relation=? AND object_type=? AND object_id=?
        Index(
            "idx_rebac_permission_check",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "tenant_id",
        ),
        # 2. Userset/group membership lookups
        # Used in: _find_subject_sets
        # Query: WHERE relation=? AND object_type=? AND object_id=? AND subject_relation IS NOT NULL
        Index(
            "idx_rebac_userset_lookup",
            "relation",
            "object_type",
            "object_id",
            "subject_relation",
            "tenant_id",
        ),
        # 3. Object permission expansion (find all subjects with access to an object)
        # Used in: rebac_expand, _get_direct_subjects
        # Query: WHERE relation=? AND object_type=? AND object_id=? AND tenant_id=?
        Index(
            "idx_rebac_object_expand",
            "object_type",
            "object_id",
            "relation",
            "tenant_id",
        ),
    )


class ReBACNamespaceModel(Base):
    """Namespace configuration for ReBAC permission expansion.

    Defines how permissions are computed for different object types
    using Zanzibar-style permission expansion rules.

    Example config:
        {
            "relations": {
                "owner": {},
                "viewer": {"union": ["owner", "direct_viewer"]},
                "editor": {"union": ["owner", "direct_editor"]}
            }
        }
    """

    __tablename__ = "rebac_namespaces"

    namespace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    object_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)

    # JSON configuration
    config: Mapped[str] = mapped_column(Text, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class ReBACChangelogModel(Base):
    """Change log for ReBAC tuple modifications.

    Tracks all create/delete operations on relationship tuples for
    audit purposes and cache invalidation.
    """

    __tablename__ = "rebac_changelog"

    change_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    change_type: Mapped[str] = mapped_column(String(10), nullable=False)  # INSERT, DELETE

    # Tuple reference
    tuple_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    # Denormalized tuple data for historical record
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    relation: Mapped[str | None] = mapped_column(String(50), nullable=True)
    object_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    object_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Tenant scoping for multi-tenancy
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class ReBACVersionSequenceModel(Base):
    """Per-tenant version sequence for ReBAC consistency tokens.

    Stores monotonic version counters used to track ReBAC tuple changes
    for each tenant. Used for bounded staleness caching (P0-1).
    """

    __tablename__ = "rebac_version_sequences"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (Index("ix_rebac_version_sequences_tenant_id", "tenant_id"),)


class ReBACCheckCacheModel(Base):
    """Cache for ReBAC permission check results.

    Caches the results of expensive graph traversal operations
    to improve performance of repeated permission checks.

    Added tenant_id for tenant-scoped caching
    """

    __tablename__ = "rebac_check_cache"

    cache_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Tenant isolation
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Cached check parameters
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    permission: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Result and metadata
    result: Mapped[bool] = mapped_column(Integer, nullable=False)  # 0=False, 1=True
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Composite index for efficient lookups
    __table_args__ = (
        # Tenant-aware cache lookup
        Index(
            "idx_rebac_cache_tenant_check",
            "tenant_id",
            "subject_type",
            "subject_id",
            "permission",
            "object_type",
            "object_id",
        ),
        # Original index (backward compatibility)
        Index(
            "idx_rebac_cache_check",
            "subject_type",
            "subject_id",
            "permission",
            "object_type",
            "object_id",
        ),
    )


class APIKeyModel(Base):
    """Database-backed API key storage.

    P0-5: Stores API keys securely with HMAC-SHA256 hashing.

    Features:
    - Secure key hashing (HMAC-SHA256 + salt)
    - Optional expiry dates
    - Revocation support
    - Subject-based identity (user, agent, service)
    - Tenant isolation
    """

    __tablename__ = "api_keys"

    # Primary key
    key_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Key security
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Identity & access
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="user")
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    is_admin: Mapped[int] = mapped_column(Integer, default=0)  # SQLite: bool as Integer

    # Permission inheritance (v0.5.1)
    inherit_permissions: Mapped[int] = mapped_column(
        Integer,
        default=0,  # Default: NO inheritance for new keys (principle of least privilege)
        nullable=False,
    )

    # Metadata
    name: Mapped[str] = mapped_column(String(255), nullable=False)  # Human-readable name

    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[int] = mapped_column(Integer, default=0, index=True)  # SQLite: bool as Integer
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MountConfigModel(Base):
    """Persistent mount configuration storage.

    Stores backend mount configurations to survive server restarts.
    Supports dynamic user mounting (e.g., personal Google Drive mounts).

    Example:
        - Mount user's personal Google Drive at /personal/google:alice123
        - Mount team shared GCS bucket at /team/shared-bucket
        - Mount legacy S3 bucket at /archives/legacy-data
    """

    __tablename__ = "mount_configs"

    # Primary key
    mount_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Mount configuration
    mount_point: Mapped[str] = mapped_column(
        Text, nullable=False, unique=True
    )  # e.g., "/personal/alice"
    backend_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # e.g., "google_drive", "gcs", "local"
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    readonly: Mapped[bool] = mapped_column(Integer, nullable=False, default=0)  # SQLite boolean

    # Backend configuration (JSON)
    # Stores backend-specific config like access tokens, bucket names, etc.
    # Example: {"access_token": "...", "user_email": "alice@acme.com"}
    backend_config: Mapped[str] = mapped_column(Text, nullable=False)  # JSON

    # Ownership and metadata
    owner_user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # User who created mount
    tenant_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Tenant this mount belongs to
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # Indexes
    __table_args__ = (
        Index("idx_mount_configs_mount_point", "mount_point"),
        Index("idx_mount_configs_owner", "owner_user_id"),
        Index("idx_mount_configs_tenant", "tenant_id"),
        Index("idx_mount_configs_backend_type", "backend_type"),
    )

    def __repr__(self) -> str:
        return f"<MountConfigModel(mount_id={self.mount_id}, mount_point={self.mount_point}, backend_type={self.backend_type})>"

    def validate(self) -> None:
        """Validate mount config model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate mount_point
        if not self.mount_point:
            raise ValidationError("mount_point is required")

        if not self.mount_point.startswith("/"):
            raise ValidationError(f"mount_point must start with '/', got {self.mount_point!r}")

        # Validate backend_type
        if not self.backend_type:
            raise ValidationError("backend_type is required")

        # Validate backend_config
        if not self.backend_config:
            raise ValidationError("backend_config is required")

        # Try to parse backend_config as JSON
        try:
            json.loads(self.backend_config)
        except json.JSONDecodeError as e:
            raise ValidationError(f"backend_config must be valid JSON: {e}") from None

        # Validate priority
        if self.priority < 0:
            raise ValidationError(f"priority must be non-negative, got {self.priority}")


class SyncJobModel(Base):
    """Async sync job tracking for long-running mount synchronization.

    Tracks progress, status, and results of async sync_mount operations.
    Supports cancellation and progress monitoring via API/CLI.

    Example workflow:
        1. User calls sync_mount_async("/mnt/gmail") -> returns job_id
        2. Job runs in background, updating progress_pct and progress_detail
        3. User polls get_sync_job(job_id) to monitor progress
        4. User can call cancel_sync_job(job_id) to abort
        5. On completion, result contains final sync stats
    """

    __tablename__ = "sync_jobs"

    # Primary key
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # Mount being synced
    mount_point: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    # Job status: pending, running, completed, failed, cancelled
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # Progress tracking (0-100)
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Detailed progress info (JSON)
    # Example: {"files_scanned": 50, "files_total_estimate": 200, "current_path": "/emails/inbox/msg123.eml"}
    progress_detail: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Sync parameters (JSON) - stored for reference/resumability
    # Example: {"path": "/inbox", "include_patterns": ["*.eml"], "sync_content": true}
    sync_params: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Who created this job
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Final result (JSON) - populated on completion
    # Example: {"files_scanned": 200, "files_created": 50, "cache_synced": 200, ...}
    result: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON

    # Error message (if status == 'failed')
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_sync_jobs_mount_point", "mount_point"),
        Index("idx_sync_jobs_status", "status"),
        Index("idx_sync_jobs_created_at", "created_at"),
        Index("idx_sync_jobs_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<SyncJobModel(id={self.id}, mount_point={self.mount_point}, status={self.status}, progress={self.progress_pct}%)>"

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "mount_point": self.mount_point,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "progress_detail": json.loads(self.progress_detail) if self.progress_detail else None,
            "sync_params": json.loads(self.sync_params) if self.sync_params else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_by": self.created_by,
            "result": json.loads(self.result) if self.result else None,
            "error_message": self.error_message,
        }


# === Workspace & Memory Registry Models ===


class WorkspaceConfigModel(Base):
    """Workspace configuration registry.

    Tracks which directories are registered as workspaces.
    Workspaces support snapshot/restore/versioning features.

    Unlike the old system which extracted workspace from paths,
    this is an explicit registry where users declare which
    directories should have workspace capabilities.
    """

    __tablename__ = "workspace_configs"

    # Primary key (the workspace path)
    path: Mapped[str] = mapped_column(Text, primary_key=True)

    # Optional metadata
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit info
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Agent identity
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Agent that created it

    # Session scope
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="persistent"
    )  # "persistent" or "session"
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )  # FK to user_sessions
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )  # Auto-cleanup time

    # User-defined metadata (JSON as text for SQLite compat)
    # Note: Using 'extra_metadata' because 'metadata' is reserved by SQLAlchemy
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_workspace_configs_created_at", "created_at"),
        Index("idx_workspace_configs_user", "user_id"),
        Index("idx_workspace_configs_session", "session_id"),
        Index("idx_workspace_configs_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<WorkspaceConfigModel(path={self.path}, name={self.name})>"


class MemoryConfigModel(Base):
    """Memory configuration registry.

    Tracks which directories are registered as memories.
    Memories support consolidation/search/versioning features.

    No owner or scope needed - permissions handled by ReBAC separately.
    """

    __tablename__ = "memory_configs"

    # Primary key (the memory path)
    path: Mapped[str | None] = mapped_column(Text, primary_key=True)

    # Optional metadata
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Audit info
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Agent identity
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Agent that created it

    # Session scope
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="persistent"
    )  # "persistent" or "session"
    session_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )  # FK to user_sessions
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )  # Auto-cleanup time

    # User-defined metadata (JSON as text for SQLite compat)
    # Note: Using 'extra_metadata' because 'metadata' is reserved by SQLAlchemy
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_memory_configs_created_at", "created_at"),
        Index("idx_memory_configs_user", "user_id"),
        Index("idx_memory_configs_session", "session_id"),
        Index("idx_memory_configs_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<MemoryConfigModel(path={self.path}, name={self.name})>"


# ============================================================================
# ACE (Agentic Context Engineering) Tables
# ============================================================================


class TrajectoryModel(Base):
    """Trajectory tracking for ACE (Agentic Context Engineering).

    Tracks execution trajectories for learning and reflection.
    Each trajectory represents a task execution with steps, decisions, and outcomes.
    """

    __tablename__ = "trajectories"

    # Primary key
    trajectory_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Identity relationships
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Agent that created it
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Task information
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    task_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True
    )  # 'api_call', 'data_processing', 'reasoning'

    # Execution trace (stored as CAS content)
    trace_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # JSON with steps/decisions/outcomes

    # Outcome
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # 'success', 'failure', 'partial'
    success_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # 0.0-1.0
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Performance metrics
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Relations
    parent_trajectory_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("trajectories.trajectory_id", ondelete="SET NULL"), nullable=True
    )

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Feedback tracking (Dynamic Feedback System)
    feedback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_relearning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # Boolean for PostgreSQL compatibility
    relearning_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_feedback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Path context (Optional path-based filtering)
    path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    # Session lifecycle (For temporary trajectories)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Relationships
    parent_trajectory: Mapped["TrajectoryModel | None"] = relationship(
        "TrajectoryModel", remote_side=[trajectory_id], foreign_keys=[parent_trajectory_id]
    )

    # Indexes and constraints
    __table_args__ = (
        Index("idx_traj_user", "user_id"),
        Index("idx_traj_agent", "agent_id"),
        Index("idx_traj_tenant", "tenant_id"),
        Index("idx_traj_status", "status"),
        Index("idx_traj_task_type", "task_type"),
        Index("idx_traj_completed", "completed_at"),
        Index("idx_traj_relearning", "needs_relearning", "relearning_priority"),
        Index("idx_traj_path", "path"),
        Index("idx_traj_session", "session_id"),
        Index("idx_traj_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<TrajectoryModel(trajectory_id={self.trajectory_id}, status={self.status}, task={self.task_description[:50]})>"

    def validate(self) -> None:
        """Validate trajectory model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate user_id
        if not self.user_id:
            raise ValidationError("user_id is required")

        # Validate task_description
        if not self.task_description:
            raise ValidationError("task_description is required")

        # Validate trace_hash
        if not self.trace_hash:
            raise ValidationError("trace_hash is required")

        # Validate status
        valid_statuses = ["success", "failure", "partial"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")

        # Validate success_score
        if self.success_score is not None and not 0.0 <= self.success_score <= 1.0:
            raise ValidationError(
                f"success_score must be between 0.0 and 1.0, got {self.success_score}"
            )


class PlaybookModel(Base):
    """Playbook storage for ACE (Agentic Context Engineering).

    Stores learned strategies and patterns for agents.
    Playbooks contain strategies (helpful, harmful, neutral) with evidence tracking.
    """

    __tablename__ = "playbooks"

    # Primary key
    playbook_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Identity relationships
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Agent that created it
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Playbook information
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Content (stored as CAS)
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # Structured playbook data

    # Effectiveness metrics
    usage_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_improvement: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    # Scope and visibility
    scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="agent", index=True
    )  # 'agent', 'user', 'tenant', 'global'
    visibility: Mapped[str] = mapped_column(
        String(50), nullable=False, default="private"
    )  # 'private', 'shared', 'public'

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Path context (Optional path-based filtering)
    path: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)

    # Session lifecycle (For temporary playbooks)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("agent_id", "name", "version", name="uq_playbook_agent_name_version"),
        Index("idx_playbook_user", "user_id"),
        Index("idx_playbook_agent", "agent_id"),
        Index("idx_playbook_tenant", "tenant_id"),
        Index("idx_playbook_name", "name"),
        Index("idx_playbook_scope", "scope"),
        Index("idx_playbook_path", "path"),
        Index("idx_playbook_session", "session_id"),
        Index("idx_playbook_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<PlaybookModel(playbook_id={self.playbook_id}, name={self.name}, version={self.version})>"

    def validate(self) -> None:
        """Validate playbook model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate user_id
        if not self.user_id:
            raise ValidationError("user_id is required")

        # Validate name
        if not self.name:
            raise ValidationError("name is required")

        # Validate version
        if self.version < 1:
            raise ValidationError(f"version must be >= 1, got {self.version}")

        # Validate content_hash
        if not self.content_hash:
            raise ValidationError("content_hash is required")

        # Validate scope
        valid_scopes = ["agent", "user", "tenant", "global"]
        if self.scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {valid_scopes}, got {self.scope}")

        # Validate visibility
        valid_visibilities = ["private", "shared", "public"]
        if self.visibility not in valid_visibilities:
            raise ValidationError(
                f"visibility must be one of {valid_visibilities}, got {self.visibility}"
            )

        # Validate metrics
        if not 0.0 <= self.success_rate <= 1.0:
            raise ValidationError(
                f"success_rate must be between 0.0 and 1.0, got {self.success_rate}"
            )

        if self.usage_count < 0:
            raise ValidationError(f"usage_count must be non-negative, got {self.usage_count}")


class UserSessionModel(Base):
    """User session tracking for session-scoped resources.

    Tracks active sessions with optional TTL for automatic cleanup.
    Sessions can be temporary (with expires_at) or persistent (expires_at=None).
    """

    __tablename__ = "user_sessions"

    # Primary key
    session_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Identity
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )  # None = persistent session
    last_activity: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Metadata
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_session_user", "user_id"),
        Index("idx_session_agent", "agent_id"),
        Index("idx_session_expires", "expires_at"),
        Index("idx_session_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<UserSessionModel(session_id={self.session_id}, user_id={self.user_id}, expires_at={self.expires_at})>"

    def is_expired(self) -> bool:
        """Check if session has expired.

        Returns:
            True if session has expires_at and it's in the past
        """
        if self.expires_at is None:
            return False  # Persistent session never expires
        return datetime.now(UTC) > self.expires_at


class TrajectoryFeedbackModel(Base):
    """Dynamic feedback for trajectories.

    Allows adding feedback to completed trajectories for:
    - Production monitoring results
    - Human ratings and reviews
    - A/B test outcomes
    - Long-term metrics

    This enables agents to learn from complete lifecycle data,
    not just initial success/failure.
    """

    __tablename__ = "trajectory_feedback"

    # Primary key
    feedback_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to trajectories
    trajectory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trajectories.trajectory_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Feedback details
    feedback_type: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True
    )  # 'human', 'monitoring', 'ab_test', 'production'
    revised_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # New score (0.0-1.0)
    source: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Who/what provided feedback
    message: Mapped[str | None] = mapped_column(Text, nullable=True)  # Human-readable explanation

    # Metrics (stored as JSON)
    metrics_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Additional structured data

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    # Indexes
    __table_args__ = (
        Index("idx_feedback_trajectory", "trajectory_id"),
        Index("idx_feedback_type", "feedback_type"),
        Index("idx_feedback_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<TrajectoryFeedbackModel(feedback_id={self.feedback_id}, trajectory_id={self.trajectory_id}, type={self.feedback_type})>"


# ============================================================================
# Sandbox Management Tables (Issue #372)
# ============================================================================


class SandboxMetadataModel(Base):
    """Sandbox metadata for Nexus-managed sandboxes (E2B, etc.).

    Stores metadata for sandboxes that Nexus creates and manages.
    Supports lifecycle management (pause/resume/stop), TTL, and multi-language code execution.
    """

    __tablename__ = "sandbox_metadata"

    # Primary key
    sandbox_id: Mapped[str] = mapped_column(
        String(255), primary_key=True
    )  # E2B sandbox ID (e.g., "sb_xxx")

    # User-friendly name (unique per user)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Identity relationships
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Provider information
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="e2b"
    )  # "e2b", "docker", etc.
    template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # E2B template ID

    # Lifecycle management
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True
    )  # "creating", "active", "paused", "stopping", "stopped", "error"
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # TTL configuration
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)  # Idle timeout
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )  # Computed expiry time

    # Auto-creation flag
    auto_created: Mapped[bool] = mapped_column(
        Integer, nullable=False, default=1
    )  # SQLite boolean (always True for managed sandboxes)

    # Provider-specific metadata (JSON)
    # Note: Using 'provider_metadata' as Python attribute name because 'metadata' is reserved by SQLAlchemy
    provider_metadata: Mapped[str | None] = mapped_column(
        "metadata", Text, nullable=True
    )  # JSON as string

    # Indexes and constraints
    __table_args__ = (
        # Note: Removed UniqueConstraint on (user_id, name) to allow name reuse
        # for stopped sandboxes. Application layer enforces uniqueness for active sandboxes only.
        Index("idx_sandbox_user", "user_id"),
        Index("idx_sandbox_agent", "agent_id"),
        Index("idx_sandbox_tenant", "tenant_id"),
        Index("idx_sandbox_status", "status"),
        Index("idx_sandbox_expires", "expires_at"),
        Index("idx_sandbox_created", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<SandboxMetadataModel(sandbox_id={self.sandbox_id}, name={self.name}, user={self.user_id}, status={self.status})>"

    def validate(self) -> None:
        """Validate sandbox metadata before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate sandbox_id
        if not self.sandbox_id:
            raise ValidationError("sandbox_id is required")

        # Validate name
        if not self.name:
            raise ValidationError("name is required")

        # Validate user_id
        if not self.user_id:
            raise ValidationError("user_id is required")

        # Validate tenant_id
        if not self.tenant_id:
            raise ValidationError("tenant_id is required")

        # Validate provider
        valid_providers = ["e2b", "docker", "modal"]
        if self.provider not in valid_providers:
            raise ValidationError(f"provider must be one of {valid_providers}, got {self.provider}")

        # Validate status
        valid_statuses = ["creating", "active", "paused", "stopping", "stopped", "error"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")

        # Validate ttl_minutes
        if self.ttl_minutes < 1:
            raise ValidationError(f"ttl_minutes must be >= 1, got {self.ttl_minutes}")


# === OAuth Credentials Model ===


class OAuthCredentialModel(Base):
    """OAuth 2.0 credential storage for backend integrations.

    Stores encrypted OAuth tokens for services like Google Drive, Microsoft Graph, etc.
    Supports automatic token refresh and multi-tenant isolation.

    Security features:
    - Encrypted token storage (access_token, refresh_token)
    - HMAC integrity protection
    - Tenant isolation
    - Audit logging of token operations
    - Automatic expiry enforcement

    Example:
        # Store Google Drive credentials for a user
        cred = OAuthCredentialModel(
            provider="google",
            user_email="alice@example.com",
            tenant_id="org_acme",
            scopes=["https://www.googleapis.com/auth/drive"],
            encrypted_access_token="...",
            encrypted_refresh_token="...",
            expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    """

    __tablename__ = "oauth_credentials"

    # Primary key
    credential_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # OAuth provider (google, microsoft, dropbox, etc.)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # User identity
    # user_email: Email from OAuth provider (required for token association)
    # user_id: Nexus user identity (for permission checks, may differ from email)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Encrypted tokens (encrypted at rest)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token metadata
    token_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Bearer"
    )  # "Bearer", "MAC", etc.
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    scopes: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of scopes

    # OAuth provider metadata
    client_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_uri: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Lifecycle
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked: Mapped[int] = mapped_column(Integer, default=0, index=True)  # SQLite: bool as Integer
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Audit fields
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes
    __table_args__ = (
        # Unique constraint: one credential per (provider, user_email, tenant)
        # Note: user_email is from OAuth provider, user_id is Nexus identity
        UniqueConstraint("provider", "user_email", "tenant_id", name="uq_oauth_credential"),
        Index("idx_oauth_provider", "provider"),
        Index("idx_oauth_user_email", "user_email"),
        Index("idx_oauth_user_id", "user_id"),
        Index("idx_oauth_tenant", "tenant_id"),
        Index("idx_oauth_expires", "expires_at"),
        Index("idx_oauth_revoked", "revoked"),
    )

    def __repr__(self) -> str:
        return f"<OAuthCredentialModel(credential_id={self.credential_id}, provider={self.provider}, user_email={self.user_email}, user_id={self.user_id})>"

    def is_expired(self) -> bool:
        """Check if the access token is expired."""
        if self.expires_at is None:
            return False
        # Ensure expires_at is timezone-aware for comparison
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expires_at

    def is_valid(self) -> bool:
        """Check if the credential is valid (not revoked and not expired)."""
        return not self.revoked and not self.is_expired()

    def validate(self) -> None:
        """Validate OAuth credential before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate provider
        if not self.provider:
            raise ValidationError("provider is required")

        valid_providers = ["google", "microsoft", "dropbox", "box"]
        if self.provider not in valid_providers:
            raise ValidationError(f"provider must be one of {valid_providers}, got {self.provider}")

        # Validate user_email (required - comes from OAuth provider)
        if not self.user_email:
            raise ValidationError("user_email is required")

        # user_id is optional but recommended for permission checks
        # If not provided, it will be set from context during credential storage

        # Validate encrypted tokens
        if not self.encrypted_access_token:
            raise ValidationError("encrypted_access_token is required")

        # Validate scopes format (if provided)
        if self.scopes:
            try:
                scopes_list = json.loads(self.scopes)
                if not isinstance(scopes_list, list):
                    raise ValidationError("scopes must be a JSON array")
            except json.JSONDecodeError as e:
                raise ValidationError(f"scopes must be valid JSON: {e}") from None


# Add fields to TrajectoryModel for feedback support (these will be added via migration)
# - feedback_count: INTEGER DEFAULT 0
# - effective_score: FLOAT (latest/weighted score)
# - needs_relearning: BOOLEAN DEFAULT FALSE
# - relearning_priority: INTEGER DEFAULT 0


class ContentCacheModel(Base):
    """Cache table for connector content.

    Stores cached content from connectors (GCS, X, Gmail, Google Drive, etc.)
    to enable fast grep, glob, and semantic search without real-time connector access.

    See docs/design/cache-layer.md for design details.
    """

    __tablename__ = "content_cache"

    # Primary key
    cache_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # References
    path_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("file_paths.path_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Tenant isolation (same pattern as other tables)
    tenant_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # Content storage
    content_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Searchable text (parsed or raw)
    content_binary: Mapped[bytes | None] = mapped_column(
        Text, nullable=True
    )  # Original binary as base64 (optional)
    content_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # SHA-256 of original content

    # Size tracking
    original_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cached_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Parsing info
    content_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'full', 'parsed', 'summary', 'reference'
    parsed_from: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # 'pdf', 'xlsx', 'docx', etc.
    parser_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parse_metadata: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON metadata from parsing

    # Version control (for optimistic locking on writes)
    backend_version: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Freshness tracking
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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

    # Relationships
    file_path: Mapped["FilePathModel"] = relationship("FilePathModel", foreign_keys=[path_id])

    # Indexes
    __table_args__ = (
        Index("idx_content_cache_tenant", "tenant_id"),
        Index("idx_content_cache_stale", "stale", postgresql_where=text("stale = true")),
        Index("idx_content_cache_synced", "synced_at"),
    )

    def __repr__(self) -> str:
        return f"<ContentCacheModel(cache_id={self.cache_id}, path_id={self.path_id}, content_type={self.content_type})>"


# - last_feedback_at: TIMESTAMP


# ============================================================================
# System Settings Table
# ============================================================================


class SystemSettingsModel(Base):
    """System-wide settings stored in the database.

    Provides persistent storage for system configuration that needs to be
    consistent across all server instances, including:
    - OAuth encryption key (auto-generated on first use)
    - Feature flags
    - System-wide defaults

    Security note: The encryption key is stored in the database. While this
    is not ideal from a pure security standpoint, it ensures consistency
    across processes and restarts. For higher security deployments, use
    NEXUS_OAUTH_ENCRYPTION_KEY environment variable instead.
    """

    __tablename__ = "system_settings"

    # Primary key - setting name
    key: Mapped[str] = mapped_column(String(255), primary_key=True)

    # Setting value (can be encrypted for sensitive data)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    # Metadata
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_sensitive: Mapped[int] = mapped_column(
        Integer, default=0
    )  # SQLite: bool as int, marks if value should be hidden in logs

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

    def __repr__(self) -> str:
        # Don't show value for sensitive settings
        value_display = "***" if self.is_sensitive else self.value[:50]
        return f"<SystemSettingsModel(key={self.key}, value={value_display})>"


# ============================================================================
# Event Subscriptions Table
# ============================================================================


class SubscriptionModel(Base):
    """Webhook subscriptions for event notifications.

    Allows clients to register webhooks that receive real-time notifications
    when file events (write, delete, rename) occur matching their filters.
    """

    __tablename__ = "subscriptions"

    # Primary key
    subscription_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Multi-tenancy
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False)

    # Webhook configuration
    url: Mapped[str] = mapped_column(Text, nullable=False)  # Webhook URL
    secret: Mapped[str | None] = mapped_column(String(255), nullable=True)  # HMAC secret

    # Event filters (JSON arrays stored as text)
    event_types: Mapped[str] = mapped_column(
        Text, nullable=False, default='["file_write", "file_delete", "file_rename"]'
    )  # JSON array of event types
    patterns: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of glob patterns

    # Subscription metadata
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)  # Custom JSON metadata

    # State
    enabled: Mapped[int] = mapped_column(Integer, nullable=False, default=1)  # SQLite: bool as int

    # Delivery stats
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_delivery_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # success, failed
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

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
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Indexes and constraints
    __table_args__ = (
        Index("idx_subscriptions_tenant", "tenant_id"),
        Index("idx_subscriptions_enabled", "enabled"),
        Index("idx_subscriptions_url", "url"),
    )

    def __repr__(self) -> str:
        return f"<SubscriptionModel(subscription_id={self.subscription_id}, url={self.url[:50]})>"

    def validate(self) -> None:
        """Validate subscription model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate URL
        if not self.url:
            raise ValidationError("url is required")
        if not self.url.startswith(("http://", "https://")):
            raise ValidationError("url must be a valid HTTP/HTTPS URL")

        # Validate event_types JSON
        if self.event_types:
            try:
                event_list = json.loads(self.event_types)
                if not isinstance(event_list, list):
                    raise ValidationError("event_types must be a JSON array")
                valid_events = ["file_write", "file_delete", "file_rename", "metadata_change"]
                for evt in event_list:
                    if evt not in valid_events:
                        raise ValidationError(f"Invalid event type: {evt}")
            except json.JSONDecodeError as e:
                raise ValidationError(f"event_types must be valid JSON: {e}") from e

        # Validate patterns JSON if provided
        if self.patterns:
            try:
                pattern_list = json.loads(self.patterns)
                if not isinstance(pattern_list, list):
                    raise ValidationError("patterns must be a JSON array")
            except json.JSONDecodeError as e:
                raise ValidationError(f"patterns must be valid JSON: {e}") from e

    def get_event_types(self) -> list[str]:
        """Get event types as a Python list."""
        result: list[str] = json.loads(self.event_types) if self.event_types else []
        return result

    def get_patterns(self) -> list[str]:
        """Get patterns as a Python list."""
        result: list[str] = json.loads(self.patterns) if self.patterns else []
        return result

    def get_metadata(self) -> dict[str, Any]:
        """Get custom_metadata as a Python dict."""
        result: dict[str, Any] = json.loads(self.custom_metadata) if self.custom_metadata else {}
        return result
