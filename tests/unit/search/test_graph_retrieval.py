"""Tests for Graph-Enhanced Retrieval (Issue #1040).

Tests for the LightRAG-style dual-level retrieval combining entity-based
(low-level) and theme-based (high-level) search.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.search.graph_retrieval import (
    VALID_GRAPH_MODES,
    GraphContext,
    GraphEnhancedRetriever,
    GraphEnhancedSearchResult,
    GraphRetrievalConfig,
    graph_enhanced_fusion,
)
from nexus.search.semantic import SemanticSearchResult


class TestGraphRetrievalConfig:
    """Test GraphRetrievalConfig dataclass."""

    def test_default_config(self):
        """Test default configuration values."""
        config = GraphRetrievalConfig()

        assert config.graph_mode == "none"
        assert config.entity_similarity_threshold == 0.75
        assert config.neighbor_hops == 2
        assert config.max_entities_per_query == 5
        assert config.prefer_abstracts is True
        assert config.expand_threshold == 0.7
        assert config.lambda_semantic == 0.4
        assert config.lambda_keyword == 0.3
        assert config.lambda_graph == 0.3
        assert config.parallel_search is True

    def test_valid_graph_modes(self):
        """Test that all valid graph modes are accepted."""
        for mode in VALID_GRAPH_MODES:
            config = GraphRetrievalConfig(graph_mode=mode)
            assert config.graph_mode == mode

    def test_invalid_graph_mode_raises(self):
        """Test that invalid graph mode raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            GraphRetrievalConfig(graph_mode="invalid")

        assert "Invalid graph_mode" in str(exc_info.value)
        assert "invalid" in str(exc_info.value)

    def test_invalid_entity_threshold_raises(self):
        """Test that out-of-range entity threshold raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            GraphRetrievalConfig(entity_similarity_threshold=1.5)

        assert "entity_similarity_threshold" in str(exc_info.value)

        with pytest.raises(ValueError):
            GraphRetrievalConfig(entity_similarity_threshold=-0.1)

    def test_invalid_neighbor_hops_raises(self):
        """Test that invalid neighbor hops raises ValueError."""
        with pytest.raises(ValueError) as exc_info:
            GraphRetrievalConfig(neighbor_hops=15)

        assert "neighbor_hops" in str(exc_info.value)

        with pytest.raises(ValueError):
            GraphRetrievalConfig(neighbor_hops=-1)

    def test_custom_lambda_weights(self):
        """Test custom lambda weights configuration."""
        config = GraphRetrievalConfig(
            lambda_semantic=0.5,
            lambda_keyword=0.3,
            lambda_graph=0.2,
        )

        assert config.lambda_semantic == 0.5
        assert config.lambda_keyword == 0.3
        assert config.lambda_graph == 0.2
        # Weights should sum to 1.0 for normalized scoring
        assert config.lambda_semantic + config.lambda_keyword + config.lambda_graph == 1.0


class TestGraphContext:
    """Test GraphContext dataclass."""

    def test_empty_context(self):
        """Test creating empty graph context."""
        context = GraphContext()

        assert context.entities == []
        assert context.relationships == []
        assert context.theme is None
        assert context.theme_abstraction_level == 0
        assert context.graph_proximity_score == 0.0

    def test_to_dict_empty(self):
        """Test to_dict with empty context."""
        context = GraphContext()
        result = context.to_dict()

        assert result == {
            "entities": [],
            "relationships": [],
            "theme": None,
            "theme_abstraction_level": 0,
            "graph_proximity_score": 0.0,
        }

    def test_to_dict_with_entities(self):
        """Test to_dict with entities populated."""
        # Create mock entities
        entity1 = MagicMock()
        entity1.to_dict.return_value = {
            "entity_id": "e1",
            "canonical_name": "Alice",
            "entity_type": "PERSON",
        }

        context = GraphContext(
            entities=[entity1],
            theme="User Authentication",
            theme_abstraction_level=2,
            graph_proximity_score=0.85,
        )
        result = context.to_dict()

        assert len(result["entities"]) == 1
        assert result["entities"][0]["canonical_name"] == "Alice"
        assert result["theme"] == "User Authentication"
        assert result["theme_abstraction_level"] == 2
        assert result["graph_proximity_score"] == 0.85

    def test_to_dict_with_relationships(self):
        """Test to_dict with relationships populated."""
        # Create mock relationship
        rel = MagicMock()
        rel.source_entity_id = "e1"
        rel.relationship_type = "WORKS_WITH"
        rel.target_entity_id = "e2"
        rel.confidence = 0.95

        context = GraphContext(relationships=[rel])
        result = context.to_dict()

        assert len(result["relationships"]) == 1
        assert result["relationships"][0] == {
            "source": "e1",
            "predicate": "WORKS_WITH",
            "target": "e2",
            "confidence": 0.95,
        }


class TestGraphEnhancedSearchResult:
    """Test GraphEnhancedSearchResult dataclass."""

    def test_basic_result(self):
        """Test creating a basic search result."""
        result = GraphEnhancedSearchResult(
            path="/docs/auth.md",
            chunk_index=0,
            chunk_text="Authentication uses JWT tokens.",
            score=0.85,
        )

        assert result.path == "/docs/auth.md"
        assert result.chunk_index == 0
        assert result.score == 0.85
        assert result.graph_score is None
        assert result.graph_context is None

    def test_from_semantic_result(self):
        """Test creating from SemanticSearchResult."""
        semantic_result = SemanticSearchResult(
            path="/docs/auth.md",
            chunk_index=1,
            chunk_text="OAuth2 flow explained.",
            score=0.9,
            start_offset=100,
            end_offset=200,
            line_start=10,
            line_end=15,
            keyword_score=0.7,
            vector_score=0.95,
        )

        result = GraphEnhancedSearchResult.from_semantic_result(
            semantic_result,
            chunk_id="chunk-123",
        )

        assert result.path == "/docs/auth.md"
        assert result.chunk_index == 1
        assert result.score == 0.9
        assert result.keyword_score == 0.7
        assert result.vector_score == 0.95
        assert result.chunk_id == "chunk-123"
        assert result.graph_score is None  # Not set yet

    def test_to_dict(self):
        """Test serializing to dictionary."""
        result = GraphEnhancedSearchResult(
            path="/docs/auth.md",
            chunk_index=0,
            chunk_text="JWT tokens are used.",
            score=0.85,
            keyword_score=0.6,
            vector_score=0.9,
            graph_score=0.8,
            graph_context=GraphContext(
                theme="Authentication",
                graph_proximity_score=0.8,
            ),
        )

        data = result.to_dict()

        assert data["path"] == "/docs/auth.md"
        assert data["score"] == 0.85
        assert data["keyword_score"] == 0.6
        assert data["vector_score"] == 0.9
        assert data["graph_score"] == 0.8
        assert data["graph_context"]["theme"] == "Authentication"


class TestGraphEnhancedFusion:
    """Test graph_enhanced_fusion function."""

    def test_empty_inputs(self):
        """Test fusion with empty inputs."""
        results = graph_enhanced_fusion(
            keyword_results=[],
            vector_results=[],
            graph_boost_ids=set(),
            theme_boost_ids=set(),
        )

        assert results == []

    def test_keyword_only(self):
        """Test fusion with only keyword results."""
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.8,
            },
            {
                "chunk_id": "c2",
                "path": "/b.md",
                "chunk_index": 0,
                "chunk_text": "test2",
                "score": 0.6,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=[],
            graph_boost_ids=set(),
            theme_boost_ids=set(),
            lambda_semantic=0.4,
            lambda_keyword=0.6,
            lambda_graph=0.0,
        )

        assert len(results) == 2
        # c1 should rank higher due to higher keyword score
        assert results[0]["chunk_id"] == "c1"

    def test_vector_only(self):
        """Test fusion with only vector results."""
        vector_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.9,
            },
            {
                "chunk_id": "c2",
                "path": "/b.md",
                "chunk_index": 0,
                "chunk_text": "test2",
                "score": 0.7,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=[],
            vector_results=vector_results,
            graph_boost_ids=set(),
            theme_boost_ids=set(),
            lambda_semantic=0.6,
            lambda_keyword=0.4,
            lambda_graph=0.0,
        )

        assert len(results) == 2
        assert results[0]["chunk_id"] == "c1"

    def test_graph_boost_direct_entity(self):
        """Test that direct entity matches get graph_score=1.0."""
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.5,
            },
        ]
        vector_results = [
            {
                "chunk_id": "c2",
                "path": "/b.md",
                "chunk_index": 0,
                "chunk_text": "test2",
                "score": 0.9,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids={"c1"},  # c1 has entity match
            theme_boost_ids=set(),
            lambda_semantic=0.3,
            lambda_keyword=0.3,
            lambda_graph=0.4,
        )

        # c1 should have graph_score=1.0
        c1_result = next(r for r in results if r["chunk_id"] == "c1")
        assert c1_result["graph_score"] == 1.0

        # c2 should have graph_score=0.0
        c2_result = next(r for r in results if r["chunk_id"] == "c2")
        assert c2_result["graph_score"] == 0.0

    def test_theme_boost(self):
        """Test that theme matches get graph_score=0.7."""
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.5,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=[],
            graph_boost_ids=set(),
            theme_boost_ids={"c1"},  # c1 has theme match
        )

        c1_result = results[0]
        assert c1_result["graph_score"] == 0.7

    def test_entity_boost_trumps_theme(self):
        """Test that entity match (1.0) takes precedence over theme match (0.7)."""
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.5,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=[],
            graph_boost_ids={"c1"},  # Also in entity match
            theme_boost_ids={"c1"},  # In theme match
        )

        c1_result = results[0]
        assert c1_result["graph_score"] == 1.0  # Entity takes precedence

    def test_combined_scoring(self):
        """Test combined score calculation."""
        keyword_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.8,
            },
        ]
        vector_results = [
            {
                "chunk_id": "c1",
                "path": "/a.md",
                "chunk_index": 0,
                "chunk_text": "test",
                "score": 0.9,
            },
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=vector_results,
            graph_boost_ids={"c1"},
            theme_boost_ids=set(),
            lambda_semantic=0.4,
            lambda_keyword=0.3,
            lambda_graph=0.3,
        )

        c1_result = results[0]
        # With normalized scores (both are 1.0 since single result each)
        # and graph_score=1.0:
        # score = 0.4 * 1.0 + 0.3 * 1.0 + 0.3 * 1.0 = 1.0
        assert c1_result["score"] == pytest.approx(1.0)

    def test_limit_respected(self):
        """Test that limit parameter is respected."""
        keyword_results = [
            {
                "chunk_id": f"c{i}",
                "path": f"/{i}.md",
                "chunk_index": 0,
                "chunk_text": f"test{i}",
                "score": 1 - i * 0.1,
            }
            for i in range(10)
        ]

        results = graph_enhanced_fusion(
            keyword_results=keyword_results,
            vector_results=[],
            graph_boost_ids=set(),
            theme_boost_ids=set(),
            limit=5,
        )

        assert len(results) == 5


class TestGraphEnhancedRetriever:
    """Test GraphEnhancedRetriever class."""

    @pytest.fixture
    def mock_semantic_search(self):
        """Create mock SemanticSearch."""
        mock = MagicMock()
        mock.embedding_provider = MagicMock()
        mock.search = AsyncMock(
            return_value=[
                SemanticSearchResult(
                    path="/docs/auth.md",
                    chunk_index=0,
                    chunk_text="Authentication with JWT.",
                    score=0.85,
                    keyword_score=0.7,
                    vector_score=0.9,
                ),
            ]
        )
        return mock

    @pytest.fixture
    def mock_graph_store(self):
        """Create mock GraphStore."""
        mock = MagicMock()
        mock.find_similar_entities = AsyncMock(return_value=[])
        mock.get_entity_mentions = AsyncMock(return_value=[])
        mock.get_neighbors = AsyncMock(return_value=[])
        mock.get_entities_in_chunk = AsyncMock(return_value=[])
        mock.get_subgraph = AsyncMock(return_value=MagicMock(entities=[], relationships=[]))
        return mock

    @pytest.fixture
    def mock_hierarchy_manager(self):
        """Create mock HierarchicalMemoryManager."""
        mock = MagicMock()
        mock.retrieve_with_hierarchy = MagicMock(
            return_value=MagicMock(
                memories=[],
                abstracts_used=0,
                atomics_used=0,
                expanded_from_abstracts=0,
            )
        )
        return mock

    @pytest.fixture
    def mock_embedding_provider(self):
        """Create mock EmbeddingProvider."""
        mock = MagicMock()
        mock.embed_texts = AsyncMock(return_value=[[0.1] * 384])
        return mock

    def test_init_with_defaults(self, mock_semantic_search):
        """Test initialization with default config."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
        )

        assert retriever.config.graph_mode == "none"
        assert retriever.graph_store is None
        assert retriever.hierarchy_manager is None

    def test_init_mode_fallback_no_graph_store(self, mock_semantic_search):
        """Test that graph_mode falls back when graph_store missing."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            config=GraphRetrievalConfig(graph_mode="low"),
        )

        # Should fall back to "none" since no graph_store
        assert retriever.config.graph_mode == "none"

    def test_init_mode_fallback_no_hierarchy(self, mock_semantic_search, mock_graph_store):
        """Test that high mode falls back when hierarchy_manager missing."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            graph_store=mock_graph_store,
            config=GraphRetrievalConfig(graph_mode="high"),
        )

        # Should fall back to "low" since graph_store exists
        assert retriever.config.graph_mode == "low"

    def test_init_dual_mode_no_hierarchy(self, mock_semantic_search, mock_graph_store):
        """Test that dual mode falls back to low when hierarchy missing."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            graph_store=mock_graph_store,
            config=GraphRetrievalConfig(graph_mode="dual"),
        )

        # Should fall back to "low"
        assert retriever.config.graph_mode == "low"

    @pytest.mark.asyncio
    async def test_search_mode_none(self, mock_semantic_search):
        """Test search with mode='none' uses only semantic search."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            config=GraphRetrievalConfig(graph_mode="none"),
        )

        results = await retriever.search("test query")

        assert len(results) == 1
        assert isinstance(results[0], GraphEnhancedSearchResult)
        mock_semantic_search.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_invalid_mode_raises(self, mock_semantic_search):
        """Test that invalid graph_mode in search raises."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
        )

        with pytest.raises(ValueError) as exc_info:
            await retriever.search("test", graph_mode="invalid")

        assert "Invalid graph_mode" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_search_mode_low(
        self,
        mock_semantic_search,
        mock_graph_store,
        mock_embedding_provider,
    ):
        """Test search with mode='low' uses entity search."""
        # Set up entity match
        mock_entity = MagicMock()
        mock_entity.entity_id = "e1"
        mock_entity.canonical_name = "Authentication"
        mock_entity.to_dict.return_value = {"entity_id": "e1", "canonical_name": "Authentication"}

        mock_mention = MagicMock()
        mock_mention.chunk_id = "/docs/auth.md:0"

        mock_graph_store.find_similar_entities = AsyncMock(return_value=[(mock_entity, 0.9)])
        mock_graph_store.get_entity_mentions = AsyncMock(return_value=[mock_mention])
        mock_graph_store.get_neighbors = AsyncMock(return_value=[])

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            graph_store=mock_graph_store,
            embedding_provider=mock_embedding_provider,
            config=GraphRetrievalConfig(graph_mode="low"),
        )

        results = await retriever.search("authentication")

        assert len(results) >= 1
        mock_graph_store.find_similar_entities.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_mode_override(self, mock_semantic_search):
        """Test that graph_mode parameter overrides config."""
        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            config=GraphRetrievalConfig(graph_mode="none"),
        )

        # Override to "none" explicitly should still work
        results = await retriever.search("test", graph_mode="none")

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_search_returns_graph_context(
        self,
        mock_semantic_search,
        mock_graph_store,
        mock_embedding_provider,
    ):
        """Test that search populates graph_context when requested."""
        mock_entity = MagicMock()
        mock_entity.entity_id = "e1"
        mock_entity.canonical_name = "JWT"
        mock_entity.to_dict.return_value = {"entity_id": "e1", "canonical_name": "JWT"}

        mock_subgraph = MagicMock()
        mock_subgraph.entities = [mock_entity]
        mock_subgraph.relationships = []

        mock_graph_store.find_similar_entities = AsyncMock(return_value=[(mock_entity, 0.9)])
        mock_graph_store.get_entity_mentions = AsyncMock(return_value=[])
        mock_graph_store.get_neighbors = AsyncMock(return_value=[])
        mock_graph_store.get_entities_in_chunk = AsyncMock(return_value=[mock_entity])
        mock_graph_store.get_subgraph = AsyncMock(return_value=mock_subgraph)

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic_search,
            graph_store=mock_graph_store,
            embedding_provider=mock_embedding_provider,
            config=GraphRetrievalConfig(graph_mode="low"),
        )

        results = await retriever.search("JWT tokens", include_graph_context=True)

        assert len(results) >= 1
        assert results[0].graph_context is not None
        assert len(results[0].graph_context.entities) > 0


class TestGraphEnhancedRetrieverLowLevel:
    """Test _search_low_level method."""

    @pytest.fixture
    def mock_graph_store(self):
        """Create mock GraphStore for low-level tests."""
        mock = MagicMock()
        return mock

    @pytest.mark.asyncio
    async def test_low_level_no_entities_found(self, mock_graph_store):
        """Test low-level search with no entity matches."""
        mock_graph_store.find_similar_entities = AsyncMock(return_value=[])

        mock_semantic = MagicMock()
        mock_semantic.embedding_provider = MagicMock()
        mock_semantic.search = AsyncMock(return_value=[])

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic,
            graph_store=mock_graph_store,
            config=GraphRetrievalConfig(graph_mode="low"),
        )

        entities, chunk_ids = await retriever._search_low_level(
            query="unknown topic",
            query_embedding=[0.1] * 384,
            limit=10,
        )

        assert entities == []
        assert chunk_ids == set()

    @pytest.mark.asyncio
    async def test_low_level_with_entity_expansion(self, mock_graph_store):
        """Test low-level search with entity neighbor expansion."""
        # Set up entity and neighbors
        mock_entity = MagicMock()
        mock_entity.entity_id = "e1"
        mock_entity.canonical_name = "Alice"

        mock_neighbor_entity = MagicMock()
        mock_neighbor_entity.entity_id = "e2"
        mock_neighbor_entity.canonical_name = "Bob"

        mock_neighbor = MagicMock()
        mock_neighbor.entity = mock_neighbor_entity

        mock_mention = MagicMock()
        mock_mention.chunk_id = "chunk-1"

        mock_neighbor_mention = MagicMock()
        mock_neighbor_mention.chunk_id = "chunk-2"

        mock_graph_store.find_similar_entities = AsyncMock(return_value=[(mock_entity, 0.9)])
        mock_graph_store.get_entity_mentions = AsyncMock(
            side_effect=[
                [mock_mention],  # First call for main entity
                [mock_neighbor_mention],  # Second call for neighbor
            ]
        )
        mock_graph_store.get_neighbors = AsyncMock(return_value=[mock_neighbor])

        mock_semantic = MagicMock()
        mock_semantic.embedding_provider = MagicMock()
        mock_semantic.search = AsyncMock(return_value=[])

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic,
            graph_store=mock_graph_store,
            config=GraphRetrievalConfig(graph_mode="low", neighbor_hops=1),
        )

        entities, chunk_ids = await retriever._search_low_level(
            query="Alice",
            query_embedding=[0.1] * 384,
            limit=10,
        )

        assert len(entities) == 1
        assert entities[0].entity_id == "e1"
        assert "chunk-1" in chunk_ids
        assert "chunk-2" in chunk_ids


class TestGraphEnhancedRetrieverHighLevel:
    """Test _search_high_level method."""

    @pytest.mark.asyncio
    async def test_high_level_no_hierarchy_manager(self):
        """Test high-level search without hierarchy manager."""
        mock_semantic = MagicMock()
        mock_semantic.embedding_provider = MagicMock()
        mock_semantic.search = AsyncMock(return_value=[])

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic,
            config=GraphRetrievalConfig(graph_mode="none"),
        )

        chunk_ids = await retriever._search_high_level(
            query_embedding=[0.1] * 384,
            limit=10,
        )

        assert chunk_ids == set()

    @pytest.mark.asyncio
    async def test_high_level_with_themes(self):
        """Test high-level search with theme matches."""
        mock_memory = MagicMock()
        mock_memory.memory_id = "mem-123"
        mock_memory.metadata_json = '{"source_chunk_id": "chunk-456"}'

        mock_hierarchy = MagicMock()
        mock_hierarchy.retrieve_with_hierarchy = MagicMock(
            return_value=MagicMock(
                memories=[mock_memory],
                abstracts_used=1,
                atomics_used=0,
                expanded_from_abstracts=0,
            )
        )

        mock_semantic = MagicMock()
        mock_semantic.embedding_provider = MagicMock()
        mock_semantic.search = AsyncMock(return_value=[])

        retriever = GraphEnhancedRetriever(
            semantic_search=mock_semantic,
            hierarchy_manager=mock_hierarchy,
            config=GraphRetrievalConfig(graph_mode="high"),
        )

        chunk_ids = await retriever._search_high_level(
            query_embedding=[0.1] * 384,
            limit=10,
        )

        assert "chunk-456" in chunk_ids
        assert "memory:mem-123" in chunk_ids
