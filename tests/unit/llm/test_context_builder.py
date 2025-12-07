"""Tests for ContextBuilder.

These tests verify the context building functionality for LLM prompts.
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
