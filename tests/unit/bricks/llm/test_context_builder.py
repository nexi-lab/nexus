"""Tests for ContextBuilder.

These tests verify the context building functionality for LLM prompts,
including adaptive retrieval depth based on query complexity (Issue #1021).
"""

from dataclasses import dataclass

import pytest

from nexus.bricks.llm.llm_context_builder import AdaptiveRetrievalConfig, ContextBuilder


@dataclass
class MockSearchResult:
    """Mock search result for testing."""

    path: str
    chunk_index: int
    chunk_text: str
    score: float | None
    start_offset: int | None = None
    end_offset: int | None = None


class TestContextBuilder:
    """Test ContextBuilder functionality."""

    @pytest.fixture()
    def builder(self) -> ContextBuilder:
        """Create a context builder instance."""
        return ContextBuilder()

    def test_init_default(self, builder: ContextBuilder) -> None:
        """Test default initialization."""
        assert builder.max_context_tokens == 3000
        assert builder.adaptive_config.k_base == 10
        assert builder.adaptive_config.enabled is True

    def test_init_custom_tokens(self) -> None:
        """Test custom token limit."""
        builder = ContextBuilder(max_context_tokens=5000)
        assert builder.max_context_tokens == 5000

    def test_build_context_empty(self, builder: ContextBuilder) -> None:
        """Test building context with empty chunks."""
        result = builder.build_context([])
        assert result == ""

    def test_build_context_single_chunk(self, builder: ContextBuilder) -> None:
        """Test building context with single chunk."""
        chunks = [
            MockSearchResult(
                path="/test.txt", chunk_index=0, chunk_text="This is test content.", score=0.95
            )
        ]
        result = builder.build_context(chunks)
        assert "Source: /test.txt" in result
        assert "This is test content." in result
        assert "0.95" in result

    def test_build_context_multiple_chunks(self, builder: ContextBuilder) -> None:
        """Test building context with multiple chunks."""
        chunks = [
            MockSearchResult(path="/a.txt", chunk_index=0, chunk_text="Content A", score=0.9),
            MockSearchResult(path="/b.txt", chunk_index=1, chunk_text="Content B", score=0.8),
        ]
        result = builder.build_context(chunks)
        assert "Content A" in result
        assert "Content B" in result

    def test_build_context_no_metadata(self, builder: ContextBuilder) -> None:
        """Test building context without metadata."""
        chunks = [
            MockSearchResult(path="/test.txt", chunk_index=0, chunk_text="Test content", score=0.9)
        ]
        result = builder.build_context(chunks, include_metadata=False)
        assert "Source:" not in result
        assert "Test content" in result

    def test_build_context_no_scores(self, builder: ContextBuilder) -> None:
        """Test building context without scores."""
        chunks = [
            MockSearchResult(path="/test.txt", chunk_index=0, chunk_text="Test content", score=0.9)
        ]
        result = builder.build_context(chunks, include_scores=False)
        assert "Relevance:" not in result
        assert "Test content" in result

    def test_build_context_respects_token_limit(self) -> None:
        """Test that context builder respects token limit."""
        builder = ContextBuilder(max_context_tokens=100)
        chunks = [
            MockSearchResult(path="/a.txt", chunk_index=0, chunk_text="A" * 200, score=0.9),
            MockSearchResult(path="/b.txt", chunk_index=0, chunk_text="B" * 200, score=0.8),
            MockSearchResult(path="/c.txt", chunk_index=0, chunk_text="C" * 200, score=0.7),
        ]
        result = builder.build_context(chunks)
        # Should not include all three chunks due to token limit
        assert "C" * 200 not in result

    def test_build_simple_context(self, builder: ContextBuilder) -> None:
        """Test building simple context without metadata."""
        chunks = [
            MockSearchResult(
                path="/test.txt", chunk_index=0, chunk_text="Simple content", score=0.9
            )
        ]
        result = builder.build_simple_context(chunks)
        assert "Source:" not in result
        assert "Simple content" in result

    def test_estimate_tokens(self, builder: ContextBuilder) -> None:
        """Test token estimation."""
        # 100 chars / 4 = 25 tokens
        tokens = builder.estimate_tokens("A" * 100)
        assert tokens == 25

    def test_estimate_tokens_empty(self, builder: ContextBuilder) -> None:
        """Test token estimation for empty string."""
        assert builder.estimate_tokens("") == 0

    def test_build_context_with_budget(self) -> None:
        """Test building context with token budget."""
        builder = ContextBuilder(max_context_tokens=3000)
        chunks = [
            MockSearchResult(
                path="/test.txt", chunk_index=0, chunk_text="Budget test content", score=0.9
            )
        ]
        result = builder.build_context_with_budget(chunks)
        assert "Budget test content" in result

    def test_build_context_with_budget_restores_max_tokens(self) -> None:
        """Test that build_context_with_budget restores original max_tokens."""
        builder = ContextBuilder(max_context_tokens=3000)
        original_max = builder.max_context_tokens
        chunks = [
            MockSearchResult(path="/test.txt", chunk_index=0, chunk_text="Content", score=0.9)
        ]
        builder.build_context_with_budget(chunks, model_context_window=8000)
        assert builder.max_context_tokens == original_max


class TestFormatSources:
    """Test format_sources static method."""

    def test_format_sources_empty(self) -> None:
        """Test formatting empty sources."""
        result = ContextBuilder.format_sources([])
        assert result == "No sources"

    def test_format_sources_single(self) -> None:
        """Test formatting single source."""
        chunks = [
            MockSearchResult(path="/doc.txt", chunk_index=0, chunk_text="Content", score=0.85)
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "relevance: 0.85" in result

    def test_format_sources_multiple_chunks_same_file(self) -> None:
        """Test formatting multiple chunks from same file."""
        chunks = [
            MockSearchResult(path="/doc.txt", chunk_index=0, chunk_text="Chunk 1", score=0.9),
            MockSearchResult(path="/doc.txt", chunk_index=1, chunk_text="Chunk 2", score=0.8),
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "[2 chunks]" in result

    def test_format_sources_multiple_files(self) -> None:
        """Test formatting sources from multiple files."""
        chunks = [
            MockSearchResult(path="/a.txt", chunk_index=0, chunk_text="Content A", score=0.9),
            MockSearchResult(path="/b.txt", chunk_index=0, chunk_text="Content B", score=0.8),
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /a.txt" in result
        assert "2. /b.txt" in result

    def test_format_sources_no_score(self) -> None:
        """Test formatting sources without scores."""
        chunks = [
            MockSearchResult(path="/doc.txt", chunk_index=0, chunk_text="Content", score=None)
        ]
        result = ContextBuilder.format_sources(chunks)
        assert "1. /doc.txt" in result
        assert "relevance" not in result


class TestQueryComplexityEstimation:
    """Test query complexity estimation (Issue #1021)."""

    @pytest.fixture()
    def builder(self) -> ContextBuilder:
        """Create a context builder instance."""
        return ContextBuilder()

    @pytest.mark.parametrize(
        "query",
        [
            "What is Python?",
            "Define REST",
            "Where is the config?",
        ],
    )
    def test_simple_query_low_complexity(self, builder: ContextBuilder, query: str) -> None:
        """Test that simple queries have low complexity scores."""
        score = builder.estimate_query_complexity(query)
        assert score < 0.4, f"Simple query '{query}' should have low complexity, got {score}"

    @pytest.mark.parametrize(
        "query",
        [
            "How does authentication compare to authorization in web security?",
            "Explain all the differences between REST vs GraphQL since 2020",
            "List complete overview of how database indexing affects performance",
        ],
    )
    def test_complex_query_high_complexity(self, builder: ContextBuilder, query: str) -> None:
        """Test that complex queries have higher complexity scores."""
        score = builder.estimate_query_complexity(query)
        assert score > 0.3, f"Complex query '{query}' should have high complexity, got {score}"

    def test_comparison_keywords_increase_complexity(self, builder: ContextBuilder) -> None:
        """Test that comparison keywords increase complexity."""
        base_score = builder.estimate_query_complexity("Python programming")
        comparison_score = builder.estimate_query_complexity(
            "Python vs JavaScript programming differences"
        )
        assert comparison_score > base_score, "Comparison keywords should increase complexity"

    def test_temporal_keywords_increase_complexity(self, builder: ContextBuilder) -> None:
        """Test that temporal keywords increase complexity."""
        base_score = builder.estimate_query_complexity("API design patterns")
        temporal_score = builder.estimate_query_complexity(
            "API design patterns evolution since REST"
        )
        assert temporal_score > base_score, "Temporal keywords should increase complexity"

    def test_aggregation_keywords_increase_complexity(self, builder: ContextBuilder) -> None:
        """Test that aggregation keywords increase complexity."""
        base_score = builder.estimate_query_complexity("Python features")
        aggregation_score = builder.estimate_query_complexity("List all Python features overview")
        assert aggregation_score > base_score, "Aggregation keywords should increase complexity"

    def test_multihop_patterns_increase_complexity(self, builder: ContextBuilder) -> None:
        """Test that multi-hop patterns increase complexity."""
        simple_score = builder.estimate_query_complexity("database performance")
        multihop_score = builder.estimate_query_complexity(
            "How does indexing affect database performance?"
        )
        assert multihop_score > simple_score, "Multi-hop patterns should increase complexity"

    @pytest.mark.parametrize(
        "query",
        [
            "",
            "x",
            "What is Python?",
            "How does authentication compare to authorization in web security since 2020?",
            "List complete comprehensive overview of all differences between REST vs GraphQL",
        ],
    )
    def test_complexity_score_clamped(self, builder: ContextBuilder, query: str) -> None:
        """Test that complexity score is always between 0 and 1."""
        score = builder.estimate_query_complexity(query)
        assert 0.0 <= score <= 1.0, f"Score {score} for '{query}' should be in [0, 1]"

    def test_proper_nouns_increase_complexity(self, builder: ContextBuilder) -> None:
        """Test that multiple proper nouns increase complexity."""
        single_score = builder.estimate_query_complexity("What is Python?")
        multiple_score = builder.estimate_query_complexity(
            "Compare Python Django Flask performance"
        )
        assert multiple_score > single_score, "Multiple entities should increase complexity"


class TestDynamicKCalculation:
    """Test dynamic k calculation (Issue #1021)."""

    @pytest.fixture()
    def builder(self) -> ContextBuilder:
        """Create a context builder instance with default config."""
        return ContextBuilder()

    def test_simple_query_returns_lower_k(self, builder: ContextBuilder) -> None:
        """Test that simple queries return k close to k_base."""
        k = builder.calculate_k_dynamic("What is Python?")
        assert k <= 15, f"Simple query should get low k, got {k}"

    def test_complex_query_returns_higher_k(self, builder: ContextBuilder) -> None:
        """Test that complex queries return higher k."""
        k = builder.calculate_k_dynamic(
            "How does authentication compare to authorization in web security?"
        )
        assert k > 10, f"Complex query should get higher k than base, got {k}"

    def test_k_respects_k_min(self) -> None:
        """Test that k never goes below k_min."""
        config = AdaptiveRetrievalConfig(k_base=1, k_min=5, k_max=20)
        builder = ContextBuilder(adaptive_config=config)
        k = builder.calculate_k_dynamic("What?")
        assert k >= 5, f"k should not go below k_min, got {k}"

    def test_k_respects_k_max(self) -> None:
        """Test that k never exceeds k_max."""
        config = AdaptiveRetrievalConfig(k_base=10, k_min=3, k_max=15)
        builder = ContextBuilder(adaptive_config=config)
        k = builder.calculate_k_dynamic(
            "Explain how all the differences between REST vs GraphQL "
            "affect system performance since 2020 and list complete overview"
        )
        assert k <= 15, f"k should not exceed k_max, got {k}"

    def test_disabled_adaptive_returns_k_base(self) -> None:
        """Test that disabled adaptive retrieval returns k_base."""
        config = AdaptiveRetrievalConfig(k_base=10, enabled=False)
        builder = ContextBuilder(adaptive_config=config)
        k = builder.calculate_k_dynamic("How does authentication compare to authorization?")
        assert k == 10, f"Disabled adaptive should return k_base, got {k}"

    def test_delta_affects_scaling(self, builder: ContextBuilder) -> None:
        """Test that delta parameter affects the scaling of k."""
        query = "How does authentication work?"
        high_k = builder.calculate_k_dynamic(query, delta=2.0)
        low_k = builder.calculate_k_dynamic(query, delta=0.1)
        assert high_k >= low_k, "Higher delta should produce equal or higher k"

    def test_get_retrieval_params(self, builder: ContextBuilder) -> None:
        """Test get_retrieval_params returns correct structure."""
        params = builder.get_retrieval_params("How does caching work?")
        assert "k" in params
        assert "k_base" in params
        assert "complexity_score" in params
        assert params["k_base"] == 10


class TestContextBuilderSatisfiesAdaptiveKProtocol:
    """AdaptiveKProtocol conformance for ContextBuilder (moved from bricks test)."""

    def test_context_builder_satisfies_protocol(self) -> None:
        from nexus.services.protocols.adaptive_k import AdaptiveKProtocol

        builder = ContextBuilder()
        assert isinstance(builder, AdaptiveKProtocol)
