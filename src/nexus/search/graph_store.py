"""Graph storage layer for knowledge graph operations.

Provides PostgreSQL-native graph storage with:
- Entity deduplication via embedding similarity
- N-hop neighbor traversal via recursive CTEs
- Subgraph extraction for context building

Design principles:
- Uses adjacency list model (entities + relationships tables)
- No external graph database required (PostgreSQL only)
- Entity resolution via pgvector HNSW similarity search
- Multi-zone isolation at every layer

Issue #1039: Graph storage layer for entities and relationships
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.exc import IntegrityError

from nexus.storage.models import (
    EntityMentionModel,
    EntityModel,
    RelationshipModel,
)


def _utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime for asyncpg compatibility."""
    return datetime.now(UTC).replace(tzinfo=None)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from nexus.search.embeddings import EmbeddingProvider


# =============================================================================
# Data Classes
# =============================================================================


@dataclass
class Entity:
    """Represents a canonical entity in the knowledge graph."""

    entity_id: str
    zone_id: str
    canonical_name: str
    entity_type: str | None = None
    embedding: list[float] | None = None
    aliases: list[str] = field(default_factory=list)
    merge_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_model(cls, model: EntityModel) -> Entity:
        """Create Entity from SQLAlchemy model."""
        aliases = []
        if model.aliases:
            with contextlib.suppress(json.JSONDecodeError):
                aliases = json.loads(model.aliases)

        embedding = None
        if model.embedding:
            with contextlib.suppress(json.JSONDecodeError):
                embedding = json.loads(model.embedding)

        metadata = {}
        if model.metadata_json:
            with contextlib.suppress(json.JSONDecodeError):
                metadata = json.loads(model.metadata_json)

        return cls(
            entity_id=model.entity_id,
            zone_id=model.zone_id,
            canonical_name=model.canonical_name,
            entity_type=model.entity_type,
            embedding=embedding,
            aliases=aliases,
            merge_count=model.merge_count,
            metadata=metadata,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "entity_id": self.entity_id,
            "canonical_name": self.canonical_name,
            "entity_type": self.entity_type,
            "aliases": self.aliases,
            "merge_count": self.merge_count,
            "metadata": self.metadata,
        }


@dataclass
class Relationship:
    """Represents a directed edge in the knowledge graph."""

    relationship_id: str
    zone_id: str
    source_entity_id: str
    target_entity_id: str
    relationship_type: str
    weight: float = 1.0
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    # Optionally populated entity details
    source_entity: Entity | None = None
    target_entity: Entity | None = None

    @classmethod
    def from_model(
        cls,
        model: RelationshipModel,
        source: EntityModel | None = None,
        target: EntityModel | None = None,
    ) -> Relationship:
        """Create Relationship from SQLAlchemy model."""
        metadata = {}
        if model.metadata_json:
            with contextlib.suppress(json.JSONDecodeError):
                metadata = json.loads(model.metadata_json)

        return cls(
            relationship_id=model.relationship_id,
            zone_id=model.zone_id,
            source_entity_id=model.source_entity_id,
            target_entity_id=model.target_entity_id,
            relationship_type=model.relationship_type,
            weight=model.weight,
            confidence=model.confidence,
            metadata=metadata,
            created_at=model.created_at,
            source_entity=Entity.from_model(source) if source else None,
            target_entity=Entity.from_model(target) if target else None,
        )


@dataclass
class EntityMention:
    """Represents a mention of an entity in a source document."""

    mention_id: str
    entity_id: str
    chunk_id: str | None = None
    memory_id: str | None = None
    confidence: float = 1.0
    mention_text: str | None = None
    char_offset_start: int | None = None
    char_offset_end: int | None = None
    created_at: datetime | None = None

    @classmethod
    def from_model(cls, model: EntityMentionModel) -> EntityMention:
        """Create EntityMention from SQLAlchemy model."""
        return cls(
            mention_id=model.mention_id,
            entity_id=model.entity_id,
            chunk_id=model.chunk_id,
            memory_id=model.memory_id,
            confidence=model.confidence,
            mention_text=model.mention_text,
            char_offset_start=model.char_offset_start,
            char_offset_end=model.char_offset_end,
            created_at=model.created_at,
        )


@dataclass
class Graph:
    """Represents a subgraph with entities and relationships."""

    entities: list[Entity] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "entities": [
                {
                    "entity_id": e.entity_id,
                    "canonical_name": e.canonical_name,
                    "entity_type": e.entity_type,
                    "aliases": e.aliases,
                    "merge_count": e.merge_count,
                }
                for e in self.entities
            ],
            "relationships": [
                {
                    "relationship_id": r.relationship_id,
                    "source_entity_id": r.source_entity_id,
                    "target_entity_id": r.target_entity_id,
                    "relationship_type": r.relationship_type,
                    "weight": r.weight,
                    "confidence": r.confidence,
                }
                for r in self.relationships
            ],
        }


@dataclass
class NeighborResult:
    """Result of N-hop neighbor traversal."""

    entity: Entity
    depth: int
    path: list[str]  # Entity IDs in the path from source


# =============================================================================
# GraphStore Class
# =============================================================================


class GraphStore:
    """PostgreSQL-native graph storage with entity resolution.

    Provides CRUD operations for entities and relationships, plus graph
    traversal queries using recursive CTEs.

    Entity Resolution:
    - Uses embedding similarity (cosine distance) for deduplication
    - Default merge threshold: 0.85 similarity
    - Automatically merges similar entities on insert

    Graph Traversal:
    - N-hop neighbor traversal via recursive CTEs
    - Configurable depth limiting
    - Relationship type filtering
    - Bidirectional traversal support
    """

    # Default similarity threshold for entity merging
    MERGE_THRESHOLD = 0.85

    # Default confidence threshold for relationships
    CONFIDENCE_THRESHOLD = 0.75

    def __init__(
        self,
        session: AsyncSession,
        zone_id: str = "default",
        embedding_provider: EmbeddingProvider | None = None,
        merge_threshold: float = 0.85,
        confidence_threshold: float = 0.75,
    ):
        """Initialize GraphStore.

        Args:
            session: SQLAlchemy async session
            zone_id: Zone ID for multi-zone isolation
            embedding_provider: Provider for generating entity embeddings
            merge_threshold: Similarity threshold for entity merging (0.0-1.0)
            confidence_threshold: Minimum confidence for relationships
        """
        self.session = session
        self.zone_id = zone_id
        self.embedding_provider = embedding_provider
        self.merge_threshold = merge_threshold
        self.confidence_threshold = confidence_threshold

    def _is_postgres(self) -> bool:
        """Check if the database is PostgreSQL (vs SQLite).

        Uses the session's engine URL to determine the database type.
        """
        bind = self.session.get_bind()
        if bind is None:
            # Fallback to environment variable
            db_url = os.environ.get("NEXUS_DATABASE_URL", "")
            return db_url.startswith(("postgres", "postgresql"))
        # Handle both Engine and Connection types
        url = getattr(bind, "url", None)
        if url is None:
            return False
        db_url = str(url)
        return db_url.startswith(("postgres", "postgresql"))

    # =========================================================================
    # Entity CRUD Operations
    # =========================================================================

    async def add_entity(
        self,
        name: str,
        entity_type: str | None = None,
        embedding: list[float] | None = None,
        aliases: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        resolve: bool = True,
    ) -> tuple[str, bool]:
        """Add an entity to the graph.

        If resolve=True, attempts to find an existing entity with similar
        embedding and merges if similarity >= merge_threshold.

        Args:
            name: Canonical name for the entity
            entity_type: Entity type (PERSON, ORG, etc.)
            embedding: Vector embedding for similarity search
            aliases: Alternative names for this entity
            metadata: Additional entity attributes
            resolve: If True, attempt entity resolution/deduplication

        Returns:
            Tuple of (entity_id, was_created) - was_created is False if merged
        """
        # Normalize name
        canonical_name = name.strip()

        # Check for exact match first
        existing = await self._find_by_name(canonical_name, entity_type)
        if existing:
            # Update aliases if needed
            if aliases:
                await self._merge_aliases(existing.entity_id, aliases)
            return existing.entity_id, False

        # Try embedding-based resolution if enabled and embedding provided
        if resolve and embedding and self.embedding_provider:
            similar = await self.find_similar_entities(
                embedding=embedding,
                entity_type=entity_type,
                limit=1,
                threshold=self.merge_threshold,
            )
            if similar:
                entity, similarity = similar[0]
                logger.debug(
                    f"Merging '{canonical_name}' into '{entity.canonical_name}' "
                    f"(similarity: {similarity:.3f})"
                )
                # Merge into existing entity
                await self._merge_into_entity(
                    entity.entity_id,
                    new_alias=canonical_name,
                    additional_aliases=aliases,
                )
                return entity.entity_id, False

        # Create new entity
        entity_id = str(uuid.uuid4())
        embedding_model_name = None
        if self.embedding_provider and embedding:
            # Try common attribute names for model name
            embedding_model_name = getattr(self.embedding_provider, "model_name", None) or getattr(
                self.embedding_provider, "model", None
            )

        model = EntityModel(
            entity_id=entity_id,
            zone_id=self.zone_id,
            canonical_name=canonical_name,
            entity_type=entity_type,
            embedding=json.dumps(embedding) if embedding else None,
            embedding_model=embedding_model_name,
            embedding_dim=len(embedding) if embedding else None,
            aliases=json.dumps(aliases) if aliases else None,
            merge_count=1,
            metadata_json=json.dumps(metadata) if metadata else None,
        )

        self.session.add(model)
        try:
            await self.session.flush()
            return entity_id, True
        except IntegrityError:
            # Race condition - entity was created by another process
            await self.session.rollback()
            existing = await self._find_by_name(canonical_name, entity_type)
            if existing:
                return existing.entity_id, False
            raise

    async def get_entity(self, entity_id: str) -> Entity | None:
        """Get an entity by ID."""
        stmt = select(EntityModel).where(
            and_(
                EntityModel.entity_id == entity_id,
                EntityModel.zone_id == self.zone_id,
            )
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return Entity.from_model(model) if model else None

    async def get_entities_batch(self, entity_ids: list[str]) -> list[Entity]:
        """Get multiple entities by ID in a single query.

        More efficient than calling get_entity() in a loop.

        Args:
            entity_ids: List of entity IDs to fetch

        Returns:
            List of Entity objects (may be fewer than requested if some don't exist)
        """
        if not entity_ids:
            return []

        stmt = select(EntityModel).where(
            and_(
                EntityModel.zone_id == self.zone_id,
                EntityModel.entity_id.in_(entity_ids),
            )
        )
        result = await self.session.execute(stmt)
        return [Entity.from_model(m) for m in result.scalars().all()]

    async def find_entity(
        self,
        name: str,
        entity_type: str | None = None,
        fuzzy: bool = False,
    ) -> Entity | None:
        """Find an entity by name.

        Args:
            name: Entity name to search for
            entity_type: Optional type filter
            fuzzy: If True, search aliases as well
        """
        # Exact match
        entity = await self._find_by_name(name, entity_type)
        if entity:
            return entity

        # Fuzzy match in aliases
        if fuzzy:
            stmt = select(EntityModel).where(
                and_(
                    EntityModel.zone_id == self.zone_id,
                    EntityModel.aliases.contains(f'"{name}"'),
                )
            )
            if entity_type:
                stmt = stmt.where(EntityModel.entity_type == entity_type)

            result = await self.session.execute(stmt)
            model = result.scalar_one_or_none()
            return Entity.from_model(model) if model else None

        return None

    async def find_similar_entities(
        self,
        embedding: list[float],
        entity_type: str | None = None,
        limit: int = 10,
        threshold: float = 0.85,
    ) -> list[tuple[Entity, float]]:
        """Find entities by embedding similarity.

        Uses pgvector cosine distance for PostgreSQL, or brute-force
        for SQLite.

        Args:
            embedding: Query embedding vector
            entity_type: Optional type filter
            limit: Maximum number of results
            threshold: Minimum similarity score (0.0-1.0)

        Returns:
            List of (entity, similarity_score) tuples, sorted by similarity
        """
        if self._is_postgres():
            return await self._find_similar_pgvector(embedding, entity_type, limit, threshold)
        else:
            return await self._find_similar_brute_force(embedding, entity_type, limit, threshold)

    async def update_entity(
        self,
        entity_id: str,
        canonical_name: str | None = None,
        entity_type: str | None = None,
        embedding: list[float] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Update an existing entity.

        Args:
            entity_id: Entity ID to update
            canonical_name: New canonical name (optional)
            entity_type: New entity type (optional)
            embedding: New embedding (optional)
            metadata: New metadata (optional, replaces existing)

        Returns:
            True if entity was updated, False if not found
        """
        updates: dict[str, Any] = {"updated_at": _utcnow_naive()}

        if canonical_name is not None:
            updates["canonical_name"] = canonical_name
        if entity_type is not None:
            updates["entity_type"] = entity_type
        if embedding is not None:
            updates["embedding"] = json.dumps(embedding)
            updates["embedding_dim"] = len(embedding)
        if metadata is not None:
            updates["metadata_json"] = json.dumps(metadata)

        stmt = (
            update(EntityModel)
            .where(
                and_(
                    EntityModel.entity_id == entity_id,
                    EntityModel.zone_id == self.zone_id,
                )
            )
            .values(**updates)
        )

        result = await self.session.execute(stmt)
        return getattr(result, "rowcount", 0) > 0

    async def delete_entity(self, entity_id: str) -> bool:
        """Delete an entity and all its relationships.

        Args:
            entity_id: Entity ID to delete

        Returns:
            True if entity was deleted, False if not found
        """
        stmt = delete(EntityModel).where(
            and_(
                EntityModel.entity_id == entity_id,
                EntityModel.zone_id == self.zone_id,
            )
        )
        result = await self.session.execute(stmt)
        return getattr(result, "rowcount", 0) > 0

    async def list_entities(
        self,
        entity_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Entity]:
        """List entities with optional type filtering."""
        stmt = select(EntityModel).where(EntityModel.zone_id == self.zone_id)

        if entity_type:
            stmt = stmt.where(EntityModel.entity_type == entity_type)

        stmt = stmt.order_by(EntityModel.canonical_name).limit(limit).offset(offset)

        result = await self.session.execute(stmt)
        return [Entity.from_model(m) for m in result.scalars().all()]

    # =========================================================================
    # Relationship CRUD Operations
    # =========================================================================

    async def add_relationship(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
        weight: float = 1.0,
        confidence: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, bool]:
        """Add a relationship between entities.

        Args:
            source_entity_id: Source entity ID
            target_entity_id: Target entity ID
            relationship_type: Type of relationship (MANAGES, WORKS_WITH, etc.)
            weight: Relationship strength (default: 1.0)
            confidence: Extraction confidence (default: 1.0)
            metadata: Additional relationship attributes

        Returns:
            Tuple of (relationship_id, was_created)
        """
        # Check for existing relationship
        existing = await self._find_relationship(
            source_entity_id, target_entity_id, relationship_type
        )
        if existing:
            # Update weight/confidence if higher
            if weight > existing.weight or confidence > existing.confidence:
                await self._update_relationship_weight(
                    existing.relationship_id,
                    max(weight, existing.weight),
                    max(confidence, existing.confidence),
                )
            return existing.relationship_id, False

        # Create new relationship
        relationship_id = str(uuid.uuid4())
        model = RelationshipModel(
            relationship_id=relationship_id,
            zone_id=self.zone_id,
            source_entity_id=source_entity_id,
            target_entity_id=target_entity_id,
            relationship_type=relationship_type,
            weight=weight,
            confidence=confidence,
            metadata_json=json.dumps(metadata) if metadata else None,
        )

        self.session.add(model)
        try:
            await self.session.flush()
            return relationship_id, True
        except IntegrityError:
            await self.session.rollback()
            existing = await self._find_relationship(
                source_entity_id, target_entity_id, relationship_type
            )
            if existing:
                return existing.relationship_id, False
            raise

    async def get_relationship(self, relationship_id: str) -> Relationship | None:
        """Get a relationship by ID."""
        stmt = select(RelationshipModel).where(
            and_(
                RelationshipModel.relationship_id == relationship_id,
                RelationshipModel.zone_id == self.zone_id,
            )
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return Relationship.from_model(model) if model else None

    async def get_relationships(
        self,
        entity_id: str,
        direction: str = "both",
        rel_types: list[str] | None = None,
        min_confidence: float | None = None,
    ) -> list[Relationship]:
        """Get relationships for an entity.

        Args:
            entity_id: Entity ID
            direction: "outgoing", "incoming", or "both"
            rel_types: Optional filter by relationship types
            min_confidence: Optional minimum confidence threshold

        Returns:
            List of relationships
        """
        conditions = [RelationshipModel.zone_id == self.zone_id]

        if direction == "outgoing":
            conditions.append(RelationshipModel.source_entity_id == entity_id)
        elif direction == "incoming":
            conditions.append(RelationshipModel.target_entity_id == entity_id)
        else:  # both
            conditions.append(
                or_(
                    RelationshipModel.source_entity_id == entity_id,
                    RelationshipModel.target_entity_id == entity_id,
                )
            )

        if rel_types:
            conditions.append(RelationshipModel.relationship_type.in_(rel_types))

        if min_confidence is not None:
            conditions.append(RelationshipModel.confidence >= min_confidence)

        stmt = select(RelationshipModel).where(and_(*conditions))
        result = await self.session.execute(stmt)
        return [Relationship.from_model(m) for m in result.scalars().all()]

    async def delete_relationship(self, relationship_id: str) -> bool:
        """Delete a relationship by ID."""
        stmt = delete(RelationshipModel).where(
            and_(
                RelationshipModel.relationship_id == relationship_id,
                RelationshipModel.zone_id == self.zone_id,
            )
        )
        result = await self.session.execute(stmt)
        return getattr(result, "rowcount", 0) > 0

    # =========================================================================
    # Entity Mention Operations
    # =========================================================================

    async def add_mention(
        self,
        entity_id: str,
        chunk_id: str | None = None,
        memory_id: str | None = None,
        confidence: float = 1.0,
        mention_text: str | None = None,
        char_offset_start: int | None = None,
        char_offset_end: int | None = None,
    ) -> str:
        """Add an entity mention (provenance tracking).

        Args:
            entity_id: Entity that was mentioned
            chunk_id: Source document chunk ID (optional)
            memory_id: Source memory ID (optional)
            confidence: Extraction confidence
            mention_text: Original mention text
            char_offset_start: Start position in source
            char_offset_end: End position in source

        Returns:
            Mention ID
        """
        mention_id = str(uuid.uuid4())
        model = EntityMentionModel(
            mention_id=mention_id,
            entity_id=entity_id,
            chunk_id=chunk_id,
            memory_id=memory_id,
            confidence=confidence,
            mention_text=mention_text,
            char_offset_start=char_offset_start,
            char_offset_end=char_offset_end,
        )
        self.session.add(model)
        await self.session.flush()
        return mention_id

    async def get_entity_mentions(
        self,
        entity_id: str,
        limit: int = 100,
    ) -> list[EntityMention]:
        """Get all mentions of an entity."""
        stmt = (
            select(EntityMentionModel)
            .where(EntityMentionModel.entity_id == entity_id)
            .order_by(EntityMentionModel.created_at.desc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [EntityMention.from_model(m) for m in result.scalars().all()]

    async def get_entities_in_chunk(self, chunk_id: str) -> list[Entity]:
        """Get all entities mentioned in a document chunk."""
        stmt = (
            select(EntityModel)
            .join(EntityMentionModel, EntityMentionModel.entity_id == EntityModel.entity_id)
            .where(EntityMentionModel.chunk_id == chunk_id)
        )
        result = await self.session.execute(stmt)
        return [Entity.from_model(m) for m in result.scalars().all()]

    async def get_entities_in_memory(self, memory_id: str) -> list[Entity]:
        """Get all entities mentioned in a memory."""
        stmt = (
            select(EntityModel)
            .join(EntityMentionModel, EntityMentionModel.entity_id == EntityModel.entity_id)
            .where(EntityMentionModel.memory_id == memory_id)
        )
        result = await self.session.execute(stmt)
        return [Entity.from_model(m) for m in result.scalars().all()]

    # =========================================================================
    # Graph Traversal Operations
    # =========================================================================

    async def get_neighbors(
        self,
        entity_id: str,
        hops: int = 1,
        direction: str = "both",
        rel_types: list[str] | None = None,
        min_confidence: float | None = None,
    ) -> list[NeighborResult]:
        """Get N-hop neighbors of an entity.

        Uses recursive CTE for efficient graph traversal.

        Args:
            entity_id: Starting entity ID
            hops: Maximum traversal depth (1-5 recommended)
            direction: "outgoing", "incoming", or "both"
            rel_types: Optional filter by relationship types
            min_confidence: Optional minimum confidence threshold

        Returns:
            List of NeighborResult with entity, depth, and path
        """
        # Cap hops to prevent runaway queries
        hops = min(hops, 10)

        if self._is_postgres():
            return await self._get_neighbors_postgres(
                entity_id, hops, direction, rel_types, min_confidence
            )
        else:
            return await self._get_neighbors_sqlite(
                entity_id, hops, direction, rel_types, min_confidence
            )

    async def get_subgraph(
        self,
        entity_ids: list[str],
        max_hops: int = 2,
        rel_types: list[str] | None = None,
        min_confidence: float | None = None,
    ) -> Graph:
        """Extract a subgraph containing the specified entities and their neighbors.

        Useful for building context for GraphRAG retrieval.

        Args:
            entity_ids: Seed entity IDs
            max_hops: Maximum traversal depth from each seed
            rel_types: Optional filter by relationship types
            min_confidence: Optional minimum confidence threshold

        Returns:
            Graph containing entities and relationships
        """
        # Collect all entities and relationships
        all_entity_ids: set[str] = set(entity_ids)
        all_relationships: list[Relationship] = []

        # Expand from each seed entity
        for entity_id in entity_ids:
            neighbors = await self.get_neighbors(
                entity_id,
                hops=max_hops,
                rel_types=rel_types,
                min_confidence=min_confidence,
            )
            for neighbor in neighbors:
                all_entity_ids.add(neighbor.entity.entity_id)

        # Get all entities in a single batch query (avoid N+1)
        entities = []
        if all_entity_ids:
            stmt = select(EntityModel).where(
                and_(
                    EntityModel.zone_id == self.zone_id,
                    EntityModel.entity_id.in_(list(all_entity_ids)),
                )
            )
            result = await self.session.execute(stmt)
            entities = [Entity.from_model(m) for m in result.scalars().all()]

        # Get all relationships between these entities
        if all_entity_ids:
            entity_list = list(all_entity_ids)
            rel_stmt = select(RelationshipModel).where(
                and_(
                    RelationshipModel.zone_id == self.zone_id,
                    RelationshipModel.source_entity_id.in_(entity_list),
                    RelationshipModel.target_entity_id.in_(entity_list),
                )
            )
            if rel_types:
                rel_stmt = rel_stmt.where(RelationshipModel.relationship_type.in_(rel_types))
            if min_confidence:
                rel_stmt = rel_stmt.where(RelationshipModel.confidence >= min_confidence)

            rel_result = await self.session.execute(rel_stmt)
            all_relationships = [Relationship.from_model(m) for m in rel_result.scalars().all()]

        return Graph(entities=entities, relationships=all_relationships)

    async def find_path(
        self,
        source_entity_id: str,
        target_entity_id: str,
        max_hops: int = 5,
        rel_types: list[str] | None = None,
    ) -> list[str] | None:
        """Find a path between two entities.

        Args:
            source_entity_id: Starting entity ID
            target_entity_id: Target entity ID
            max_hops: Maximum path length
            rel_types: Optional filter by relationship types

        Returns:
            List of entity IDs in the path, or None if no path exists
        """
        neighbors = await self.get_neighbors(
            source_entity_id,
            hops=max_hops,
            rel_types=rel_types,
        )

        for neighbor in neighbors:
            if neighbor.entity.entity_id == target_entity_id:
                return neighbor.path + [target_entity_id]

        return None

    # =========================================================================
    # Bulk Operations
    # =========================================================================

    async def add_entities_bulk(
        self,
        entities: list[dict[str, Any]],
        resolve: bool = True,
    ) -> list[tuple[str, bool]]:
        """Add multiple entities in bulk.

        Args:
            entities: List of entity dicts with keys: name, entity_type, embedding, aliases, metadata
            resolve: If True, attempt entity resolution/deduplication (slower, sequential)
                     If False, use fast batch insert (no deduplication)

        Returns:
            List of (entity_id, was_created) tuples
        """
        if resolve:
            # With resolution, we need to check each entity individually
            results = []
            for entity_data in entities:
                entity_id, was_created = await self.add_entity(
                    name=entity_data.get("name", ""),
                    entity_type=entity_data.get("entity_type"),
                    embedding=entity_data.get("embedding"),
                    aliases=entity_data.get("aliases"),
                    metadata=entity_data.get("metadata"),
                    resolve=True,
                )
                results.append((entity_id, was_created))
            return results

        # Fast batch insert without resolution
        models = []
        results = []
        for entity_data in entities:
            entity_id = str(uuid.uuid4())
            name = entity_data.get("name", "")
            embedding = entity_data.get("embedding")
            aliases = entity_data.get("aliases")
            metadata = entity_data.get("metadata")

            model = EntityModel(
                entity_id=entity_id,
                zone_id=self.zone_id,
                canonical_name=name,
                entity_type=entity_data.get("entity_type"),
                embedding=json.dumps(embedding) if embedding else None,
                embedding_dim=len(embedding) if embedding else None,
                aliases=json.dumps(aliases) if aliases else None,
                merge_count=1,
                metadata_json=json.dumps(metadata) if metadata else None,
            )
            models.append(model)
            results.append((entity_id, True))

        # Batch insert
        self.session.add_all(models)
        await self.session.flush()
        return results

    async def add_relationships_bulk(
        self,
        relationships: list[dict[str, Any]],
        skip_duplicates: bool = True,
    ) -> list[tuple[str, bool]]:
        """Add multiple relationships in bulk.

        Args:
            relationships: List of relationship dicts with keys:
                source_entity_id, target_entity_id, relationship_type, weight, confidence, metadata
            skip_duplicates: If True, check for duplicates (slower, sequential)
                            If False, use fast batch insert (no duplicate check)

        Returns:
            List of (relationship_id, was_created) tuples
        """
        if skip_duplicates:
            # Check each relationship for duplicates
            results = []
            for rel_data in relationships:
                rel_id, was_created = await self.add_relationship(
                    source_entity_id=rel_data["source_entity_id"],
                    target_entity_id=rel_data["target_entity_id"],
                    relationship_type=rel_data["relationship_type"],
                    weight=rel_data.get("weight", 1.0),
                    confidence=rel_data.get("confidence", 1.0),
                    metadata=rel_data.get("metadata"),
                )
                results.append((rel_id, was_created))
            return results

        # Fast batch insert without duplicate check
        models = []
        results = []
        for rel_data in relationships:
            rel_id = str(uuid.uuid4())
            metadata = rel_data.get("metadata")

            model = RelationshipModel(
                relationship_id=rel_id,
                zone_id=self.zone_id,
                source_entity_id=rel_data["source_entity_id"],
                target_entity_id=rel_data["target_entity_id"],
                relationship_type=rel_data["relationship_type"],
                weight=rel_data.get("weight", 1.0),
                confidence=rel_data.get("confidence", 1.0),
                metadata_json=json.dumps(metadata) if metadata else None,
            )
            models.append(model)
            results.append((rel_id, True))

        # Batch insert
        self.session.add_all(models)
        await self.session.flush()
        return results

    # =========================================================================
    # Private Helper Methods
    # =========================================================================

    async def _find_by_name(
        self,
        name: str,
        entity_type: str | None = None,
    ) -> Entity | None:
        """Find entity by exact canonical name match."""
        stmt = select(EntityModel).where(
            and_(
                EntityModel.zone_id == self.zone_id,
                EntityModel.canonical_name == name,
            )
        )
        if entity_type:
            stmt = stmt.where(EntityModel.entity_type == entity_type)

        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return Entity.from_model(model) if model else None

    async def _find_relationship(
        self,
        source_entity_id: str,
        target_entity_id: str,
        relationship_type: str,
    ) -> Relationship | None:
        """Find existing relationship by source, target, and type."""
        stmt = select(RelationshipModel).where(
            and_(
                RelationshipModel.zone_id == self.zone_id,
                RelationshipModel.source_entity_id == source_entity_id,
                RelationshipModel.target_entity_id == target_entity_id,
                RelationshipModel.relationship_type == relationship_type,
            )
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return Relationship.from_model(model) if model else None

    async def _merge_aliases(self, entity_id: str, new_aliases: list[str]) -> None:
        """Merge new aliases into an entity."""
        entity = await self.get_entity(entity_id)
        if entity:
            existing = set(entity.aliases)
            updated = existing | set(new_aliases)
            if updated != existing:
                stmt = (
                    update(EntityModel)
                    .where(EntityModel.entity_id == entity_id)
                    .values(aliases=json.dumps(list(updated)))
                )
                await self.session.execute(stmt)

    async def _merge_into_entity(
        self,
        entity_id: str,
        new_alias: str,
        additional_aliases: list[str] | None = None,
    ) -> None:
        """Merge a new name into an existing entity as an alias."""
        entity = await self.get_entity(entity_id)
        if entity:
            aliases = set(entity.aliases)
            aliases.add(new_alias)
            if additional_aliases:
                aliases.update(additional_aliases)

            stmt = (
                update(EntityModel)
                .where(EntityModel.entity_id == entity_id)
                .values(
                    aliases=json.dumps(list(aliases)),
                    merge_count=entity.merge_count + 1,
                    updated_at=_utcnow_naive(),
                )
            )
            await self.session.execute(stmt)

    async def _update_relationship_weight(
        self,
        relationship_id: str,
        weight: float,
        confidence: float,
    ) -> None:
        """Update relationship weight and confidence."""
        stmt = (
            update(RelationshipModel)
            .where(RelationshipModel.relationship_id == relationship_id)
            .values(weight=weight, confidence=confidence)
        )
        await self.session.execute(stmt)

    async def _find_similar_pgvector(
        self,
        embedding: list[float],
        entity_type: str | None,
        limit: int,
        threshold: float,
    ) -> list[tuple[Entity, float]]:
        """Find similar entities using pgvector cosine distance."""
        # Build the query using raw SQL for pgvector operations
        embedding_json = json.dumps(embedding)

        query = text("""
            SELECT
                entity_id, zone_id, canonical_name, entity_type,
                embedding, embedding_model, embedding_dim,
                aliases, merge_count, metadata_json,
                created_at, updated_at,
                1 - (embedding::vector <=> :embedding::vector) as similarity
            FROM entities
            WHERE zone_id = :zone_id
              AND embedding IS NOT NULL
              AND (:entity_type IS NULL OR entity_type = :entity_type)
              AND 1 - (embedding::vector <=> :embedding::vector) >= :threshold
            ORDER BY embedding::vector <=> :embedding::vector
            LIMIT :limit
        """)

        result = await self.session.execute(
            query,
            {
                "embedding": embedding_json,
                "zone_id": self.zone_id,
                "entity_type": entity_type,
                "threshold": threshold,
                "limit": limit,
            },
        )

        entities = []
        for row in result.fetchall():
            entity = Entity(
                entity_id=row.entity_id,
                zone_id=row.zone_id,
                canonical_name=row.canonical_name,
                entity_type=row.entity_type,
                embedding=json.loads(row.embedding) if row.embedding else None,
                aliases=json.loads(row.aliases) if row.aliases else [],
                merge_count=row.merge_count,
                metadata=json.loads(row.metadata_json) if row.metadata_json else {},
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            entities.append((entity, row.similarity))

        return entities

    async def _find_similar_brute_force(
        self,
        embedding: list[float],
        entity_type: str | None,
        limit: int,
        threshold: float,
    ) -> list[tuple[Entity, float]]:
        """Find similar entities using brute-force cosine similarity (for SQLite)."""
        import math

        def cosine_similarity(a: list[float], b: list[float]) -> float:
            if len(a) != len(b):
                return 0.0
            dot = sum(x * y for x, y in zip(a, b, strict=False))
            norm_a = math.sqrt(sum(x * x for x in a))
            norm_b = math.sqrt(sum(x * x for x in b))
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return dot / (norm_a * norm_b)

        # Get all entities with embeddings
        stmt = select(EntityModel).where(
            and_(
                EntityModel.zone_id == self.zone_id,
                EntityModel.embedding.isnot(None),
            )
        )
        if entity_type:
            stmt = stmt.where(EntityModel.entity_type == entity_type)

        result = await self.session.execute(stmt)

        # Calculate similarities
        candidates = []
        for model in result.scalars().all():
            if model.embedding:
                entity_embedding = json.loads(model.embedding)
                similarity = cosine_similarity(embedding, entity_embedding)
                if similarity >= threshold:
                    candidates.append((Entity.from_model(model), similarity))

        # Sort by similarity and limit
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[:limit]

    async def _get_neighbors_postgres(
        self,
        entity_id: str,
        hops: int,
        direction: str,
        rel_types: list[str] | None,
        min_confidence: float | None,
    ) -> list[NeighborResult]:
        """Get neighbors using PostgreSQL recursive CTE."""
        # Build direction condition
        if direction == "outgoing":
            join_condition = "r.source_entity_id = gt.entity_id"
            next_entity = "r.target_entity_id"
        elif direction == "incoming":
            join_condition = "r.target_entity_id = gt.entity_id"
            next_entity = "r.source_entity_id"
        else:  # both
            join_condition = (
                "(r.source_entity_id = gt.entity_id OR r.target_entity_id = gt.entity_id)"
            )
            next_entity = "CASE WHEN r.source_entity_id = gt.entity_id THEN r.target_entity_id ELSE r.source_entity_id END"

        # Build relationship type filter
        rel_type_filter = ""
        if rel_types:
            rel_types_str = ", ".join(f"'{rt}'" for rt in rel_types)
            rel_type_filter = f"AND r.relationship_type IN ({rel_types_str})"

        # Build confidence filter
        confidence_filter = ""
        if min_confidence is not None:
            confidence_filter = f"AND r.confidence >= {min_confidence}"

        query = text(f"""
            WITH RECURSIVE graph_traversal(
                entity_id,
                depth,
                path
            ) AS (
                -- Base case: starting entity
                SELECT
                    e.entity_id::VARCHAR,
                    0 as depth,
                    ARRAY[e.entity_id]::VARCHAR[] as path
                FROM entities e
                WHERE e.entity_id = :entity_id
                  AND e.zone_id = :zone_id

                UNION ALL

                -- Recursive case: follow relationships
                SELECT
                    ({next_entity})::VARCHAR as entity_id,
                    gt.depth + 1 as depth,
                    gt.path || ({next_entity})::VARCHAR as path
                FROM relationships r
                JOIN graph_traversal gt ON {join_condition}
                WHERE gt.depth < :max_hops
                  AND NOT (({next_entity})::VARCHAR = ANY(gt.path))
                  AND r.zone_id = :zone_id
                  {rel_type_filter}
                  {confidence_filter}
            )
            SELECT DISTINCT ON (gt.entity_id)
                gt.entity_id,
                gt.depth,
                gt.path,
                e.zone_id,
                e.canonical_name,
                e.entity_type,
                e.embedding,
                e.aliases,
                e.merge_count,
                e.metadata_json,
                e.created_at,
                e.updated_at
            FROM graph_traversal gt
            JOIN entities e ON e.entity_id = gt.entity_id
            WHERE gt.depth > 0
            ORDER BY gt.entity_id, gt.depth
        """)

        result = await self.session.execute(
            query,
            {
                "entity_id": entity_id,
                "zone_id": self.zone_id,
                "max_hops": hops,
            },
        )

        neighbors = []
        for row in result.fetchall():
            entity = Entity(
                entity_id=row.entity_id,
                zone_id=row.zone_id,
                canonical_name=row.canonical_name,
                entity_type=row.entity_type,
                embedding=json.loads(row.embedding) if row.embedding else None,
                aliases=json.loads(row.aliases) if row.aliases else [],
                merge_count=row.merge_count,
                metadata=json.loads(row.metadata_json) if row.metadata_json else {},
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            neighbors.append(
                NeighborResult(
                    entity=entity,
                    depth=row.depth,
                    path=list(row.path),
                )
            )

        return neighbors

    async def _get_neighbors_sqlite(
        self,
        entity_id: str,
        hops: int,
        direction: str,
        rel_types: list[str] | None,
        min_confidence: float | None,
    ) -> list[NeighborResult]:
        """Get neighbors using SQLite recursive CTE."""
        # SQLite version of the recursive query (slightly different syntax)
        if direction == "outgoing":
            join_condition = "r.source_entity_id = gt.entity_id"
            next_entity = "r.target_entity_id"
        elif direction == "incoming":
            join_condition = "r.target_entity_id = gt.entity_id"
            next_entity = "r.source_entity_id"
        else:  # both
            join_condition = (
                "(r.source_entity_id = gt.entity_id OR r.target_entity_id = gt.entity_id)"
            )
            next_entity = "CASE WHEN r.source_entity_id = gt.entity_id THEN r.target_entity_id ELSE r.source_entity_id END"

        # Build relationship type filter
        rel_type_filter = ""
        if rel_types:
            rel_types_str = ", ".join(f"'{rt}'" for rt in rel_types)
            rel_type_filter = f"AND r.relationship_type IN ({rel_types_str})"

        # Build confidence filter
        confidence_filter = ""
        if min_confidence is not None:
            confidence_filter = f"AND r.confidence >= {min_confidence}"

        query = text(f"""
            WITH RECURSIVE graph_traversal(
                entity_id,
                depth,
                path
            ) AS (
                -- Base case: starting entity
                SELECT
                    e.entity_id,
                    0 as depth,
                    e.entity_id as path
                FROM entities e
                WHERE e.entity_id = :entity_id
                  AND e.zone_id = :zone_id

                UNION ALL

                -- Recursive case: follow relationships
                SELECT
                    {next_entity} as entity_id,
                    gt.depth + 1 as depth,
                    gt.path || ',' || {next_entity} as path
                FROM relationships r
                JOIN graph_traversal gt ON {join_condition}
                WHERE gt.depth < :max_hops
                  AND gt.path NOT LIKE '%' || {next_entity} || '%'
                  AND r.zone_id = :zone_id
                  {rel_type_filter}
                  {confidence_filter}
            )
            SELECT DISTINCT
                gt.entity_id,
                MIN(gt.depth) as depth,
                gt.path,
                e.zone_id,
                e.canonical_name,
                e.entity_type,
                e.embedding,
                e.aliases,
                e.merge_count,
                e.metadata_json,
                e.created_at,
                e.updated_at
            FROM graph_traversal gt
            JOIN entities e ON e.entity_id = gt.entity_id
            WHERE gt.depth > 0
            GROUP BY gt.entity_id
            ORDER BY depth
        """)

        result = await self.session.execute(
            query,
            {
                "entity_id": entity_id,
                "zone_id": self.zone_id,
                "max_hops": hops,
            },
        )

        neighbors = []
        for row in result.fetchall():
            entity = Entity(
                entity_id=row.entity_id,
                zone_id=row.zone_id,
                canonical_name=row.canonical_name,
                entity_type=row.entity_type,
                embedding=json.loads(row.embedding) if row.embedding else None,
                aliases=json.loads(row.aliases) if row.aliases else [],
                merge_count=row.merge_count,
                metadata=json.loads(row.metadata_json) if row.metadata_json else {},
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            # SQLite stores path as comma-separated string
            path = row.path.split(",") if row.path else []
            neighbors.append(
                NeighborResult(
                    entity=entity,
                    depth=row.depth,
                    path=path,
                )
            )

        return neighbors
