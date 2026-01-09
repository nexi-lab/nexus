"""Tests for ContextBuilder.

These tests verify the context building functionality for LLM prompts,
including adaptive retrieval depth based on query complexity (Issue #1021).
"""

from dataclasses import dataclass

import pytest


# Mock SemanticSearchResult for testing
@dataclass
class MockSearchResult:
    """Mock search result for testing."""

    path: str
    chunk_index: int
    chunk_text: str
    score: float | None = None
    start_offset: int | None = None
    end_offset: int | None = None


class TestContextBuilder:
    """Test ContextBuilder functionality."""

    @pytest.fixture
    def builder(self):
        """Create a context builder instance."""
        from nexus.llm.context_builder import ContextBuilder

        return ContextBuilder(max_context_tokens=3000)

    def test_init_default(self) -> None:
        """Test default initialization."""
        from nexus.llm.context_builder import ContextBuilder

        builder = ContextBuilder()
        assert builder.max_context_tokens == 3000

    def test_init_custom_tokens(self) -> None:
        """Test custom token limit."""
        from nexus.llm.context_builder import ContextBuilder

        builder = ContextBuilder(max_context_tokens=5000)
        assert builder.max_context_tokens == 5000

    def test_build_context_empty(self, builder) -> None:
        """Test building context with empty chunks."""
        result = builder.build_context([])
        assert result == ""

    def test_build_context_single_chunk(self, builder) -> None:
        """Test building context with single chunk."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="This is test content.",
                score=0.95,
            )
        ]
        result = builder.build_context(chunks)
        assert "/test.txt" in result
        assert "This is test content." in result
        assert "0.95" in result

    def test_build_context_multiple_chunks(self, builder) -> None:
        """Test building context with multiple chunks."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/a.txt",
                chunk_index=0,
                chunk_text="Content A",
                score=0.9,
            ),
            SemanticSearchResult(
                path="/b.txt",
                chunk_index=1,
                chunk_text="Content B",
                score=0.8,
            ),
        ]
        result = builder.build_context(chunks)
        assert "/a.txt" in result
        assert "/b.txt" in result
        assert "Content A" in result
        assert "Content B" in result

    def test_build_context_no_metadata(self, builder) -> None:
        """Test building context without metadata."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="Test content",
                score=0.9,
            )
        ]
        result = builder.build_context(chunks, include_metadata=False)
        assert "/test.txt" not in result
        assert "Test content" in result

    def test_build_context_no_scores(self, builder) -> None:
        """Test building context without scores."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="Test content",
                score=0.9,
            )
        ]
        result = builder.build_context(chunks, include_scores=False)
        assert "0.9" not in result
        assert "/test.txt" in result

    def test_build_context_respects_token_limit(self) -> None:
        """Test that context builder respects token limit."""
        from nexus.llm.context_builder import ContextBuilder
        from nexus.search.semantic import SemanticSearchResult

        # Create builder with small token limit (100 tokens ≈ 400 chars)
        builder = ContextBuilder(max_context_tokens=100)

        # Create chunks that would exceed limit
        chunks = [
            SemanticSearchResult(
                path="/a.txt",
                chunk_index=0,
                chunk_text="A" * 200,  # 50 tokens
                score=0.9,
            ),
            SemanticSearchResult(
                path="/b.txt",
                chunk_index=0,
                chunk_text="B" * 200,  # 50 tokens
                score=0.8,
            ),
            SemanticSearchResult(
                path="/c.txt",
                chunk_index=0,
                chunk_text="C" * 200,  # 50 tokens - should be cut off
                score=0.7,
            ),
        ]
        result = builder.build_context(chunks)
        # Third chunk should be excluded due to token limit
        assert "/c.txt" not in result or "C" * 200 not in result

    def test_build_simple_context(self, builder) -> None:
        """Test building simple context without metadata."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="Simple content",
                score=0.9,
            )
        ]
        result = builder.build_simple_context(chunks)
        assert "Simple content" in result
        # Should not include source path or score
        assert "Source:" not in result or "/test.txt" not in result

    def test_estimate_tokens(self, builder) -> None:
        """Test token estimation."""
        # 4 chars ≈ 1 token
        text = "A" * 100
        tokens = builder.estimate_tokens(text)
        assert tokens == 25  # 100 / 4

    def test_estimate_tokens_empty(self, builder) -> None:
        """Test token estimation for empty string."""
        assert builder.estimate_tokens("") == 0

    def test_build_context_with_budget(self, builder) -> None:
        """Test building context with token budget."""
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="Budget test content",
                score=0.9,
            )
        ]
        result = builder.build_context_with_budget(
            chunks,
            system_prompt_tokens=100,
            query_tokens=50,
            max_output_tokens=1000,
            model_context_window=8000,
        )
        assert "Budget test content" in result

    def test_build_context_with_budget_restores_max_tokens(self, builder) -> None:
        """Test that build_context_with_budget restores original max_tokens."""
        from nexus.search.semantic import SemanticSearchResult

        original_max = builder.max_context_tokens
        chunks = [
            SemanticSearchResult(
                path="/test.txt",
                chunk_index=0,
                chunk_text="Content",
                score=0.9,
            )
        ]
        builder.build_context_with_budget(chunks)
        assert builder.max_context_tokens == original_max


class TestFormatSources:
    """Test format_sources static method."""

    def test_format_sources_empty(self) -> None:
        """Test formatting empty sources."""
        from nexus.llm.context_builder import ContextBuilder

        result = ContextBuilder.format_sources([])
        assert result == "No sources"

    def test_format_sources_single(self) -> None:
        """Test formatting single source."""
        from nexus.llm.context_builder import ContextBuilder
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/doc.txt",
                chunk_index=0,
                chunk_text="Content",
                score=0.85,
            )
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "relevance: 0.85" in result

    def test_format_sources_multiple_chunks_same_file(self) -> None:
        """Test formatting multiple chunks from same file."""
        from nexus.llm.context_builder import ContextBuilder
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/doc.txt",
                chunk_index=0,
                chunk_text="Chunk 1",
                score=0.9,
            ),
            SemanticSearchResult(
                path="/doc.txt",
                chunk_index=1,
                chunk_text="Chunk 2",
                score=0.8,
            ),
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "[2 chunks]" in result

    def test_format_sources_multiple_files(self) -> None:
        """Test formatting sources from multiple files."""
        from nexus.llm.context_builder import ContextBuilder
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/a.txt",
                chunk_index=0,
                chunk_text="Content A",
                score=0.9,
            ),
            SemanticSearchResult(
                path="/b.txt",
                chunk_index=0,
                chunk_text="Content B",
                score=0.8,
            ),
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /a.txt" in result
        assert "2. /b.txt" in result

    def test_format_sources_no_score(self) -> None:
        """Test formatting sources without scores."""
        from nexus.llm.context_builder import ContextBuilder
        from nexus.search.semantic import SemanticSearchResult

        chunks = [
            SemanticSearchResult(
                path="/doc.txt",
                chunk_index=0,
                chunk_text="Content",
                score=None,
            )
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "relevance" not in result


class TestQueryComplexityEstimation:
    """Test query complexity estimation (Issue #1021)."""

    @pytest.fixture
    def builder(self):
        """Create a context builder instance."""
        from nexus.llm.context_builder import ContextBuilder

        return ContextBuilder()

    def test_simple_query_low_complexity(self, builder) -> None:
        """Test that simple queries have low complexity scores."""
        simple_queries = [
            "What is Python?",
            "Who is Einstein?",
            "Where is Paris?",
            "Define machine learning",
        ]
        for query in simple_queries:
            score = builder.estimate_query_complexity(query)
            assert score < 0.3, f"Simple query '{query}' should have low complexity, got {score}"

    def test_complex_query_high_complexity(self, builder) -> None:
        """Test that complex queries have higher complexity scores."""
        complex_queries = [
            "How does authentication compare to authorization in web security?",
            "Explain how the relationship between microservices affects system scalability",
            "What are all the differences between REST and GraphQL APIs since 2020?",
        ]
        for query in complex_queries:
            score = builder.estimate_query_complexity(query)
            assert score > 0.4, f"Complex query '{query}' should have high complexity, got {score}"

    def test_comparison_keywords_increase_complexity(self, builder) -> None:
        """Test that comparison keywords increase complexity."""
        base_query = "Python programming"
        comparison_query = "Python vs JavaScript programming differences"

        base_score = builder.estimate_query_complexity(base_query)
        comparison_score = builder.estimate_query_complexity(comparison_query)

        assert comparison_score > base_score, "Comparison keywords should increase complexity"

    def test_temporal_keywords_increase_complexity(self, builder) -> None:
        """Test that temporal keywords increase complexity."""
        base_query = "API design patterns"
        temporal_query = "API design patterns evolution since REST"

        base_score = builder.estimate_query_complexity(base_query)
        temporal_score = builder.estimate_query_complexity(temporal_query)

        assert temporal_score > base_score, "Temporal keywords should increase complexity"

    def test_aggregation_keywords_increase_complexity(self, builder) -> None:
        """Test that aggregation keywords increase complexity."""
        base_query = "Python features"
        aggregation_query = "List all Python features overview"

        base_score = builder.estimate_query_complexity(base_query)
        aggregation_score = builder.estimate_query_complexity(aggregation_query)

        assert aggregation_score > base_score, "Aggregation keywords should increase complexity"

    def test_multihop_patterns_increase_complexity(self, builder) -> None:
        """Test that multi-hop patterns increase complexity."""
        simple_query = "database performance"
        multihop_query = "How does indexing affect database performance?"

        simple_score = builder.estimate_query_complexity(simple_query)
        multihop_score = builder.estimate_query_complexity(multihop_query)

        assert multihop_score > simple_score, "Multi-hop patterns should increase complexity"

    def test_complexity_score_clamped(self, builder) -> None:
        """Test that complexity score is always between 0 and 1."""
        queries = [
            "",  # Empty query
            "x",  # Single character
            "What is x?",  # Simple question
            # Very complex query with many indicators
            "Explain how all the differences between REST vs GraphQL affect "
            "system performance since 2020 and list the complete history",
        ]
        for query in queries:
            score = builder.estimate_query_complexity(query)
            assert 0.0 <= score <= 1.0, f"Score {score} for '{query}' should be in [0, 1]"

    def test_proper_nouns_increase_complexity(self, builder) -> None:
        """Test that multiple proper nouns increase complexity."""
        single_entity = "What is Python?"
        multiple_entities = "Compare Python Django Flask performance"

        single_score = builder.estimate_query_complexity(single_entity)
        multiple_score = builder.estimate_query_complexity(multiple_entities)

        # Multiple entities (proper nouns) should increase complexity
        assert multiple_score > single_score, "Multiple entities should increase complexity"


class TestDynamicKCalculation:
    """Test dynamic k calculation (Issue #1021)."""

    @pytest.fixture
    def builder(self):
        """Create a context builder instance with default config."""
        from nexus.llm.context_builder import ContextBuilder

        return ContextBuilder()

    def test_simple_query_returns_lower_k(self, builder) -> None:
        """Test that simple queries return k close to k_base."""
        simple_query = "What is Python?"
        k = builder.calculate_k_dynamic(simple_query, k_base=10)

        # Simple query should get k close to or below k_base
        assert k <= 12, f"Simple query should get low k, got {k}"

    def test_complex_query_returns_higher_k(self, builder) -> None:
        """Test that complex queries return higher k."""
        complex_query = "How does authentication compare to authorization in web security?"
        k = builder.calculate_k_dynamic(complex_query, k_base=10)

        # Complex query should get higher k
        assert k > 10, f"Complex query should get higher k than base, got {k}"

    def test_k_respects_k_min(self, builder) -> None:
        """Test that k never goes below k_min."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig, ContextBuilder

        config = AdaptiveRetrievalConfig(k_base=10, k_min=5, k_max=20, delta=0.5)
        builder = ContextBuilder(adaptive_config=config)

        # Even for simple query, k should not go below k_min
        simple_query = "What?"
        k = builder.calculate_k_dynamic(simple_query)

        assert k >= 5, f"k should not go below k_min, got {k}"

    def test_k_respects_k_max(self, builder) -> None:
        """Test that k never exceeds k_max."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig, ContextBuilder

        config = AdaptiveRetrievalConfig(k_base=10, k_min=3, k_max=15, delta=2.0)
        builder = ContextBuilder(adaptive_config=config)

        # Very complex query with high delta should still respect k_max
        complex_query = (
            "Explain how all the differences between REST vs GraphQL affect "
            "system performance since 2020 and list complete overview"
        )
        k = builder.calculate_k_dynamic(complex_query)

        assert k <= 15, f"k should not exceed k_max, got {k}"

    def test_disabled_adaptive_returns_k_base(self, builder) -> None:
        """Test that disabled adaptive retrieval returns k_base."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig, ContextBuilder

        config = AdaptiveRetrievalConfig(k_base=10, enabled=False)
        builder = ContextBuilder(adaptive_config=config)

        complex_query = "How does authentication compare to authorization?"
        k = builder.calculate_k_dynamic(complex_query)

        assert k == 10, f"Disabled adaptive should return k_base, got {k}"

    def test_delta_affects_scaling(self) -> None:
        """Test that delta parameter affects the scaling of k."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig, ContextBuilder

        query = "How does authentication work?"

        low_delta_config = AdaptiveRetrievalConfig(k_base=10, delta=0.1)
        high_delta_config = AdaptiveRetrievalConfig(k_base=10, delta=1.0)

        low_delta_builder = ContextBuilder(adaptive_config=low_delta_config)
        high_delta_builder = ContextBuilder(adaptive_config=high_delta_config)

        low_k = low_delta_builder.calculate_k_dynamic(query)
        high_k = high_delta_builder.calculate_k_dynamic(query)

        assert high_k >= low_k, "Higher delta should produce equal or higher k"

    def test_get_retrieval_params(self, builder) -> None:
        """Test get_retrieval_params returns correct structure."""
        query = "How does caching work?"
        params = builder.get_retrieval_params(query)

        assert "k" in params
        assert "k_base" in params
        assert "complexity_score" in params
        assert isinstance(params["k"], int)
        assert isinstance(params["complexity_score"], float)
        assert 0.0 <= params["complexity_score"] <= 1.0


class TestAdaptiveRetrievalConfig:
    """Test AdaptiveRetrievalConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig

        config = AdaptiveRetrievalConfig()

        assert config.k_base == 10
        assert config.k_min == 3
        assert config.k_max == 20
        assert config.delta == 0.5
        assert config.enabled is True

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        from nexus.llm.context_builder import AdaptiveRetrievalConfig

        config = AdaptiveRetrievalConfig(
            k_base=15,
            k_min=5,
            k_max=30,
            delta=0.8,
            enabled=False,
        )

        assert config.k_base == 15
        assert config.k_min == 5
        assert config.k_max == 30
        assert config.delta == 0.8
        assert config.enabled is False
