"""Tests for Graph Storage Layer (Issue #1039).

Tests for the PostgreSQL-native graph storage with entity resolution,
N-hop traversal, and subgraph extraction.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.search.graph_store import (
    Entity,
    EntityMention,
    Graph,
    GraphStore,
    NeighborResult,
    Relationship,
)
from nexus.storage.models import EntityMentionModel, EntityModel, RelationshipModel


class TestEntity:
    """Test Entity dataclass."""

    def test_from_model_basic(self):
        """Test creating Entity from SQLAlchemy model."""
        model = MagicMock(spec=EntityModel)
        model.entity_id = "test-entity-id"
        model.zone_id = "zone1"
        model.canonical_name = "John Smith"
        model.entity_type = "PERSON"
        model.embedding = None
        model.aliases = None
        model.merge_count = 1
        model.metadata_json = None
        model.created_at = None
        model.updated_at = None

        entity = Entity.from_model(model)

        assert entity.entity_id == "test-entity-id"
        assert entity.zone_id == "zone1"
        assert entity.canonical_name == "John Smith"
        assert entity.entity_type == "PERSON"
        assert entity.aliases == []
        assert entity.merge_count == 1
        assert entity.metadata == {}

    def test_from_model_with_json_fields(self):
        """Test creating Entity with JSON fields populated."""
        model = MagicMock(spec=EntityModel)
        model.entity_id = "test-entity-id"
        model.zone_id = "zone1"
        model.canonical_name = "Acme Corp"
        model.entity_type = "ORG"
        model.embedding = json.dumps([0.1, 0.2, 0.3])
        model.aliases = json.dumps(["Acme", "Acme Corporation", "ACME Inc."])
        model.merge_count = 3
        model.metadata_json = json.dumps({"industry": "tech", "employees": 500})
        model.created_at = None
        model.updated_at = None

        entity = Entity.from_model(model)

        assert entity.canonical_name == "Acme Corp"
        assert entity.embedding == [0.1, 0.2, 0.3]
        assert entity.aliases == ["Acme", "Acme Corporation", "ACME Inc."]
        assert entity.merge_count == 3
        assert entity.metadata == {"industry": "tech", "employees": 500}

    def test_from_model_invalid_json(self):
        """Test handling of invalid JSON in fields."""
        model = MagicMock(spec=EntityModel)
        model.entity_id = "test-entity-id"
        model.zone_id = "zone1"
        model.canonical_name = "Test Entity"
        model.entity_type = None
        model.embedding = "not valid json"
        model.aliases = "also not valid"
        model.merge_count = 1
        model.metadata_json = "{broken"
        model.created_at = None
        model.updated_at = None

        # Should not raise, should return defaults
        entity = Entity.from_model(model)

        assert entity.embedding is None
        assert entity.aliases == []
        assert entity.metadata == {}


class TestRelationship:
    """Test Relationship dataclass."""

    def test_from_model_basic(self):
        """Test creating Relationship from SQLAlchemy model."""
        model = MagicMock(spec=RelationshipModel)
        model.relationship_id = "rel-id-1"
        model.zone_id = "zone1"
        model.source_entity_id = "entity-1"
        model.target_entity_id = "entity-2"
        model.relationship_type = "MANAGES"
        model.weight = 1.0
        model.confidence = 0.95
        model.metadata_json = None
        model.created_at = None

        rel = Relationship.from_model(model)

        assert rel.relationship_id == "rel-id-1"
        assert rel.source_entity_id == "entity-1"
        assert rel.target_entity_id == "entity-2"
        assert rel.relationship_type == "MANAGES"
        assert rel.weight == 1.0
        assert rel.confidence == 0.95

    def test_from_model_with_entities(self):
        """Test creating Relationship with entity details."""
        rel_model = MagicMock(spec=RelationshipModel)
        rel_model.relationship_id = "rel-id-1"
        rel_model.zone_id = "zone1"
        rel_model.source_entity_id = "entity-1"
        rel_model.target_entity_id = "entity-2"
        rel_model.relationship_type = "WORKS_WITH"
        rel_model.weight = 1.0
        rel_model.confidence = 0.9
        rel_model.metadata_json = None
        rel_model.created_at = None

        source_model = MagicMock(spec=EntityModel)
        source_model.entity_id = "entity-1"
        source_model.zone_id = "zone1"
        source_model.canonical_name = "Alice"
        source_model.entity_type = "PERSON"
        source_model.embedding = None
        source_model.aliases = None
        source_model.merge_count = 1
        source_model.metadata_json = None
        source_model.created_at = None
        source_model.updated_at = None

        target_model = MagicMock(spec=EntityModel)
        target_model.entity_id = "entity-2"
        target_model.zone_id = "zone1"
        target_model.canonical_name = "Bob"
        target_model.entity_type = "PERSON"
        target_model.embedding = None
        target_model.aliases = None
        target_model.merge_count = 1
        target_model.metadata_json = None
        target_model.created_at = None
        target_model.updated_at = None

        rel = Relationship.from_model(rel_model, source_model, target_model)

        assert rel.source_entity is not None
        assert rel.source_entity.canonical_name == "Alice"
        assert rel.target_entity is not None
        assert rel.target_entity.canonical_name == "Bob"


class TestEntityMention:
    """Test EntityMention dataclass."""

    def test_from_model(self):
        """Test creating EntityMention from SQLAlchemy model."""
        model = MagicMock(spec=EntityMentionModel)
        model.mention_id = "mention-1"
        model.entity_id = "entity-1"
        model.chunk_id = "chunk-123"
        model.memory_id = None
        model.confidence = 0.95
        model.mention_text = "John"
        model.char_offset_start = 10
        model.char_offset_end = 14
        model.created_at = None

        mention = EntityMention.from_model(model)

        assert mention.mention_id == "mention-1"
        assert mention.entity_id == "entity-1"
        assert mention.chunk_id == "chunk-123"
        assert mention.memory_id is None
        assert mention.confidence == 0.95
        assert mention.mention_text == "John"
        assert mention.char_offset_start == 10
        assert mention.char_offset_end == 14


class TestGraph:
    """Test Graph dataclass."""

    def test_to_dict_empty(self):
        """Test empty graph conversion."""
        graph = Graph()
        result = graph.to_dict()

        assert result == {"entities": [], "relationships": []}

    def test_to_dict_with_data(self):
        """Test graph with entities and relationships."""
        entities = [
            Entity(
                entity_id="e1",
                zone_id="t1",
                canonical_name="Alice",
                entity_type="PERSON",
                aliases=["A"],
                merge_count=1,
            ),
            Entity(
                entity_id="e2",
                zone_id="t1",
                canonical_name="Bob",
                entity_type="PERSON",
                aliases=[],
                merge_count=1,
            ),
        ]
        relationships = [
            Relationship(
                relationship_id="r1",
                zone_id="t1",
                source_entity_id="e1",
                target_entity_id="e2",
                relationship_type="WORKS_WITH",
                weight=1.0,
                confidence=0.9,
            ),
        ]

        graph = Graph(entities=entities, relationships=relationships)
        result = graph.to_dict()

        assert len(result["entities"]) == 2
        assert result["entities"][0]["canonical_name"] == "Alice"
        assert len(result["relationships"]) == 1
        assert result["relationships"][0]["relationship_type"] == "WORKS_WITH"


class TestNeighborResult:
    """Test NeighborResult dataclass."""

    def test_creation(self):
        """Test creating NeighborResult."""
        entity = Entity(
            entity_id="e1",
            zone_id="t1",
            canonical_name="Alice",
        )
        result = NeighborResult(entity=entity, depth=2, path=["e0", "e1"])

        assert result.entity.canonical_name == "Alice"
        assert result.depth == 2
        assert result.path == ["e0", "e1"]


class TestGraphStoreInit:
    """Test GraphStore initialization."""

    def test_init_defaults(self):
        """Test initialization with default values."""
        mock_session = MagicMock()

        store = GraphStore(mock_session)

        assert store.session == mock_session
        assert store.zone_id == "default"
        assert store.embedding_provider is None
        assert store.merge_threshold == 0.85
        assert store.confidence_threshold == 0.75

    def test_init_custom_values(self):
        """Test initialization with custom values."""
        mock_session = MagicMock()
        mock_provider = MagicMock()

        store = GraphStore(
            mock_session,
            zone_id="custom-zone",
            embedding_provider=mock_provider,
            merge_threshold=0.9,
            confidence_threshold=0.8,
        )

        assert store.zone_id == "custom-zone"
        assert store.embedding_provider == mock_provider
        assert store.merge_threshold == 0.9
        assert store.confidence_threshold == 0.8


class TestGraphStoreEntityCRUD:
    """Test GraphStore entity CRUD operations."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        session = AsyncMock()
        return session

    @pytest.fixture
    def store(self, mock_session):
        """Create GraphStore with mock session."""
        return GraphStore(mock_session, zone_id="test-zone")

    @pytest.mark.asyncio
    async def test_add_entity_new(self, store, mock_session):
        """Test adding a new entity."""
        # Mock no existing entity found - use MagicMock for the result
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        # Make execute return a coroutine that returns the mock result
        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()

        entity_id, was_created = await store.add_entity(
            name="John Smith",
            entity_type="PERSON",
        )

        assert was_created is True
        assert entity_id is not None
        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_entity_existing(self, store, mock_session):
        """Test adding entity that already exists."""
        # Mock existing entity found
        existing_model = MagicMock(spec=EntityModel)
        existing_model.entity_id = "existing-id"
        existing_model.zone_id = "test-zone"
        existing_model.canonical_name = "John Smith"
        existing_model.entity_type = "PERSON"
        existing_model.embedding = None
        existing_model.aliases = None
        existing_model.merge_count = 1
        existing_model.metadata_json = None
        existing_model.created_at = None
        existing_model.updated_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = existing_model

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute
        mock_session.add = MagicMock()

        entity_id, was_created = await store.add_entity(
            name="John Smith",
            entity_type="PERSON",
        )

        assert was_created is False
        assert entity_id == "existing-id"
        mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_entity(self, store, mock_session):
        """Test getting an entity by ID."""
        mock_model = MagicMock(spec=EntityModel)
        mock_model.entity_id = "test-id"
        mock_model.zone_id = "test-zone"
        mock_model.canonical_name = "Test Entity"
        mock_model.entity_type = "CONCEPT"
        mock_model.embedding = None
        mock_model.aliases = None
        mock_model.merge_count = 1
        mock_model.metadata_json = None
        mock_model.created_at = None
        mock_model.updated_at = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_model

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        entity = await store.get_entity("test-id")

        assert entity is not None
        assert entity.entity_id == "test-id"
        assert entity.canonical_name == "Test Entity"

    @pytest.mark.asyncio
    async def test_get_entity_not_found(self, store, mock_session):
        """Test getting non-existent entity."""
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        entity = await store.get_entity("non-existent-id")

        assert entity is None

    @pytest.mark.asyncio
    async def test_delete_entity(self, store, mock_session):
        """Test deleting an entity."""
        mock_result = MagicMock()
        mock_result.rowcount = 1

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        success = await store.delete_entity("test-id")

        assert success is True

    @pytest.mark.asyncio
    async def test_delete_entity_not_found(self, store, mock_session):
        """Test deleting non-existent entity."""
        mock_result = MagicMock()
        mock_result.rowcount = 0

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        success = await store.delete_entity("non-existent-id")

        assert success is False


class TestGraphStoreRelationshipCRUD:
    """Test GraphStore relationship CRUD operations."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return AsyncMock()

    @pytest.fixture
    def store(self, mock_session):
        """Create GraphStore with mock session."""
        return GraphStore(mock_session, zone_id="test-zone")

    @pytest.mark.asyncio
    async def test_add_relationship_new(self, store, mock_session):
        """Test adding a new relationship."""
        # Mock no existing relationship
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()

        rel_id, was_created = await store.add_relationship(
            source_entity_id="entity-1",
            target_entity_id="entity-2",
            relationship_type="MANAGES",
            confidence=0.95,
        )

        assert was_created is True
        assert rel_id is not None
        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_relationships_outgoing(self, store, mock_session):
        """Test getting outgoing relationships."""
        mock_rel = MagicMock(spec=RelationshipModel)
        mock_rel.relationship_id = "rel-1"
        mock_rel.zone_id = "test-zone"
        mock_rel.source_entity_id = "entity-1"
        mock_rel.target_entity_id = "entity-2"
        mock_rel.relationship_type = "MANAGES"
        mock_rel.weight = 1.0
        mock_rel.confidence = 0.95
        mock_rel.metadata_json = None
        mock_rel.created_at = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_rel]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        relationships = await store.get_relationships("entity-1", direction="outgoing")

        assert len(relationships) == 1
        assert relationships[0].relationship_type == "MANAGES"


class TestGraphStoreSimilaritySearch:
    """Test entity similarity search."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return AsyncMock()

    @pytest.fixture
    def store(self, mock_session):
        """Create GraphStore with mock session."""
        return GraphStore(mock_session, zone_id="test-zone")

    @pytest.mark.asyncio
    async def test_find_similar_brute_force(self, store, mock_session):
        """Test brute-force similarity search (SQLite path)."""
        # Create mock entity with embedding
        mock_model = MagicMock(spec=EntityModel)
        mock_model.entity_id = "entity-1"
        mock_model.zone_id = "test-zone"
        mock_model.canonical_name = "Similar Entity"
        mock_model.entity_type = "CONCEPT"
        mock_model.embedding = json.dumps([1.0, 0.0, 0.0])  # Normalized vector
        mock_model.aliases = None
        mock_model.merge_count = 1
        mock_model.metadata_json = None
        mock_model.created_at = None
        mock_model.updated_at = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_model]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        # Query with similar embedding
        with patch.dict("os.environ", {"NEXUS_DATABASE_URL": "sqlite:///test.db"}):
            results = await store.find_similar_entities(
                embedding=[1.0, 0.0, 0.0],
                limit=5,
                threshold=0.8,
            )

        assert len(results) == 1
        entity, similarity = results[0]
        assert entity.canonical_name == "Similar Entity"
        assert similarity == 1.0  # Identical vectors


class TestGraphStoreEntityMentions:
    """Test entity mention operations."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return AsyncMock()

    @pytest.fixture
    def store(self, mock_session):
        """Create GraphStore with mock session."""
        return GraphStore(mock_session, zone_id="test-zone")

    @pytest.mark.asyncio
    async def test_add_mention(self, store, mock_session):
        """Test adding an entity mention."""
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()

        mention_id = await store.add_mention(
            entity_id="entity-1",
            chunk_id="chunk-123",
            confidence=0.95,
            mention_text="John",
            char_offset_start=10,
            char_offset_end=14,
        )

        assert mention_id is not None
        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_entity_mentions(self, store, mock_session):
        """Test getting mentions of an entity."""
        mock_mention = MagicMock(spec=EntityMentionModel)
        mock_mention.mention_id = "mention-1"
        mock_mention.entity_id = "entity-1"
        mock_mention.chunk_id = "chunk-123"
        mock_mention.memory_id = None
        mock_mention.confidence = 0.95
        mock_mention.mention_text = "John"
        mock_mention.char_offset_start = 10
        mock_mention.char_offset_end = 14
        mock_mention.created_at = None

        mock_scalars = MagicMock()
        mock_scalars.all.return_value = [mock_mention]
        mock_result = MagicMock()
        mock_result.scalars.return_value = mock_scalars

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute

        mentions = await store.get_entity_mentions("entity-1")

        assert len(mentions) == 1
        assert mentions[0].mention_text == "John"


class TestGraphStoreBulkOperations:
    """Test bulk operations."""

    @pytest.fixture
    def mock_session(self):
        """Create a mock async session."""
        return AsyncMock()

    @pytest.fixture
    def store(self, mock_session):
        """Create GraphStore with mock session."""
        return GraphStore(mock_session, zone_id="test-zone")

    @pytest.mark.asyncio
    async def test_add_entities_bulk(self, store, mock_session):
        """Test adding multiple entities."""
        # Mock no existing entities
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()

        entities = [
            {"name": "Alice", "entity_type": "PERSON"},
            {"name": "Bob", "entity_type": "PERSON"},
            {"name": "Acme Corp", "entity_type": "ORG"},
        ]

        results = await store.add_entities_bulk(entities)

        assert len(results) == 3
        assert all(was_created for _, was_created in results)

    @pytest.mark.asyncio
    async def test_add_relationships_bulk(self, store, mock_session):
        """Test adding multiple relationships."""
        # Mock no existing relationships
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None

        async def mock_execute(*args, **kwargs):
            return mock_result

        mock_session.execute = mock_execute
        mock_session.flush = AsyncMock()
        mock_session.add = MagicMock()

        relationships = [
            {
                "source_entity_id": "e1",
                "target_entity_id": "e2",
                "relationship_type": "WORKS_WITH",
            },
            {
                "source_entity_id": "e1",
                "target_entity_id": "e3",
                "relationship_type": "MANAGES",
                "confidence": 0.9,
            },
        ]

        results = await store.add_relationships_bulk(relationships)

        assert len(results) == 2
        assert all(was_created for _, was_created in results)
