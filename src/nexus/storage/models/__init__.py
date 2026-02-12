"""SQLAlchemy models for Nexus metadata store.

Issue #1246 Phase 4: Core models extracted to individual modules.
Remaining models stay here for backward compatibility.

Extracted modules:
    models._base           — Base, _generate_uuid, _get_uuid_server_default
    models.file_path       — FilePathModel
    models.version_history — VersionHistoryModel
    models.operation_log   — OperationLogModel
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
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

# Re-export shared base utilities (used by remaining models in this file)
from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default

# Re-export extracted models so all existing imports continue to work:
#   from nexus.storage.models import FilePathModel, VersionHistoryModel, OperationLogModel
from nexus.storage.models.file_path import FilePathModel as FilePathModel
from nexus.storage.models.operation_log import OperationLogModel as OperationLogModel
from nexus.storage.models.persistent_namespace_view import (
    PersistentNamespaceViewModel as PersistentNamespaceViewModel,
)
from nexus.storage.models.version_history import VersionHistoryModel as VersionHistoryModel


class DirectoryEntryModel(Base):
    """Sparse directory index for O(1) non-recursive listings (Issue #924).

    Stores parent-child relationships at the directory level rather than file level.
    This enables fast non-recursive directory listings without scanning all descendants.

    Performance:
    - Before: list("/workspace/", recursive=False) with 10k files → ~500ms (LIKE scan)
    - After: list("/workspace/", recursive=False) → ~5ms (index lookup)

    Population Strategy:
    - New files: Indexed on put()/put_batch()
    - Existing files: Lazy population on modification, or optional backfill script
    - Fallback: If no index entries exist for a path, falls back to LIKE query
    """

    __tablename__ = "directory_entries"

    # Composite primary key: (zone_id, parent_path, entry_name)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(
        String(255), primary_key=True, nullable=False, default="default"
    )
    parent_path: Mapped[str] = mapped_column(String(4096), primary_key=True, nullable=False)
    entry_name: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)

    # Entry type: "file" or "directory"
    entry_type: Mapped[str] = mapped_column(String(10), nullable=False)

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

    # Indexes for fast lookups
    __table_args__ = (
        # Primary lookup pattern: list all entries in a directory for a zone
        Index("idx_directory_entries_lookup", "zone_id", "parent_path"),
        # PostgreSQL text_pattern_ops for LIKE prefix queries on parent_path
        Index(
            "idx_directory_entries_parent_prefix",
            "parent_path",
            postgresql_ops={"parent_path": "text_pattern_ops"},
        ),
    )

    def __repr__(self) -> str:
        return f"<DirectoryEntryModel(zone={self.zone_id}, parent={self.parent_path}, name={self.entry_name}, type={self.entry_type})>"

    def validate(self) -> None:
        """Validate directory entry model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate parent_path
        if not self.parent_path:
            raise ValidationError("parent_path is required")

        if not self.parent_path.startswith("/"):
            raise ValidationError(f"parent_path must start with '/', got {self.parent_path!r}")

        if not self.parent_path.endswith("/"):
            raise ValidationError(f"parent_path must end with '/', got {self.parent_path!r}")

        # Validate entry_name
        if not self.entry_name:
            raise ValidationError("entry_name is required")

        if "/" in self.entry_name:
            raise ValidationError(f"entry_name cannot contain '/', got {self.entry_name!r}")

        # Validate entry_type
        if self.entry_type not in ("file", "directory"):
            raise ValidationError(
                f"entry_type must be 'file' or 'directory', got {self.entry_type!r}"
            )


class FileMetadataModel(Base):
    """File metadata storage.

    Stores arbitrary key-value metadata for files.
    """

    __tablename__ = "file_metadata"

    # Primary key
    metadata_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
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
    # Note: content_hash already has unique=True which creates an index
    __table_args__ = (
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
    the old zone_id+agent_id pattern.
    """

    __tablename__ = "workspace_snapshots"

    # Primary key
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Workspace identification (changed from zone_id+agent_id to workspace_path)
    # Note: Index defined in __table_args__ (idx_workspace_snapshots_workspace_path)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Snapshot metadata
    snapshot_number: Mapped[int] = mapped_column(Integer, nullable=False)  # Sequential version
    # Note: Index defined in __table_args__ (idx_workspace_snapshots_manifest)
    manifest_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # SHA-256 hash of manifest (CAS key)

    # Snapshot stats (for quick display)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Change tracking
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON array of tags

    # Timestamps
    # Note: Index defined in __table_args__ (idx_workspace_snapshots_created_at)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
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


class WorkflowModel(Base):
    """Workflow definitions.

    Stores workflow definitions and their configurations.
    """

    __tablename__ = "workflows"

    # Primary key
    workflow_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Multi-tenancy
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False)

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
    # Note: zone_id is covered by uq_zone_workflow_name prefix
    __table_args__ = (
        UniqueConstraint("zone_id", "name", name="uq_zone_workflow_name"),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
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

    # Line numbers in original document (for source navigation)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
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
    )  # 'zone', 'user', 'agent'
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
        valid_types = ["zone", "user", "agent"]
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


class AgentRecordModel(Base):
    """Agent record for lifecycle tracking (Agent OS Phase 1, Issue #1240).

    Stores agent identity, lifecycle state, session generation counter,
    and heartbeat timestamps. Uses optimistic locking via the generation
    column for cross-DB (SQLite + PostgreSQL) concurrency control.
    """

    __tablename__ = "agent_records"

    agent_id: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="UNKNOWN")
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    agent_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as string
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_agent_records_zone_state", "zone_id", "state"),
        Index("idx_agent_records_state_heartbeat", "state", "last_heartbeat"),
        Index("idx_agent_records_owner", "owner_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentRecordModel(agent_id={self.agent_id}, state={self.state}, "
            f"generation={self.generation})>"
        )


class AgentEventModel(Base):
    """Agent lifecycle events audit log (Issue #1307).

    Append-only table recording agent lifecycle events such as sandbox
    creation, connection, and termination. Used by SandboxAuthService
    to satisfy the "sandbox lifecycle events recorded as agent events"
    acceptance criterion.
    """

    __tablename__ = "agent_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    zone_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON as string
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("ix_agent_events_agent_created", "agent_id", "created_at"),
        Index("ix_agent_events_type", "event_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<AgentEventModel(id={self.id}, agent_id={self.agent_id}, "
            f"event_type={self.event_type})>"
        )


class MemoryModel(Base):
    """Memory storage for AI agents.

    Identity-based memory with order-neutral paths and 3-layer permissions.
    Canonical storage by memory_id, with virtual path views for browsing.
    """

    __tablename__ = "memories"

    # Primary key
    memory_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Content (CAS reference)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Identity relationships
    # Note: Indexes defined in __table_args__ (idx_memory_zone, idx_memory_user, idx_memory_agent)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Real user ownership
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Created by agent

    # Scope and visibility
    scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="agent"
    )  # 'agent', 'user', 'zone', 'global', 'session'
    visibility: Mapped[str] = mapped_column(
        String(50), nullable=False, default="private"
    )  # 'private', 'shared', 'public'

    # Session scope for session-scoped memories
    # Note: Indexes defined in __table_args__ (idx_memory_session, idx_memory_expires)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Memory metadata
    memory_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # 'fact', 'preference', 'experience', 'strategy', 'anti_pattern', 'observation', 'trajectory', 'reflection', 'consolidated'
    importance: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # 0.0-1.0 importance score (may be decayed)

    # Importance decay tracking (#1030 - SimpleMem importance decay)
    importance_original: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Original importance before decay (preserved for recalculation)
    last_accessed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Last time memory was retrieved (for decay calculation)
    access_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )  # Number of times memory was accessed

    # State management (#368)
    # Note: Index defined in __table_args__ (idx_memory_state)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )  # 'inactive', 'active' - supports manual approval workflow (default: active for backward compatibility)

    # Namespace organization (v0.8.0 - #350)
    # Note: Index defined in __table_args__ (idx_memory_namespace)
    namespace: Mapped[str | None] = mapped_column(
        String(255), nullable=True
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

    # Version tracking (#1184 - Memory versioning)
    current_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # Current version number, incremented on each update

    # Append-only lineage tracking (#1188 - Non-destructive updates)
    supersedes_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # Memory this one replaces (FK to memory_id)
    superseded_by_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )  # Memory that replaced this one (denormalized for fast lookup)

    # Semantic search support (#406)
    embedding_model: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # Name of embedding model used
    embedding_dim: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Dimension of embedding vector
    embedding: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Vector embedding (JSON array for SQLite, vector for PostgreSQL)

    # Entity extraction support (#1025 - SimpleMem symbolic layer)
    entities_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON: [{"text": "John Smith", "type": "PERSON", "start": 0, "end": 10}, ...]
    entity_types: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Comma-separated: "PERSON,ORG,DATE"
    person_refs: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Comma-separated person names for quick filtering

    # Temporal metadata for date-based queries (#1028)
    temporal_refs_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON: [{"original": "tomorrow", "resolved": "2025-01-11", "type": "date"}, ...]
    earliest_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Earliest date mentioned in content (indexed for queries)
    latest_date: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Latest date mentioned in content (indexed for queries)

    # Relationship extraction support (#1038 - LightRAG/GraphRAG style)
    relationships_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON: [{"subject": "Alice", "predicate": "MANAGES", "object": "team", "confidence": 0.95}, ...]
    relationship_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Count of extracted relationships for filtering

    # Hierarchical memory abstraction (#1029 - SimpleMem recursive consolidation)
    # Level 0 = atomic, 1 = cluster, 2 = abstract, 3+ = meta-abstract
    abstraction_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Points to higher-level abstraction (parent in hierarchy)
    parent_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # JSON array of lower-level memory IDs (children in hierarchy)
    child_memory_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True if consolidated into higher level (still queryable but lower priority)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

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

    # Bi-temporal validity period (#1183)
    # valid_at: When the fact became true in the real world (NULL = use created_at)
    # invalid_at: When the fact became false (NULL = still valid)
    valid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_memory_zone", "zone_id"),
        Index("idx_memory_user", "user_id"),
        Index("idx_memory_agent", "agent_id"),
        Index("idx_memory_scope", "scope"),
        Index("idx_memory_type", "memory_type"),
        Index("idx_memory_created_at", "created_at"),
        Index("idx_memory_session", "session_id"),
        Index("idx_memory_expires", "expires_at"),
        Index("idx_memory_namespace", "namespace"),  # v0.8.0
        Index("idx_memory_state", "state"),  # #368 - memory state management
        Index("idx_memory_entity_types", "entity_types"),  # #1025 - entity type filtering
        Index("idx_memory_earliest_date", "earliest_date"),  # #1028 - temporal query filtering
        Index("idx_memory_latest_date", "latest_date"),  # #1028 - temporal query filtering
        Index(
            "idx_memory_relationship_count", "relationship_count"
        ),  # #1038 - relationship filtering
        Index("idx_memory_abstraction_level", "abstraction_level"),  # #1029 - hierarchy level
        Index("idx_memory_parent", "parent_memory_id"),  # #1029 - parent lookup
        Index("idx_memory_archived", "is_archived"),  # #1029 - archived filtering
        Index("idx_memory_last_accessed", "last_accessed_at"),  # #1030 - decay calculation
        Index("idx_memory_valid_at", "valid_at"),  # #1183 - bi-temporal validity start
        Index("idx_memory_invalid_at", "invalid_at"),  # #1183 - bi-temporal validity end
        Index("idx_memory_current_version", "current_version"),  # #1184 - version tracking
        Index("idx_memory_supersedes", "supersedes_id"),  # #1188 - lineage lookup
        Index("idx_memory_superseded_by", "superseded_by_id"),  # #1188 - reverse lineage
        # Unique constraint on (namespace, path_key) for upsert mode
        # Note: Only enforced when both are NOT NULL (partial index for SQLite/Postgres)
        Index(
            "idx_memory_namespace_key",
            "namespace",
            "path_key",
            unique=True,
            sqlite_where=text("path_key IS NOT NULL"),
        ),
        # ========== Postgres Best Practices: BRIN Index ==========
        # BRIN indexes are 10-100x smaller than B-tree for time-series data.
        # Memory records are typically inserted in time order.
        # Reference: https://www.postgresql.org/docs/current/brin-intro.html
        Index(
            "idx_memory_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
        # Zone-scoped BRIN for time-range queries within a zone
        Index(
            "idx_memory_zone_created_brin",
            "zone_id",
            "created_at",
            postgresql_using="brin",
        ),
        # #1183: BRIN index for bi-temporal validity period queries
        Index(
            "idx_memory_valid_at_brin",
            "valid_at",
            postgresql_using="brin",
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
        valid_scopes = ["agent", "user", "zone", "global"]
        if self.scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {valid_scopes}, got {self.scope}")

        # Validate visibility
        valid_visibilities = ["private", "shared", "public"]
        if self.visibility not in valid_visibilities:
            raise ValidationError(
                f"visibility must be one of {valid_visibilities}, got {self.visibility}"
            )

        # Validate state (#368, #1188)
        valid_states = ["inactive", "active", "deleted"]
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

    Added zone_id for zone isolation (P0-2 fix)

    Examples:
        - (agent:alice, member-of, group:developers)
        - (group:developers, owner-of, file:/workspace/project.txt)
    """

    __tablename__ = "rebac_tuples"

    tuple_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Zone isolation - P0-2 Critical Security Fix
    # Note: Covered by composite indexes in __table_args__ (idx_rebac_zone_subject, idx_rebac_zone_object, etc.)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    subject_zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    object_zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Subject (who/what has the relationship)
    # Note: Covered by composite indexes (idx_rebac_permission_check, idx_rebac_subject_relation)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    subject_relation: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # For userset-as-subject

    # Relation type
    # Note: Covered by idx_rebac_relation in __table_args__
    relation: Mapped[str] = mapped_column(String(50), nullable=False)

    # Object (what is being accessed/owned)
    # Note: Covered by composite indexes (idx_rebac_object_expand, idx_rebac_permission_check)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Optional conditions (JSON)
    conditions: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Composite index for efficient lookups
    __table_args__ = (
        # Zone-scoped indexes
        Index("idx_rebac_zone_subject", "zone_id", "subject_type", "subject_id"),
        Index("idx_rebac_zone_object", "zone_id", "object_type", "object_id"),
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
            "zone_id",
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
            "zone_id",
        ),
        # 3. Object permission expansion (find all subjects with access to an object)
        # Used in: rebac_expand, _get_direct_subjects
        # Query: WHERE relation=? AND object_type=? AND object_id=? AND zone_id=?
        Index(
            "idx_rebac_object_expand",
            "object_type",
            "object_id",
            "relation",
            "zone_id",
        ),
        # ========== Issue #687: Partial indexes for non-expired tuples (SpiceDB optimization) ==========
        # These partial indexes only include tuples where expires_at IS NULL (most common case).
        # Benefits: 30-50% smaller indexes, 10-30% faster lookups, better cache efficiency.
        # Reference: SpiceDB uses similar pattern with WHERE deleted_xid IS NULL
        #
        # 1. Partial permission check index (most common query pattern)
        # Covers: WHERE subject_type=? AND subject_id=? AND relation=? AND object_type=? AND object_id=?
        #         AND (expires_at IS NULL OR expires_at >= ?)
        Index(
            "idx_rebac_alive_permission_check",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "zone_id",
            postgresql_where=text("expires_at IS NULL"),
        ),
        # 2. Partial subject lookup index (for reverse lookups)
        # Covers: WHERE subject_type=? AND subject_id=? AND zone_id=?
        Index(
            "idx_rebac_alive_by_subject",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            postgresql_where=text("expires_at IS NULL"),
        ),
        # 3. Partial zone-scoped object index
        # Covers: WHERE zone_id=? AND object_type=? AND object_id=? AND relation=?
        Index(
            "idx_rebac_alive_zone_object",
            "zone_id",
            "object_type",
            "object_id",
            "relation",
            postgresql_where=text("expires_at IS NULL"),
        ),
        # 4. Partial userset lookup index (for group membership with subject_relation)
        # Covers: WHERE relation=? AND object_type=? AND object_id=? AND subject_relation IS NOT NULL
        Index(
            "idx_rebac_alive_userset",
            "relation",
            "object_type",
            "object_id",
            "subject_relation",
            "zone_id",
            postgresql_where=text("expires_at IS NULL AND subject_relation IS NOT NULL"),
        ),
        # ========== Issue #904: Cross-zone share index ==========
        # Optimizes queries for finding files shared with a user from other zones.
        # Query pattern: WHERE subject_type=? AND subject_id=?
        #                  AND relation IN ('shared-viewer', 'shared-editor', 'shared-owner')
        # This is a partial index covering only cross-zone share relations.
        Index(
            "idx_rebac_cross_zone_shares",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            postgresql_where=text(
                "relation IN ('shared-viewer', 'shared-editor', 'shared-owner') "
                "AND expires_at IS NULL"
            ),
        ),
        # ========== Postgres Best Practices: Covering Index ==========
        # Include commonly needed columns to enable index-only scans (2-5x faster)
        # Reference: https://www.postgresql.org/docs/current/indexes-index-only-scans.html
        Index(
            "idx_rebac_permission_check_covering",
            "subject_type",
            "subject_id",
            "relation",
            "object_type",
            "object_id",
            "zone_id",
            postgresql_include=["tuple_id", "expires_at", "created_at"],
            postgresql_where=text("expires_at IS NULL"),
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


class ReBACGroupClosureModel(Base):
    """Leopard-style transitive group closure for O(1) membership lookups.

    Pre-computes transitive group memberships to eliminate recursive queries.
    Based on Google Zanzibar's Leopard index (Section 2.4.2).

    Examples:
        If user:alice -> group:team-a -> group:engineering -> group:all-employees,
        this table stores:
        - (user, alice, group, team-a, depth=1)
        - (user, alice, group, engineering, depth=2)
        - (user, alice, group, all-employees, depth=3)

    Performance:
        - Read: O(1) - single query to get all transitive groups
        - Write: O(depth) - update closure when membership changes
        - Space: O(members x groups)

    Related: Issue #692
    """

    __tablename__ = "rebac_group_closure"

    # Composite primary key
    member_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    member_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    group_type: Mapped[str] = mapped_column(String(50), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    # Metadata
    depth: Mapped[int] = mapped_column(Integer, nullable=False)  # Distance in hierarchy

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # Indexes defined in migration (add_leopard_group_closure.py)

    def __repr__(self) -> str:
        return (
            f"<ReBACGroupClosureModel("
            f"{self.member_type}:{self.member_id} -> "
            f"{self.group_type}:{self.group_id}, depth={self.depth})>"
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

    # Zone scoping for multi-zone isolation
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, index=True
    )


class ReBACVersionSequenceModel(Base):
    """Per-zone version sequence for ReBAC consistency tokens.

    Stores monotonic version counters used to track ReBAC tuple changes
    for each zone. Used for bounded staleness caching (P0-1).
    """

    __tablename__ = "rebac_version_sequences"

    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # Note: No explicit index needed - zone_id is the primary key
    __table_args__: tuple = ()


class FileSystemVersionSequenceModel(Base):
    """Per-zone version sequence for filesystem consistency tokens (Issue #1187).

    Stores monotonic revision counters used to track filesystem changes
    for each zone. Used for Zookie consistency tokens to enable
    read-after-write consistency guarantees.

    See: nexus.core.zookie for the Zookie class implementation.
    """

    __tablename__ = "filesystem_version_sequences"

    zone_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    current_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    # Note: No explicit index needed - zone_id is the primary key
    __table_args__: tuple = ()


class ReBACCheckCacheModel(Base):
    """Cache for ReBAC permission check results.

    Caches the results of expensive graph traversal operations
    to improve performance of repeated permission checks.

    Added zone_id for zone-scoped caching
    """

    __tablename__ = "rebac_check_cache"

    cache_id: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Zone isolation
    # Note: Covered by composite index idx_rebac_cache_zone_check in __table_args__
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Cached check parameters
    # Note: All covered by composite indexes idx_rebac_cache_zone_check and idx_rebac_cache_check
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)
    object_type: Mapped[str] = mapped_column(String(50), nullable=False)
    object_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Result and metadata
    result: Mapped[bool] = mapped_column(Integer, nullable=False)  # 0=False, 1=True
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Composite index for efficient lookups
    __table_args__ = (
        # Zone-aware cache lookup
        Index(
            "idx_rebac_cache_zone_check",
            "zone_id",
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
    - Zone isolation
    """

    __tablename__ = "api_keys"

    # Primary key
    key_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Key security
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Identity & access
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject_type: Mapped[str | None] = mapped_column(String(50), nullable=True, default="user")
    subject_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)
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


class OAuthAPIKeyModel(Base):
    """Stores encrypted API key values for OAuth users.

    Since API keys are hashed in the api_keys table (for security), we can't retrieve
    the raw key value to return to users on subsequent logins. This table stores the
    encrypted raw API key value so OAuth users can retrieve their key on login.

    SECURITY:
    - Keys are encrypted using Fernet (AES-128 + HMAC-SHA256)
    - Only used for OAuth-generated keys (not user-created keys)
    - Automatically cleaned up when the corresponding API key is deleted
    """

    __tablename__ = "oauth_api_keys"

    # Primary key (references api_keys.key_id)
    key_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("api_keys.key_id", ondelete="CASCADE"),
        primary_key=True,
    )

    # User ID (for easier queries without joining api_keys table)
    # Note: Index defined in __table_args__ (idx_oauth_api_keys_user)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Encrypted API key value (can be decrypted and returned to user)
    encrypted_key_value: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # Fernet-encrypted raw API key (e.g., "sk-...")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes
    __table_args__ = (Index("idx_oauth_api_keys_user", "user_id"),)

    def __repr__(self) -> str:
        return f"<OAuthAPIKeyModel(key_id={self.key_id}, user_id={self.user_id})>"


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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
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
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="default"
    )  # Zone this mount belongs to
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
    # Note: mount_point already has unique=True which creates an index
    __table_args__ = (
        Index("idx_mount_configs_owner", "owner_user_id"),
        Index("idx_mount_configs_zone", "zone_id"),
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
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Mount being synced
    # Note: Index defined in __table_args__ (idx_sync_jobs_mount_point)
    mount_point: Mapped[str] = mapped_column(Text, nullable=False)

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


# === Delta Sync Change Tracking (Issue #1127) ===


class BackendChangeLogModel(Base):
    """Change log for delta sync tracking (Issue #1127).

    Tracks the last synced state of each file per backend, enabling
    incremental sync by comparing against current backend state.

    Change Detection Strategy (rsync-inspired):
    1. Quick check: Compare size + mtime first (fastest, no network)
    2. Backend version: Compare GCS generation or S3 version ID
    3. Content hash: Fallback for backends without native versioning

    Performance:
    - Before: Full scan of 10,000 files → ~100 seconds
    - After: Delta check of 10,000 files, 10 changed → ~0.2 seconds
    """

    __tablename__ = "backend_change_log"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # File identification (composite unique key)
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    backend_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Change detection fields (rsync-inspired quick check)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Backend-specific version tracking
    # GCS: generation number (monotonically increasing integer as string)
    # S3: version ID (if versioning enabled)
    # Other: content hash or timestamp
    backend_version: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Content hash (SHA-256 or BLAKE3) - fallback for change detection
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Sync tracking
    synced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Optional: zone isolation for multi-zone deployments
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Indexes and constraints
    __table_args__ = (
        # Unique constraint: one entry per path per backend per zone
        UniqueConstraint("path", "backend_name", "zone_id", name="uq_backend_change_log"),
        # Lookup patterns
        Index("idx_bcl_path_backend", "path", "backend_name"),
        Index("idx_bcl_synced_at", "backend_name", "synced_at"),
        Index("idx_bcl_zone", "zone_id"),
        # BRIN index for time-series queries (append-only pattern)
        Index(
            "idx_bcl_synced_brin",
            "synced_at",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return f"<BackendChangeLogModel(path={self.path}, backend={self.backend_name}, synced_at={self.synced_at})>"

    def validate(self) -> None:
        """Validate change log model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        if not self.path:
            raise ValidationError("path is required")

        if not self.backend_name:
            raise ValidationError("backend_name is required")

        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")


class SyncBacklogModel(Base):
    """Backlog for bidirectional sync write-back operations (Issue #1129).

    Tracks pending write-back operations from Nexus to source backends.
    Supports coalescing (multiple writes to same path merge into one),
    retry with backoff, and TTL-based expiry.

    Status flow: pending -> in_progress -> completed/failed/expired
    """

    __tablename__ = "sync_backlog"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # File identification
    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    backend_name: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Operation details
    operation_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # write, delete, mkdir, rename
    content_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # CAS hash for writes
    new_path: Mapped[str | None] = mapped_column(String(4096), nullable=True)  # For rename ops

    # Status tracking
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending/in_progress/completed/failed/expired
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=5)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Unique per path+backend+zone for pending entries (enables upsert coalescing)
        # PostgreSQL: partial unique index; SQLite: regular unique index
        UniqueConstraint(
            "path",
            "backend_name",
            "zone_id",
            "status",
            name="uq_sync_backlog_pending",
        ),
        # Pending fetch: ordered by creation time
        Index("idx_sb_status_created", "status", "created_at"),
        # Per-backend processing
        Index("idx_sb_backend_zone_status", "backend_name", "zone_id", "status"),
        # BRIN index for time-range cleanup
        Index(
            "idx_sb_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<SyncBacklogModel(path={self.path}, backend={self.backend_name}, "
            f"op={self.operation_type}, status={self.status})>"
        )


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
    # Note: Indexes defined in __table_args__ (idx_workspace_configs_user, idx_workspace_configs_agent)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Agent that created it

    # Session scope
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="persistent"
    )  # "persistent" or "session"
    # Note: Indexes defined in __table_args__ (idx_workspace_configs_session, idx_workspace_configs_expires)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # FK to user_sessions
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Auto-cleanup time

    # User-defined metadata (JSON as text for SQLite compat)
    # Note: Using 'extra_metadata' because 'metadata' is reserved by SQLAlchemy
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_workspace_configs_created_at", "created_at"),
        Index("idx_workspace_configs_user", "user_id"),
        Index("idx_workspace_configs_agent", "agent_id"),
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
    # Note: Indexes defined in __table_args__ (idx_memory_configs_user, idx_memory_configs_agent)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Agent that created it

    # Session scope
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="persistent"
    )  # "persistent" or "session"
    # Note: Indexes defined in __table_args__ (idx_memory_configs_session, idx_memory_configs_expires)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # FK to user_sessions
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Auto-cleanup time

    # User-defined metadata (JSON as text for SQLite compat)
    # Note: Using 'extra_metadata' because 'metadata' is reserved by SQLAlchemy
    extra_metadata: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_memory_configs_created_at", "created_at"),
        Index("idx_memory_configs_user", "user_id"),
        Index("idx_memory_configs_agent", "agent_id"),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Identity relationships
    # Note: Indexes defined in __table_args__ (idx_traj_user, idx_traj_agent, idx_traj_zone)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Agent that created it
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Task information
    task_description: Mapped[str] = mapped_column(Text, nullable=False)
    # Note: Index defined in __table_args__ (idx_traj_task_type)
    task_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )  # 'api_call', 'data_processing', 'reasoning'

    # Execution trace (stored as CAS content)
    trace_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # JSON with steps/decisions/outcomes

    # Outcome
    # Note: Index defined in __table_args__ (idx_traj_status)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False
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
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    # Note: Index defined in __table_args__ (idx_traj_completed)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Feedback tracking (Dynamic Feedback System)
    feedback_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effective_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    needs_relearning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )  # Boolean for PostgreSQL compatibility
    relearning_priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_feedback_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Path context (Optional path-based filtering)
    # Note: Index defined in __table_args__ (idx_traj_path)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Session lifecycle (For temporary trajectories)
    # Note: Indexes defined in __table_args__ (idx_traj_session, idx_traj_expires)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    parent_trajectory: Mapped["TrajectoryModel | None"] = relationship(
        "TrajectoryModel", remote_side=[trajectory_id], foreign_keys=[parent_trajectory_id]
    )

    # Indexes and constraints
    __table_args__ = (
        Index("idx_traj_user", "user_id"),
        Index("idx_traj_agent", "agent_id"),
        Index("idx_traj_zone", "zone_id"),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Identity relationships
    # Note: Indexes defined in __table_args__ (idx_playbook_user, idx_playbook_agent, idx_playbook_zone)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)  # Owner
    agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Agent that created it
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Playbook information
    # Note: Index defined in __table_args__ (idx_playbook_name)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
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
    # Note: Index defined in __table_args__ (idx_playbook_scope)
    scope: Mapped[str] = mapped_column(
        String(50), nullable=False, default="agent"
    )  # 'agent', 'user', 'zone', 'global'
    visibility: Mapped[str] = mapped_column(
        String(50), nullable=False, default="private"
    )  # 'private', 'shared', 'public'

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
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Path context (Optional path-based filtering)
    # Note: Index defined in __table_args__ (idx_playbook_path)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Session lifecycle (For temporary playbooks)
    # Note: Indexes defined in __table_args__ (idx_playbook_session, idx_playbook_expires)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes and constraints
    __table_args__ = (
        UniqueConstraint("agent_id", "name", "version", name="uq_playbook_agent_name_version"),
        Index("idx_playbook_user", "user_id"),
        Index("idx_playbook_agent", "agent_id"),
        Index("idx_playbook_zone", "zone_id"),
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
        valid_scopes = ["agent", "user", "zone", "global"]
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Identity
    # Note: Indexes defined in __table_args__ (idx_session_user, idx_session_agent, idx_session_zone)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Lifecycle
    # Note: Indexes defined in __table_args__ (idx_session_created, idx_session_expires)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
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
        Index("idx_session_zone", "zone_id"),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Foreign key to trajectories
    # Note: Index defined in __table_args__ (idx_feedback_trajectory)
    trajectory_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("trajectories.trajectory_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Feedback details
    # Note: Index defined in __table_args__ (idx_feedback_type)
    feedback_type: Mapped[str] = mapped_column(
        String(50), nullable=False
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
    # Note: Index defined in __table_args__ (idx_feedback_created)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
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
    # Note: Index defined in __table_args__ (idx_sandbox_name)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Identity relationships
    # Note: Indexes defined in __table_args__ (idx_sandbox_user, idx_sandbox_agent, idx_sandbox_zone)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Provider information
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="e2b"
    )  # "e2b", "docker", etc.
    template_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # E2B template ID

    # Lifecycle management
    # Note: Indexes defined in __table_args__ (idx_sandbox_status, idx_sandbox_created)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # "creating", "active", "paused", "stopping", "stopped", "error"
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # TTL configuration
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=10)  # Idle timeout
    # Note: Index defined in __table_args__ (idx_sandbox_expires)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
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
        Index("idx_sandbox_name", "name"),
        Index("idx_sandbox_user", "user_id"),
        Index("idx_sandbox_agent", "agent_id"),
        Index("idx_sandbox_zone", "zone_id"),
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

        # Validate zone_id
        if not self.zone_id:
            raise ValidationError("zone_id is required")

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
    Supports automatic token refresh and multi-zone isolation.

    Security features:
    - Encrypted token storage (access_token, refresh_token)
    - HMAC integrity protection
    - Zone isolation
    - Audit logging of token operations
    - Automatic expiry enforcement

    Example:
        # Store Google Drive credentials for a user
        cred = OAuthCredentialModel(
            provider="google",
            user_email="alice@example.com",
            zone_id="org_acme",
            scopes=["https://www.googleapis.com/auth/drive"],
            encrypted_access_token="...",
            encrypted_refresh_token="...",
            expires_at=datetime.now(UTC) + timedelta(hours=1)
        )
    """

    __tablename__ = "oauth_credentials"

    # Primary key
    credential_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # OAuth provider (google, microsoft, dropbox, etc.)
    # Note: Index defined in __table_args__ (idx_oauth_provider)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)

    # User identity
    # user_email: Email from OAuth provider (required for token association)
    # user_id: Nexus user identity (for permission checks, may differ from email)
    # Note: Indexes defined in __table_args__ (idx_oauth_user_email, idx_oauth_user_id, idx_oauth_zone)
    user_email: Mapped[str] = mapped_column(String(255), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Encrypted tokens (encrypted at rest)
    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Token metadata
    token_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Bearer"
    )  # "Bearer", "MAC", etc.
    # Note: Index defined in __table_args__ (idx_oauth_expires)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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
    # Note: Index defined in __table_args__ (idx_oauth_revoked)
    revoked: Mapped[int] = mapped_column(Integer, default=0)  # SQLite: bool as Integer
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Audit fields
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes
    __table_args__ = (
        # Unique constraint: one credential per (provider, user_email, zone)
        # Note: user_email is from OAuth provider, user_id is Nexus identity
        UniqueConstraint("provider", "user_email", "zone_id", name="uq_oauth_credential"),
        Index("idx_oauth_provider", "provider"),
        Index("idx_oauth_user_email", "user_email"),
        Index("idx_oauth_user_id", "user_id"),
        Index("idx_oauth_zone", "zone_id"),
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


class UserModel(Base):
    """Core user account model.

    Stores user identity and profile information.
    Supports multiple authentication methods and external user management.

    Key features:
    - Multiple auth methods (password, OAuth, external, API key)
    - Multi-zone support via ReBAC groups
    - Soft delete support (is_active, deleted_at)
    - Email/username uniqueness via partial indexes
    """

    __tablename__ = "users"

    # Primary key
    # STANDARDIZED: Use UUID for all new users (recommended for security and consistency)
    # For backward compatibility: Existing APIKeyModel.user_id values remain as strings
    # Migration: Existing user_ids can be gradually migrated to UUID-based UserModel entries
    user_id: Mapped[str] = mapped_column(
        String(255), primary_key=True
    )  # Unique user identifier (UUID for new users, string for backward compatibility)

    # Identity
    # NOTE: Uniqueness enforced via partial unique indexes (see migration) to support soft delete
    # Do NOT use unique=True here - it would prevent email/username reuse after soft delete
    # Note: Indexes defined in __table_args__ (idx_users_username, idx_users_email)
    username: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # For username/password auth (unique for active users via partial index)
    email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Email address (unique for active users via partial index)

    # Profile
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Authentication
    # Note: Users can have multiple auth methods (password + OAuth accounts)
    # This field indicates the PRIMARY auth method used for account creation
    password_hash: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # Bcrypt hash for username/password auth (512 chars for future-proofing with argon2/scrypt)
    # Note: Index defined in __table_args__ (idx_users_auth_method)
    primary_auth_method: Mapped[str] = mapped_column(
        String(50), nullable=False, default="password"
    )  # 'password', 'oauth', 'external', 'api_key' - indicates how account was created

    # External user management
    # Note: Covered by composite idx_users_external in __table_args__
    external_user_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # ID in external user service
    external_user_service: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # External service identifier (e.g., 'auth0', 'okta', 'custom')
    # NOTE: Endpoint configuration stored in ExternalUserServiceModel, not per-user

    # Self-serve API key (convenience layer)
    # For self-serve users: plaintext API key stored here for retrieval
    # Hashed version also stored in api_keys table for secure authentication
    api_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Plaintext API key (for user convenience - NOT for authentication)
    zone_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, index=True
    )  # Self-serve zone ID (typically user's email)

    # Multi-zone
    # NOTE: Zone membership is managed via ReBAC groups ONLY
    # ReBAC Tuple: (user:user_id, member-of, group:zone-{zone_id})
    # No primary_zone_id field - all zone relationships via ReBAC

    # Admin status
    # Note: Per-zone admin status managed via ReBAC relations:
    # - (user:user_id, admin-of, group:zone-{zone_id}) for zone admin
    # - (user:user_id, member-of, group:zone-{zone_id}) for zone member
    is_global_admin: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # Global admin across all zones (rare, for super-admins only)

    # Status
    # SOFT DELETE: Users are marked inactive instead of hard deleted
    # This preserves audit trail, API keys, and relationships
    # Hard delete only via admin command after retention period (e.g., 90 days)
    # Note: Indexes defined in __table_args__ (idx_users_active, idx_users_deleted)
    is_active: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )  # SQLite: bool as Integer (0 = soft deleted)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )  # Timestamp when user was soft deleted (None = active)
    email_verified: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )  # SQLite: bool as Integer

    # Metadata (renamed to avoid SQLAlchemy reserved name)
    user_metadata: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON as string for additional user data

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
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)

    # Relationships
    oauth_accounts: Mapped[list["UserOAuthAccountModel"]] = relationship(
        "UserOAuthAccountModel", back_populates="user", cascade="all, delete-orphan"
    )
    # Note: Zone membership is managed via ReBAC groups, not a separate table
    # ReBAC Tuple: (user:user_id, member-of, group:zone-{zone_id})
    # This leverages existing ReBAC infrastructure (Google Zanzibar pattern)
    #
    # Note: APIKeyModel.user_id is NOT a foreign key (by design for backward compatibility)
    # We can query API keys by user_id but won't enforce referential integrity
    # api_keys: Relationship would be defined via backref if needed, but not as FK

    # Indexes
    __table_args__ = (
        # Partial unique indexes to support soft delete (email/username can be reused after deletion)
        # SQLite doesn't support partial indexes in table args, so these must be created via raw SQL in migration:
        # CREATE UNIQUE INDEX idx_users_email_active ON users(email) WHERE is_active=1 AND deleted_at IS NULL;
        # CREATE UNIQUE INDEX idx_users_username_active ON users(username) WHERE is_active=1 AND deleted_at IS NULL;
        Index("idx_users_email", "email"),
        Index("idx_users_username", "username"),
        Index("idx_users_auth_method", "primary_auth_method"),
        Index("idx_users_external", "external_user_service", "external_user_id"),
        Index("idx_users_active", "is_active"),
        Index("idx_users_deleted", "deleted_at"),
        # Composite index for common lookup pattern: email + active status
        Index("idx_users_email_active_deleted", "email", "is_active", "deleted_at"),
    )

    def __repr__(self) -> str:
        return f"<UserModel(user_id={self.user_id}, email={self.email}, username={self.username})>"

    def is_deleted(self) -> bool:
        """Check if user is soft deleted."""
        return self.is_active == 0 or self.deleted_at is not None


class UserOAuthAccountModel(Base):
    """OAuth provider accounts linked to users for authentication.

    **Purpose**: Links external OAuth providers (Google, GitHub, etc.) to user accounts
    for server authentication (login). This is separate from OAuthCredentialModel
    which stores tokens for backend integrations (Google Drive, Gmail, etc.).

    **Key Distinction**:
    - UserOAuthAccountModel: User logs in with Google → gets Nexus access
    - OAuthCredentialModel: User connects Google Drive → accesses their files

    Supports multiple OAuth accounts per user (e.g., Google + GitHub for login).
    """

    __tablename__ = "user_oauth_accounts"

    # Primary key
    oauth_account_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Foreign key to user
    # Note: Index defined in __table_args__ (idx_user_oauth_user)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )

    # OAuth provider
    provider: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'google', 'github', 'microsoft', etc.
    provider_user_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # User ID from OAuth provider (e.g., Google sub claim)
    provider_email: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Email from OAuth provider

    # OAuth token storage (encrypted)
    # SECURITY: Only store ID tokens (short-lived, for verification)
    # ID tokens are sufficient for authentication - no need for access/refresh tokens
    # For backend integrations (Google Drive, etc.), use OAuthCredentialModel instead
    encrypted_id_token: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Encrypted ID token from OAuth provider (for authentication verification)
    # NOTE: Access/refresh tokens removed - ID tokens are sufficient for authentication
    # If userinfo calls are needed, use the ID token directly or implement separate flow
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Profile data from OAuth provider
    provider_profile: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON as string (name, picture, etc.)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relationships
    user: Mapped["UserModel"] = relationship("UserModel", back_populates="oauth_accounts")

    # Indexes
    __table_args__ = (
        # CRITICAL: Unique constraint prevents duplicate OAuth accounts (race condition protection)
        UniqueConstraint("provider", "provider_user_id", name="uq_provider_user"),
        Index("idx_user_oauth_user", "user_id"),
        Index("idx_user_oauth_provider", "provider"),
        Index("idx_user_oauth_provider_user", "provider", "provider_user_id"),
    )

    def __repr__(self) -> str:
        return f"<UserOAuthAccountModel(oauth_account_id={self.oauth_account_id}, provider={self.provider}, user_id={self.user_id})>"


class ZoneModel(Base):
    """Zone metadata model.

    Stores organizational/zone information for multi-zone isolation.
    Zone membership is still managed via ReBAC groups (group:zone-{zone_id}),
    but this table provides a place to store zone metadata (name, settings, etc.).

    Key features:
    - Stores zone display name and metadata
    - Soft delete support (is_active, deleted_at)
    - Timestamps for audit trail
    """

    __tablename__ = "zones"

    # Primary key
    zone_id: Mapped[str] = mapped_column(
        String(255), primary_key=True
    )  # Zone identifier (matches zone_id used throughout the system)

    # Metadata
    # Note: Index defined in __table_args__ (idx_zones_name)
    name: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # Display name for the zone/organization

    # Note: unique=True creates an index, no need for additional index=True
    domain: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True
    )  # Unique domain identifier (company URL, email domain, etc.)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)  # Optional description

    # Settings (extensible JSON field)
    settings: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON as string for additional zone settings/config

    # Status
    # Note: Index defined in __table_args__ (idx_zones_active)
    is_active: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False
    )  # SQLite: bool as Integer (0 = soft deleted)

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True
    )  # Timestamp when zone was soft deleted (None = active)

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

    # Indexes
    __table_args__ = (
        Index("idx_zones_name", "name"),
        Index("idx_zones_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<ZoneModel(zone_id={self.zone_id}, name={self.name}, domain={self.domain}, is_active={self.is_active})>"


class ExternalUserServiceModel(Base):
    """Configuration for external user management services.

    Allows Nexus to delegate user authentication/authorization to external services.
    """

    __tablename__ = "external_user_services"

    # Primary key
    service_id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )

    # Service identification
    service_name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )  # 'auth0', 'okta', 'custom', etc.

    # Service endpoints
    # SECURITY: auth_endpoint MUST be validated against whitelist of allowed domains
    # Validate in application layer before storing to prevent SSRF attacks
    auth_endpoint: Mapped[str] = mapped_column(
        Text, nullable=False
    )  # Endpoint to validate tokens (e.g., JWKS URI, userinfo endpoint)
    user_lookup_endpoint: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Optional: endpoint to fetch user details

    # Authentication method
    auth_method: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # 'jwt', 'api_key', 'oauth', 'custom'

    # Configuration (encrypted JSON)
    # SECURITY: Config contains secrets (client_id, client_secret, etc.) - must be encrypted
    encrypted_config: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Encrypted JSON config (client_id, client_secret, audience, etc.)

    # Status
    is_active: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

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
        return f"<ExternalUserServiceModel(service_id={self.service_id}, service_name={self.service_name})>"


# Add fields to TrajectoryModel for feedback support (these will be added via migration)
# - feedback_count: INTEGER DEFAULT 0
# - effective_score: FLOAT (latest/weighted score)
# - needs_relearning: BOOLEAN DEFAULT FALSE
# - relearning_priority: INTEGER DEFAULT 0


class ContentCacheModel(Base):
    """Cache table for connector content metadata.

    Stores metadata for cached content from connectors (GCS, X, Gmail, Google Drive, etc.)
    to enable fast grep, glob, and semantic search without real-time connector access.

    Content Storage Architecture:
        - Binary content is stored on disk via FileContentCache (fast mmap reads)
        - PostgreSQL stores metadata only (path, hash, size, synced_at)
        - The content_binary column is deprecated (kept for backward compatibility)
        - Disk storage enables Zoekt trigram indexing for sub-50ms code search

    See docs/design/cache-layer.md for design details.
    """

    __tablename__ = "content_cache"

    # Primary key
    cache_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # References
    path_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("file_paths.path_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )

    # Zone isolation (same pattern as other tables)
    # Note: Index defined in __table_args__ (idx_content_cache_zone)
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")

    # Content storage
    content_text: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Searchable text (parsed or raw)
    content_binary: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )  # DEPRECATED: Binary now stored on disk via FileContentCache (kept for migration)
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
        Index("idx_content_cache_zone", "zone_id"),
        Index("idx_content_cache_stale", "stale", postgresql_where=text("stale = true")),
        Index("idx_content_cache_synced", "synced_at"),
        Index(
            "idx_content_cache_backend_version",
            "backend_version",
            postgresql_where=text("backend_version IS NOT NULL"),
        ),
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
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Multi-tenancy
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False)

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
        Index("idx_subscriptions_zone", "zone_id"),
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
                valid_events = [
                    "file_write",
                    "file_delete",
                    "file_rename",
                    "metadata_change",
                    "dir_create",
                    "dir_delete",
                ]
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


# ============================================================================
# Tiger Cache Tables (Issue #682)
# ============================================================================


class TigerResourceMapModel(Base):
    """Maps resource UUIDs to int64 IDs for Roaring Bitmap compatibility.

    Roaring Bitmaps require integer IDs, but our resources use UUIDs.
    This table provides a stable mapping.

    Note: zone_id is intentionally excluded from this table.
    Resource paths are globally unique (e.g., /skills/system/docs is the same
    file regardless of who queries it). Zone isolation is enforced at the
    bitmap/permission level, not the resource ID mapping.

    Related: Issue #682, Issue #979 (cross-zone fix)
    """

    __tablename__ = "tiger_resource_map"

    # Auto-increment int64 ID for bitmap storage
    # Integer for SQLite auto-increment compatibility (SQLite only auto-increments INTEGER PRIMARY KEY)
    resource_int_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Resource identification (no zone - paths are globally unique)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    __table_args__ = (
        # Unique constraint on (resource_type, resource_id) - no zone
        UniqueConstraint("resource_type", "resource_id", name="uq_tiger_resource"),
        Index("idx_tiger_resource_lookup", "resource_type", "resource_id"),
    )

    def __repr__(self) -> str:
        return f"<TigerResourceMapModel({self.resource_int_id}: {self.resource_type}:{self.resource_id})>"


class TigerCacheModel(Base):
    """Stores pre-materialized permissions as Roaring Bitmaps.

    Each row represents all resources a subject can access with a given permission.
    The bitmap_data contains a serialized Roaring Bitmap of resource_int_ids.

    Performance:
        - O(1) lookup for "can user X access resource Y?"
        - O(intersection) for filtering lists by permission

    Related: Issue #682
    """

    __tablename__ = "tiger_cache"

    # Primary key (Integer for SQLite auto-increment compatibility)
    cache_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Subject (who has access)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Permission type (read, write, execute, etc.)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)

    # Resource type (file, directory, etc.)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Serialized Roaring Bitmap (binary)
    bitmap_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # Revision for staleness detection
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

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

    __table_args__ = (
        # Unique constraint
        UniqueConstraint(
            "subject_type",
            "subject_id",
            "permission",
            "resource_type",
            "zone_id",
            name="uq_tiger_cache",
        ),
        # Index for fast cache lookup
        Index(
            "idx_tiger_cache_lookup",
            "zone_id",
            "subject_type",
            "subject_id",
            "permission",
            "resource_type",
        ),
        # Index for revision-based invalidation
        Index("idx_tiger_cache_revision", "revision"),
    )

    def __repr__(self) -> str:
        return (
            f"<TigerCacheModel({self.subject_type}:{self.subject_id} "
            f"{self.permission} {self.resource_type}, rev={self.revision})>"
        )


class TigerCacheQueueModel(Base):
    """Queue for async background updates of Tiger Cache.

    When permissions change, entries are added to this queue.
    A background worker processes the queue to update affected caches.

    Related: Issue #682
    """

    __tablename__ = "tiger_cache_queue"

    # Primary key
    # Integer for SQLite auto-increment compatibility
    queue_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Subject to update
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Permission to recompute
    permission: Mapped[str] = mapped_column(String(50), nullable=False)

    # Resource type
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Zone
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Priority (lower = higher priority)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    # Status: pending, processing, completed, failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Error info if failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("idx_tiger_queue_pending", "status", "priority", "created_at"),)

    def __repr__(self) -> str:
        return (
            f"<TigerCacheQueueModel(queue_id={self.queue_id}, "
            f"{self.subject_type}:{self.subject_id}, status={self.status})>"
        )


class TigerDirectoryGrantsModel(Base):
    """Tracks directory-level permission grants for Leopard-style expansion.

    When permission is granted on a directory, this table records it so:
    1. Pre-materialization: Expand grant to all descendants
    2. New file integration: When file created, inherit from ancestor directories
    3. Move handling: When file moves, update based on old/new ancestors

    Related: Issue #1089 (Leopard-style directory grant pre-materialization)
    """

    __tablename__ = "tiger_directory_grants"

    # Primary key (BigInteger for PostgreSQL compatibility)
    grant_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # Subject (who has access)
    subject_type: Mapped[str] = mapped_column(String(50), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Permission type (read, write, execute)
    permission: Mapped[str] = mapped_column(String(50), nullable=False)

    # Directory path that was granted (e.g., /workspace/project/)
    directory_path: Mapped[str] = mapped_column(Text, nullable=False)

    # Zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Revision at time of grant (for consistency)
    grant_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    # Whether to include files created after the grant
    include_future_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Expansion status: pending, in_progress, completed, failed
    expansion_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # Progress tracking
    expanded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Error info if expansion failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Unique constraint: one grant per (subject, permission, directory, zone)
        UniqueConstraint(
            "zone_id",
            "directory_path",
            "permission",
            "subject_type",
            "subject_id",
            name="uq_tiger_directory_grants",
        ),
        # Index for finding grants by path prefix (for new file integration)
        Index("idx_tiger_dir_grants_path_prefix", "zone_id", "directory_path"),
        # Index for finding grants by subject (for cache invalidation)
        Index("idx_tiger_dir_grants_subject", "zone_id", "subject_type", "subject_id"),
        # Index for pending expansions (for background worker)
        Index("idx_tiger_dir_grants_pending", "expansion_status", "created_at"),
        # Index for permission lookups
        Index("idx_tiger_dir_grants_lookup", "zone_id", "directory_path", "permission"),
    )

    def __repr__(self) -> str:
        return (
            f"<TigerDirectoryGrantsModel(grant_id={self.grant_id}, "
            f"{self.subject_type}:{self.subject_id}, "
            f"dir={self.directory_path}, status={self.expansion_status})>"
        )


# ==============================================================================
# Nexus Pay Models (Issue #1199)
# ==============================================================================


class AgentWalletMeta(Base):
    """Wallet metadata for Nexus Pay. Balances in TigerBeetle, settings here."""

    __tablename__ = "agent_wallet_meta"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_account_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    x402_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    x402_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    daily_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    monthly_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    per_tx_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    daily_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    monthly_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    daily_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    monthly_reset_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("idx_wallet_meta_zone", "zone_id"),
        Index("idx_wallet_meta_tb_id", "tigerbeetle_account_id"),
        Index("idx_wallet_meta_daily_reset", "daily_reset_at"),
        Index("idx_wallet_meta_monthly_reset", "monthly_reset_at"),
    )


class PaymentTransactionMeta(Base):
    """Transaction metadata for Nexus Pay. Amounts in TigerBeetle, context here."""

    __tablename__ = "payment_transaction_meta"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_transfer_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    from_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="credits")
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    x402_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    x402_network: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_tx_meta_from_time", "from_agent_id", "created_at"),
        Index("idx_tx_meta_to_time", "to_agent_id", "created_at"),
        Index("idx_tx_meta_zone_time", "zone_id", "created_at"),
        Index("idx_tx_meta_task", "task_id"),
        Index("idx_tx_meta_x402_hash", "x402_tx_hash"),
    )


class CreditReservationMeta(Base):
    """Credit reservation metadata for two-phase transfers."""

    __tablename__ = "credit_reservation_meta"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_transfer_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    queue_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_reservation_agent_status", "agent_id", "status"),
        Index("idx_reservation_expires", "expires_at"),
        Index("idx_reservation_task", "task_id"),
        Index("idx_reservation_zone", "zone_id", "status"),
    )


class UsageEvent(Base):
    """Usage events for API metering and analytics."""

    __tablename__ = "usage_events"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resource: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_usage_zone_type_time", "zone_id", "event_type", "created_at"),
        Index("idx_usage_agent_time", "agent_id", "created_at"),
        Index("idx_usage_resource", "resource"),
    )

    def get_metadata(self) -> dict[str, Any]:
        """Parse metadata JSON."""
        result: dict[str, Any] = json.loads(self.metadata_json) if self.metadata_json else {}
        return result

    def set_metadata(self, data: dict[str, Any]) -> None:
        """Serialize metadata to JSON."""
        self.metadata_json = json.dumps(data) if data else None


# ==============================================================================
# Share Link Models (Issue #227)
# ==============================================================================


class ShareLinkModel(Base):
    """Capability URL-based share links for anonymous/external file access.

    Implements W3C TAG Capability URL best practices:
    - Unguessable tokens (UUID v4 = 122 bits entropy)
    - Time-limited access via expires_at
    - Optional password protection (Argon2id hashed)
    - Download limits
    - Revocable

    Security: The share link token IS the credential (capability URL pattern).
    No ReBAC tuple is created for anonymous access - validation is stateless
    against this table on each request.
    """

    __tablename__ = "share_links"

    # Primary key - also the token in the share URL
    # UUID v4 provides 122 bits of entropy (exceeds W3C TAG recommendation of 120 bits)
    link_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Resource being shared
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)  # 'file', 'directory'
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)  # path_id or virtual_path

    # Permission level granted by link
    # Maps to ReBAC relations: viewer (read), editor (read+write), owner (full)
    permission_level: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")

    # Multi-zone: link belongs to this zone
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default", index=True)

    # Creator tracking
    created_by: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Optional password protection (Argon2id hash)
    # NULL = no password required
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Access limits
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_access_count: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = unlimited
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Revocation
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Optional extra data (JSON)
    extra_data: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Fast lookup by resource
        Index("idx_share_links_resource", "resource_type", "resource_id"),
        # Partial index for active (non-revoked, non-expired) links
        Index(
            "idx_share_links_active",
            "link_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # Lookup by creator
        Index("idx_share_links_created_by", "zone_id", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<ShareLinkModel(link_id={self.link_id}, resource={self.resource_type}:{self.resource_id}, permission={self.permission_level})>"

    def is_valid(self) -> bool:
        """Check if the share link is currently valid for access."""
        now = datetime.now(UTC)

        # Check revocation
        if self.revoked_at is not None:
            return False

        # Check expiration
        if self.expires_at is not None and self.expires_at < now:
            return False

        # Check access count limit
        return not (
            self.max_access_count is not None and self.access_count >= self.max_access_count
        )


class ShareLinkAccessLogModel(Base):
    """Access log for share link usage tracking.

    Logs every access attempt (successful or not) for:
    - Security auditing
    - Usage analytics
    - Abuse detection
    """

    __tablename__ = "share_link_access_log"

    # Primary key
    log_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Reference to share link
    link_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("share_links.link_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Access details
    accessed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max length
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Result
    success: Mapped[int] = mapped_column(Integer, nullable=False)  # SQLite: bool as int
    failure_reason: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # 'expired', 'revoked', 'limit_exceeded', 'wrong_password'

    # Optional: authenticated user who accessed (if logged in)
    accessed_by_user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    accessed_by_zone_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        # Time-based queries for analytics
        Index("idx_share_link_access_log_time", "link_id", "accessed_at"),
        # Security: find accesses by IP
        Index("idx_share_link_access_log_ip", "ip_address"),
    )

    def __repr__(self) -> str:
        status = "success" if self.success else f"failed:{self.failure_reason}"
        return f"<ShareLinkAccessLogModel(log_id={self.log_id}, link_id={self.link_id}, status={status})>"


class MigrationHistoryModel(Base):
    """Tracks migration history for upgrade/rollback support.

    Records all migration operations (upgrades, rollbacks, imports) for:
    - Audit trail of version changes
    - Rollback point identification
    - Migration status tracking

    Issue #165: Migration Tools & Upgrade Paths
    """

    __tablename__ = "migration_history"

    # Primary key
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Version information
    from_version: Mapped[str] = mapped_column(String(20), nullable=False)
    to_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Migration type: 'upgrade', 'rollback', 'import'
    migration_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Status: 'pending', 'running', 'completed', 'failed', 'rolled_back'
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # Backup information
    backup_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Error tracking
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Additional metadata as JSON
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Query migrations by status
        Index("idx_migration_history_status", "status"),
        # Query migrations by time
        Index("idx_migration_history_started_at", "started_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<MigrationHistoryModel(id={self.id}, "
            f"{self.from_version}->{self.to_version}, "
            f"type={self.migration_type}, status={self.status})>"
        )


# ============================================================================
# Graph Storage Layer for Knowledge Graph (#1039)
# ============================================================================


class EntityModel(Base):
    """Entity registry for knowledge graph.

    Stores canonical entities with embeddings for semantic matching/deduplication.
    Enables cross-document entity linking without requiring a separate graph database.

    Entity Resolution:
    - Uses embedding similarity (cosine distance) for deduplication
    - Default merge threshold: 0.85 similarity
    - Aliases track alternative names for merged entities

    Issue #1039: Graph storage layer for entities and relationships.
    """

    __tablename__ = "entities"

    # Primary key
    entity_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # P0 SECURITY: Defense-in-depth zone isolation
    # Issue #773: Made non-nullable for strict multi-zone isolation
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    # Entity identification
    canonical_name: Mapped[str] = mapped_column(
        String(512), nullable=False
    )  # Normalized/canonical name
    entity_type: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )  # PERSON, ORG, LOCATION, CONCEPT, DATE, etc.

    # Embedding for semantic entity matching/deduplication
    # Stored as JSON array for SQLite, vector type for PostgreSQL (like MemoryModel)
    embedding: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Vector embedding for similarity search
    embedding_model: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )  # Name of embedding model used
    embedding_dim: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )  # Dimension of embedding vector

    # Entity resolution tracking
    aliases: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # JSON array of alternative names for this entity
    merge_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )  # How many mentions merged into this entity

    # Metadata
    metadata_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Additional entity attributes as JSON

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

    # Relationships (ORM)
    source_relationships: Mapped[list["RelationshipModel"]] = relationship(
        "RelationshipModel",
        foreign_keys="RelationshipModel.source_entity_id",
        back_populates="source_entity",
        cascade="all, delete-orphan",
    )
    target_relationships: Mapped[list["RelationshipModel"]] = relationship(
        "RelationshipModel",
        foreign_keys="RelationshipModel.target_entity_id",
        back_populates="target_entity",
        cascade="all, delete-orphan",
    )
    mentions: Mapped[list["EntityMentionModel"]] = relationship(
        "EntityMentionModel",
        back_populates="entity",
        cascade="all, delete-orphan",
    )

    # Indexes and constraints
    __table_args__ = (
        # Unique constraint on (zone_id, canonical_name) for entity deduplication
        UniqueConstraint("zone_id", "canonical_name", name="uq_entity_zone_name"),
        # Zone-scoped queries
        Index("idx_entities_zone", "zone_id"),
        # Entity type filtering
        Index("idx_entities_type", "entity_type"),
        # Combined zone + type for filtered queries
        Index("idx_entities_zone_type", "zone_id", "entity_type"),
        # Name lookup (for exact matching before embedding similarity)
        Index("idx_entities_canonical_name", "canonical_name"),
    )

    def __repr__(self) -> str:
        return f"<EntityModel(entity_id={self.entity_id}, name={self.canonical_name}, type={self.entity_type})>"

    def validate(self) -> None:
        """Validate entity model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate canonical_name
        if not self.canonical_name:
            raise ValidationError("canonical_name is required")

        if len(self.canonical_name) > 512:
            raise ValidationError(
                f"canonical_name must be 512 characters or less, got {len(self.canonical_name)}"
            )

        # Validate entity_type if provided
        valid_types = [
            "PERSON",
            "ORG",
            "LOCATION",
            "DATE",
            "TIME",
            "NUMBER",
            "CONCEPT",
            "EVENT",
            "PRODUCT",
            "TECHNOLOGY",
            "EMAIL",
            "URL",
            "OTHER",
        ]
        if self.entity_type is not None and self.entity_type not in valid_types:
            raise ValidationError(
                f"entity_type must be one of {valid_types}, got {self.entity_type}"
            )

        # Validate merge_count
        if self.merge_count < 1:
            raise ValidationError(f"merge_count must be at least 1, got {self.merge_count}")


class RelationshipModel(Base):
    """Relationships between entities (adjacency list).

    Stores directed edges in the knowledge graph. Uses an adjacency list model
    for efficient storage and PostgreSQL recursive CTEs for N-hop traversal.

    Relationship Types:
    - WORKS_WITH, MANAGES, REPORTS_TO (organizational)
    - CREATES, MODIFIES, OWNS (ownership)
    - DEPENDS_ON, BLOCKS, RELATES_TO (dependencies)
    - MENTIONS, REFERENCES (informational)
    - LOCATED_IN, PART_OF, HAS, USES (structural)

    Issue #1039: Graph storage layer for entities and relationships.
    """

    __tablename__ = "relationships"

    # Primary key
    relationship_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # P0 SECURITY: Defense-in-depth zone isolation
    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    # Source and target entities (foreign keys)
    source_entity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )
    target_entity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Relationship metadata
    relationship_type: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # MANAGES, WORKS_WITH, DEPENDS_ON, etc.
    weight: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )  # Relationship strength (for weighted traversal)
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )  # Extraction confidence from LLM (0.0-1.0)

    # Additional metadata
    metadata_json: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # Additional relationship attributes as JSON

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Relationships (ORM)
    source_entity: Mapped["EntityModel"] = relationship(
        "EntityModel",
        foreign_keys=[source_entity_id],
        back_populates="source_relationships",
    )
    target_entity: Mapped["EntityModel"] = relationship(
        "EntityModel",
        foreign_keys=[target_entity_id],
        back_populates="target_relationships",
    )

    # Indexes and constraints
    __table_args__ = (
        # Unique constraint: one relationship type per source-target pair per zone
        UniqueConstraint(
            "zone_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
            name="uq_relationship_tuple",
        ),
        # Graph traversal indexes (critical for N-hop queries)
        Index("idx_relationships_source", "source_entity_id"),
        Index("idx_relationships_target", "target_entity_id"),
        Index("idx_relationships_type", "relationship_type"),
        # Composite index for outgoing edge traversal
        Index("idx_relationships_source_type", "source_entity_id", "relationship_type"),
        # Composite index for incoming edge traversal
        Index("idx_relationships_target_type", "target_entity_id", "relationship_type"),
        # Zone-scoped queries
        Index("idx_relationships_zone", "zone_id"),
        # Confidence filtering (for filtering low-quality extractions)
        Index("idx_relationships_confidence", "confidence"),
    )

    def __repr__(self) -> str:
        return f"<RelationshipModel(id={self.relationship_id}, {self.source_entity_id} -{self.relationship_type}-> {self.target_entity_id})>"

    def validate(self) -> None:
        """Validate relationship model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate source_entity_id
        if not self.source_entity_id:
            raise ValidationError("source_entity_id is required")

        # Validate target_entity_id
        if not self.target_entity_id:
            raise ValidationError("target_entity_id is required")

        # Validate relationship_type
        if not self.relationship_type:
            raise ValidationError("relationship_type is required")

        valid_types = [
            "WORKS_WITH",
            "MANAGES",
            "REPORTS_TO",
            "CREATES",
            "MODIFIES",
            "OWNS",
            "DEPENDS_ON",
            "BLOCKS",
            "RELATES_TO",
            "MENTIONS",
            "REFERENCES",
            "LOCATED_IN",
            "PART_OF",
            "HAS",
            "USES",
            "OTHER",
        ]
        if self.relationship_type not in valid_types:
            raise ValidationError(
                f"relationship_type must be one of {valid_types}, got {self.relationship_type}"
            )

        # Validate weight
        if self.weight < 0.0:
            raise ValidationError(f"weight must be non-negative, got {self.weight}")

        # Validate confidence
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")

        # Validate no self-loops
        if self.source_entity_id == self.target_entity_id:
            raise ValidationError(
                "Self-loops are not allowed (source_entity_id == target_entity_id)"
            )


class EntityMentionModel(Base):
    """Entity mentions linking entities to source chunks/memories (provenance).

    Tracks where each entity was mentioned in the original documents,
    enabling source attribution and confidence aggregation.

    Issue #1039: Graph storage layer for entities and relationships.
    """

    __tablename__ = "entity_mentions"

    # Primary key
    mention_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Foreign key to entity
    entity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )

    # Source references (at least one should be set)
    chunk_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("document_chunks.chunk_id", ondelete="CASCADE"),
        nullable=True,
    )  # Link to document chunk
    memory_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("memories.memory_id", ondelete="CASCADE"),
        nullable=True,
    )  # Link to memory

    # Mention details
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0
    )  # Extraction confidence
    mention_text: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # Original text that matched (before normalization)

    # Position in source (for highlighting)
    char_offset_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_offset_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Relationships (ORM)
    entity: Mapped["EntityModel"] = relationship(
        "EntityModel",
        back_populates="mentions",
    )

    # Indexes
    __table_args__ = (
        # Entity lookup (find all mentions of an entity)
        Index("idx_entity_mentions_entity", "entity_id"),
        # Chunk lookup (find all entities mentioned in a chunk)
        Index("idx_entity_mentions_chunk", "chunk_id"),
        # Memory lookup (find all entities mentioned in a memory)
        Index("idx_entity_mentions_memory", "memory_id"),
        # Confidence filtering
        Index("idx_entity_mentions_confidence", "confidence"),
    )

    def __repr__(self) -> str:
        source = f"chunk={self.chunk_id}" if self.chunk_id else f"memory={self.memory_id}"
        return (
            f"<EntityMentionModel(mention_id={self.mention_id}, entity={self.entity_id}, {source})>"
        )

    def validate(self) -> None:
        """Validate entity mention model before database operations.

        Raises:
            ValidationError: If validation fails with clear message.
        """
        from nexus.core.exceptions import ValidationError

        # Validate entity_id
        if not self.entity_id:
            raise ValidationError("entity_id is required")

        # Validate at least one source reference
        if self.chunk_id is None and self.memory_id is None:
            raise ValidationError("At least one of chunk_id or memory_id must be set")

        # Validate confidence
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")

        # Validate offset consistency
        if (self.char_offset_start is None) != (self.char_offset_end is None):
            raise ValidationError(
                "char_offset_start and char_offset_end must both be set or both be None"
            )

        if self.char_offset_start is not None and self.char_offset_end is not None:
            if self.char_offset_start < 0:
                raise ValidationError(
                    f"char_offset_start must be non-negative, got {self.char_offset_start}"
                )
            if self.char_offset_end < self.char_offset_start:
                raise ValidationError(
                    f"char_offset_end must be >= char_offset_start, got {self.char_offset_end} < {self.char_offset_start}"
                )
