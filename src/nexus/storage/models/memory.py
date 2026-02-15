"""Memory and knowledge graph models.

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from nexus.core.exceptions import ValidationError
from nexus.storage.models._base import Base, ResourceConfigMixin, uuid_pk

if TYPE_CHECKING:
    pass


class MemoryModel(Base):
    """Memory storage for AI agents.

    Identity-based memory with order-neutral paths and 3-layer permissions.
    Canonical storage by memory_id, with virtual path views for browsing.
    """

    __tablename__ = "memories"

    memory_id: Mapped[str] = uuid_pk()

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    scope: Mapped[str] = mapped_column(String(50), nullable=False, default="agent")
    visibility: Mapped[str] = mapped_column(String(50), nullable=False, default="private")

    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    memory_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    importance: Mapped[float | None] = mapped_column(Float, nullable=True)

    importance_original: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    state: Mapped[str] = mapped_column(String(20), nullable=False, default="active")

    namespace: Mapped[str | None] = mapped_column(String(255), nullable=True)
    path_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    trajectory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    playbook_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    consolidated_from: Mapped[str | None] = mapped_column(Text, nullable=True)
    consolidation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    supersedes_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)

    entities_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    entity_types: Mapped[str | None] = mapped_column(String(255), nullable=True)
    person_refs: Mapped[str | None] = mapped_column(Text, nullable=True)

    temporal_refs_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    earliest_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latest_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    relationships_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationship_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # #1191: Temporal stability classification
    temporal_stability: Mapped[str | None] = mapped_column(String(20), nullable=True)
    stability_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_ttl_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    abstraction_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_memory_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    child_memory_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    valid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    invalid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_memory_zone", "zone_id"),
        Index("idx_memory_user", "user_id"),
        Index("idx_memory_agent", "agent_id"),
        Index("idx_memory_scope", "scope"),
        Index("idx_memory_type", "memory_type"),
        Index("idx_memory_created_at", "created_at"),
        Index("idx_memory_session", "session_id"),
        Index("idx_memory_expires", "expires_at"),
        Index("idx_memory_namespace", "namespace"),
        Index("idx_memory_state", "state"),
        Index("idx_memory_entity_types", "entity_types"),
        Index("idx_memory_earliest_date", "earliest_date"),
        Index("idx_memory_latest_date", "latest_date"),
        Index("idx_memory_relationship_count", "relationship_count"),
        Index("idx_memory_abstraction_level", "abstraction_level"),
        Index("idx_memory_parent", "parent_memory_id"),
        Index("idx_memory_archived", "is_archived"),
        Index("idx_memory_last_accessed", "last_accessed_at"),
        Index("idx_memory_valid_at", "valid_at"),
        Index("idx_memory_invalid_at", "invalid_at"),
        Index("idx_memory_current_version", "current_version"),
        Index("idx_memory_supersedes", "supersedes_id"),
        Index("idx_memory_superseded_by", "superseded_by_id"),
        Index(
            "idx_memory_namespace_key",
            "namespace",
            "path_key",
            unique=True,
            sqlite_where=text("path_key IS NOT NULL"),
        ),
        Index("idx_memory_created_brin", "created_at", postgresql_using="brin"),
        Index("idx_memory_zone_created_brin", "zone_id", "created_at", postgresql_using="brin"),
        Index("idx_memory_valid_at_brin", "valid_at", postgresql_using="brin"),
        Index("idx_memory_temporal_stability", "temporal_stability"),
    )

    def __repr__(self) -> str:
        return f"<MemoryModel(memory_id={self.memory_id}, user_id={self.user_id}, agent_id={self.agent_id})>"

    def validate(self) -> None:
        """Validate memory model before database operations."""
        if not self.content_hash:
            raise ValidationError("content_hash is required")
        valid_scopes = ["agent", "user", "zone", "global"]
        if self.scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {valid_scopes}, got {self.scope}")
        valid_visibilities = ["private", "shared", "public"]
        if self.visibility not in valid_visibilities:
            raise ValidationError(
                f"visibility must be one of {valid_visibilities}, got {self.visibility}"
            )
        valid_states = ["inactive", "active", "deleted"]
        if self.state not in valid_states:
            raise ValidationError(f"state must be one of {valid_states}, got {self.state}")
        if self.importance is not None and not 0.0 <= self.importance <= 1.0:
            raise ValidationError(f"importance must be between 0.0 and 1.0, got {self.importance}")
        # #1191: Validate temporal stability
        valid_stabilities = ["static", "semi_dynamic", "dynamic"]
        if self.temporal_stability is not None and self.temporal_stability not in valid_stabilities:
            raise ValidationError(
                f"temporal_stability must be one of {valid_stabilities}, got {self.temporal_stability}"
            )


class MemoryConfigModel(ResourceConfigMixin, Base):
    """Memory configuration registry.

    Tracks which directories are registered as memories.
    """

    __tablename__ = "memory_configs"

    path: Mapped[str | None] = mapped_column(Text, primary_key=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_memory_configs_created_at", "created_at"),
        Index("idx_memory_configs_user", "user_id"),
        Index("idx_memory_configs_agent", "agent_id"),
        Index("idx_memory_configs_session", "session_id"),
        Index("idx_memory_configs_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"<MemoryConfigModel(path={self.path}, name={self.name})>"


class EntityRegistryModel(Base):
    """Entity registry for identity-based memory system.

    Lightweight registry for ID disambiguation and relationship tracking.
    """

    __tablename__ = "entity_registry"

    entity_type: Mapped[str] = mapped_column(String(50), primary_key=True, nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)

    parent_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    entity_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_entity_registry_id_lookup", "entity_id"),
        Index("idx_entity_registry_parent", "parent_type", "parent_id"),
    )

    def __repr__(self) -> str:
        return f"<EntityRegistryModel(entity_type={self.entity_type}, entity_id={self.entity_id})>"

    def validate(self) -> None:
        """Validate entity registry model before database operations."""
        valid_types = ["zone", "user", "agent"]
        if self.entity_type not in valid_types:
            raise ValidationError(
                f"entity_type must be one of {valid_types}, got {self.entity_type}"
            )
        if not self.entity_id:
            raise ValidationError("entity_id is required")
        if (self.parent_type is None) != (self.parent_id is None):
            raise ValidationError("parent_type and parent_id must both be set or both be None")
        if self.parent_type is not None and self.parent_type not in valid_types:
            raise ValidationError(
                f"parent_type must be one of {valid_types}, got {self.parent_type}"
            )


class EntityModel(Base):
    """Entity registry for knowledge graph.

    Stores canonical entities with embeddings for semantic matching/deduplication.
    """

    __tablename__ = "entities"

    entity_id: Mapped[str] = uuid_pk()

    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

    canonical_name: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)

    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    merge_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    source_relationships: Mapped[list[RelationshipModel]] = relationship(
        "RelationshipModel",
        foreign_keys="RelationshipModel.source_entity_id",
        back_populates="source_entity",
        cascade="all, delete-orphan",
    )
    target_relationships: Mapped[list[RelationshipModel]] = relationship(
        "RelationshipModel",
        foreign_keys="RelationshipModel.target_entity_id",
        back_populates="target_entity",
        cascade="all, delete-orphan",
    )
    mentions: Mapped[list[EntityMentionModel]] = relationship(
        "EntityMentionModel",
        back_populates="entity",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint("zone_id", "canonical_name", name="uq_entity_zone_name"),
        Index("idx_entities_zone", "zone_id"),
        Index("idx_entities_type", "entity_type"),
        Index("idx_entities_zone_type", "zone_id", "entity_type"),
        Index("idx_entities_canonical_name", "canonical_name"),
    )

    def __repr__(self) -> str:
        return f"<EntityModel(entity_id={self.entity_id}, name={self.canonical_name}, type={self.entity_type})>"

    def validate(self) -> None:
        """Validate entity model before database operations."""
        if not self.canonical_name:
            raise ValidationError("canonical_name is required")
        if len(self.canonical_name) > 512:
            raise ValidationError(
                f"canonical_name must be 512 characters or less, got {len(self.canonical_name)}"
            )
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
        if self.merge_count < 1:
            raise ValidationError(f"merge_count must be at least 1, got {self.merge_count}")


class RelationshipModel(Base):
    """Relationships between entities (adjacency list).

    Stores directed edges in the knowledge graph.
    """

    __tablename__ = "relationships"

    relationship_id: Mapped[str] = uuid_pk()

    zone_id: Mapped[str] = mapped_column(String(64), nullable=False, default="default")

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

    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)

    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    source_entity: Mapped[EntityModel] = relationship(
        "EntityModel",
        foreign_keys=[source_entity_id],
        back_populates="source_relationships",
    )
    target_entity: Mapped[EntityModel] = relationship(
        "EntityModel",
        foreign_keys=[target_entity_id],
        back_populates="target_relationships",
    )

    __table_args__ = (
        UniqueConstraint(
            "zone_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
            name="uq_relationship_tuple",
        ),
        Index("idx_relationships_source", "source_entity_id"),
        Index("idx_relationships_target", "target_entity_id"),
        Index("idx_relationships_type", "relationship_type"),
        Index("idx_relationships_source_type", "source_entity_id", "relationship_type"),
        Index("idx_relationships_target_type", "target_entity_id", "relationship_type"),
        Index("idx_relationships_zone", "zone_id"),
        Index("idx_relationships_confidence", "confidence"),
    )

    def __repr__(self) -> str:
        return f"<RelationshipModel(id={self.relationship_id}, {self.source_entity_id} -{self.relationship_type}-> {self.target_entity_id})>"

    def validate(self) -> None:
        """Validate relationship model before database operations."""
        if not self.source_entity_id:
            raise ValidationError("source_entity_id is required")
        if not self.target_entity_id:
            raise ValidationError("target_entity_id is required")
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
        if self.weight < 0.0:
            raise ValidationError(f"weight must be non-negative, got {self.weight}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")
        if self.source_entity_id == self.target_entity_id:
            raise ValidationError(
                "Self-loops are not allowed (source_entity_id == target_entity_id)"
            )


class EntityMentionModel(Base):
    """Entity mentions linking entities to source chunks/memories (provenance)."""

    __tablename__ = "entity_mentions"

    mention_id: Mapped[str] = uuid_pk()

    entity_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("entities.entity_id", ondelete="CASCADE"),
        nullable=False,
    )

    chunk_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("document_chunks.chunk_id", ondelete="CASCADE"),
        nullable=True,
    )
    memory_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("memories.memory_id", ondelete="CASCADE"),
        nullable=True,
    )

    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    mention_text: Mapped[str | None] = mapped_column(String(512), nullable=True)

    char_offset_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_offset_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    entity: Mapped[EntityModel] = relationship(
        "EntityModel",
        back_populates="mentions",
    )

    __table_args__ = (
        Index("idx_entity_mentions_entity", "entity_id"),
        Index("idx_entity_mentions_chunk", "chunk_id"),
        Index("idx_entity_mentions_memory", "memory_id"),
        Index("idx_entity_mentions_confidence", "confidence"),
    )

    def __repr__(self) -> str:
        source = f"chunk={self.chunk_id}" if self.chunk_id else f"memory={self.memory_id}"
        return (
            f"<EntityMentionModel(mention_id={self.mention_id}, entity={self.entity_id}, {source})>"
        )

    def validate(self) -> None:
        """Validate entity mention model before database operations."""
        if not self.entity_id:
            raise ValidationError("entity_id is required")
        if self.chunk_id is None and self.memory_id is None:
            raise ValidationError("At least one of chunk_id or memory_id must be set")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValidationError(f"confidence must be between 0.0 and 1.0, got {self.confidence}")
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
