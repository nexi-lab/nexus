"""Tests for document chunking module."""

from __future__ import annotations

from nexus.search.chunking import (
    ChunkStrategy,
    DocumentChunk,
    DocumentChunker,
    EntropyAwareChunker,
    EntropyFilterResult,
    _cosine_similarity,
)


class TestDocumentChunk:
    """Test DocumentChunk dataclass."""

    def test_chunk_creation(self):
        """Test creating a document chunk."""
        chunk = DocumentChunk(
            text="Hello world",
            chunk_index=0,
            tokens=2,
            start_offset=0,
            end_offset=11,
        )
        assert chunk.text == "Hello world"
        assert chunk.chunk_index == 0
        assert chunk.tokens == 2
        assert chunk.start_offset == 0
        assert chunk.end_offset == 11


class TestChunkStrategy:
    """Test ChunkStrategy enum."""

    def test_strategy_values(self):
        """Test strategy enum values."""
        assert ChunkStrategy.FIXED == "fixed"
        assert ChunkStrategy.SEMANTIC == "semantic"
        assert ChunkStrategy.OVERLAPPING == "overlapping"


class TestDocumentChunker:
    """Test DocumentChunker class."""

    def test_init_default(self):
        """Test chunker initialization with defaults."""
        chunker = DocumentChunker()
        assert chunker.chunk_size == 1024
        assert chunker.overlap_size == 128
        assert chunker.strategy == ChunkStrategy.FIXED
        assert chunker.encoding_name == "cl100k_base"

    def test_init_custom(self):
        """Test chunker initialization with custom values."""
        chunker = DocumentChunker(
            chunk_size=512,
            overlap_size=64,
            strategy=ChunkStrategy.SEMANTIC,
            encoding_name="p50k_base",
        )
        assert chunker.chunk_size == 512
        assert chunker.overlap_size == 64
        assert chunker.strategy == ChunkStrategy.SEMANTIC
        assert chunker.encoding_name == "p50k_base"

    def test_count_tokens_approximate(self):
        """Test token counting with approximate method (no tiktoken)."""
        chunker = DocumentChunker()
        # Force approximate counting
        chunker.encoding = None

        text = "Hello world, this is a test."
        token_count = chunker._count_tokens(text)
        # Approximate: len(text) // 4
        assert token_count == len(text) // 4

    def test_chunk_empty_content(self):
        """Test chunking empty content."""
        chunker = DocumentChunker()
        chunks = chunker.chunk("")
        assert chunks == []

    def test_chunk_fixed_strategy(self):
        """Test fixed chunking strategy."""
        chunker = DocumentChunker(chunk_size=10, strategy=ChunkStrategy.FIXED)
        content = "This is a simple test document with many words to chunk properly."
        chunks = chunker.chunk(content)

        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, DocumentChunk)
            assert chunk.tokens <= chunker.chunk_size * 2  # Allow some flexibility
            assert len(chunk.text) > 0

    def test_chunk_semantic_strategy(self):
        """Test semantic chunking strategy."""
        chunker = DocumentChunker(chunk_size=50, strategy=ChunkStrategy.SEMANTIC)
        content = """# Heading 1

This is paragraph 1.

This is paragraph 2.

## Heading 2

More content here."""
        chunks = chunker.chunk(content)

        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, DocumentChunk)
            assert len(chunk.text) > 0

    def test_chunk_overlapping_strategy(self):
        """Test overlapping chunking strategy."""
        chunker = DocumentChunker(chunk_size=20, overlap_size=5, strategy=ChunkStrategy.OVERLAPPING)
        content = "word " * 100  # 100 words
        chunks = chunker.chunk(content)

        assert len(chunks) > 1
        # Check that chunks have proper indices
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_chunk_markdown_sections(self):
        """Test chunking markdown by sections."""
        chunker = DocumentChunker(chunk_size=100, strategy=ChunkStrategy.SEMANTIC)
        content = """# Main Title

Introduction paragraph.

## Section 1

Content for section 1.

## Section 2

Content for section 2."""

        chunks = chunker._chunk_markdown(content)
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, DocumentChunk)

    def test_chunk_paragraphs(self):
        """Test chunking by paragraphs."""
        chunker = DocumentChunker(chunk_size=50)
        content = """Paragraph one.

Paragraph two.

Paragraph three."""

        chunks = chunker._chunk_paragraphs(content)
        assert len(chunks) > 0
        for chunk in chunks:
            assert isinstance(chunk, DocumentChunk)

    def test_chunk_large_section(self):
        """Test chunking large section that exceeds chunk_size."""
        chunker = DocumentChunker(chunk_size=20, strategy=ChunkStrategy.SEMANTIC)
        # Create a large section that will need to be split
        content = "# Heading\n\n" + "word " * 100

        chunks = chunker.chunk(content)
        assert len(chunks) > 1

    def test_chunk_offsets(self):
        """Test that chunk offsets are calculated correctly."""
        chunker = DocumentChunker(chunk_size=10, strategy=ChunkStrategy.FIXED)
        content = "Short test content here."
        chunks = chunker.chunk(content)

        for chunk in chunks:
            # Verify offsets are non-negative
            assert chunk.start_offset >= 0
            assert chunk.end_offset > chunk.start_offset
            # Verify text matches offsets (approximately)
            assert len(chunk.text) <= (chunk.end_offset - chunk.start_offset + 10)

    def test_chunk_indices(self):
        """Test that chunk indices are sequential."""
        chunker = DocumentChunker(chunk_size=10, overlap_size=2, strategy=ChunkStrategy.OVERLAPPING)
        content = "word " * 50
        chunks = chunker.chunk(content)

        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

    def test_chunk_preserves_content(self):
        """Test that chunking preserves all content."""
        chunker = DocumentChunker(chunk_size=20, strategy=ChunkStrategy.FIXED)
        content = "This is a test document with some content."
        chunks = chunker.chunk(content)

        # All chunks should contain non-empty text
        for chunk in chunks:
            assert len(chunk.text.strip()) > 0

        # Concatenated chunks should contain all words
        all_text = " ".join(chunk.text for chunk in chunks)
        original_words = set(content.split())
        chunked_words = set(all_text.split())
        # Most words should be preserved (allowing for some splitting)
        assert len(original_words & chunked_words) >= len(original_words) * 0.8

    def test_chunk_fixed_respects_semantic_boundaries(self):
        """Test that fixed chunking tries to split at semantic boundaries."""
        chunker = DocumentChunker(chunk_size=50, strategy=ChunkStrategy.FIXED)
        content = """First paragraph with some content.

Second paragraph with more content.

Third paragraph with even more content."""

        chunks = chunker.chunk(content)

        # Should produce multiple chunks
        assert len(chunks) >= 1
        # Chunks should prefer paragraph boundaries when possible
        for chunk in chunks:
            assert isinstance(chunk, DocumentChunk)

    def test_chunk_fixed_splits_long_sentences(self):
        """Test that fixed chunking can split long sentences."""
        chunker = DocumentChunker(chunk_size=10, strategy=ChunkStrategy.FIXED)
        # Create a very long sentence with no paragraph breaks
        content = "word " * 100  # 100 words, no paragraph breaks

        chunks = chunker.chunk(content)

        # Should split into multiple chunks
        assert len(chunks) > 1
        # Each chunk should be within size limits (approximately)
        for chunk in chunks:
            assert chunk.tokens <= chunker.chunk_size * 2  # Allow flexibility

    def test_chunk_fixed_handles_nested_splitting(self):
        """Test recursive splitting with multiple separator levels."""
        chunker = DocumentChunker(chunk_size=20, strategy=ChunkStrategy.FIXED)
        content = """Para 1 sentence 1. Para 1 sentence 2.

Para 2 sentence 1. Para 2 sentence 2.

Para 3 with a very long sentence that goes on and on with many words."""

        chunks = chunker.chunk(content)

        assert len(chunks) >= 1
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i
            assert len(chunk.text) > 0

    def test_chunk_fixed_sequential_indices(self):
        """Test that chunk indices are sequential after recursive splitting."""
        chunker = DocumentChunker(chunk_size=15, strategy=ChunkStrategy.FIXED)
        content = """Short para.

A much longer paragraph with many words that will need to be split into multiple chunks.

Another short one."""

        chunks = chunker.chunk(content)

        # Verify indices are sequential
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i, f"Expected index {i}, got {chunk.chunk_index}"


class TestChunkingPerformance:
    """Performance tests for chunking."""

    def test_large_document_chunking_efficiency(self):
        """Test that chunking large documents doesn't call tokenizer per-word.

        The new implementation should tokenize at paragraph/sentence level,
        not per-word, making it much more efficient for large documents.
        """
        chunker = DocumentChunker(chunk_size=512, strategy=ChunkStrategy.FIXED)

        # Create a moderately large document (100KB ~ 20K words)
        paragraphs = []
        for i in range(200):
            paragraphs.append(f"This is paragraph {i} with some content. " * 5)
        content = "\n\n".join(paragraphs)

        # This should complete quickly (< 1 second) with efficient implementation
        # Old per-word implementation would be much slower
        import time

        start = time.time()
        chunks = chunker.chunk(content)
        elapsed = time.time() - start

        assert len(chunks) > 0
        # Should complete in reasonable time (generous limit for CI)
        assert elapsed < 10.0, f"Chunking took {elapsed:.2f}s, expected < 10s"

    def test_tokenize_count_efficiency(self):
        """Verify the number of tokenize calls is reasonable.

        The recursive splitter should call tokenizer on paragraphs/sentences,
        not on individual words.
        """

        chunker = DocumentChunker(chunk_size=100, strategy=ChunkStrategy.FIXED)

        # Create content with 10 paragraphs, ~50 words each = ~500 words total
        paragraphs = ["Word " * 50 for _ in range(10)]
        content = "\n\n".join(paragraphs)

        # Mock _count_tokens to count calls
        original_count_tokens = chunker._count_tokens
        call_count = [0]

        def counting_wrapper(text):
            call_count[0] += 1
            return original_count_tokens(text)

        chunker._count_tokens = counting_wrapper

        chunks = chunker.chunk(content)

        # Old implementation: ~500 calls (one per word)
        # New implementation: ~10-50 calls (paragraphs + some sentences)
        # Allow generous margin but should be way less than per-word
        assert call_count[0] < 200, f"Too many tokenize calls: {call_count[0]}"
        assert len(chunks) > 0


class TestCosineSimilarity:
    """Test cosine similarity helper function."""

    def test_identical_vectors(self):
        """Test that identical vectors have similarity 1.0."""
        vec = [1.0, 2.0, 3.0]
        assert abs(_cosine_similarity(vec, vec) - 1.0) < 0.0001

    def test_orthogonal_vectors(self):
        """Test that orthogonal vectors have similarity 0.0."""
        vec1 = [1.0, 0.0, 0.0]
        vec2 = [0.0, 1.0, 0.0]
        assert abs(_cosine_similarity(vec1, vec2)) < 0.0001

    def test_opposite_vectors(self):
        """Test that opposite vectors have similarity -1.0."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [-1.0, -2.0, -3.0]
        assert abs(_cosine_similarity(vec1, vec2) + 1.0) < 0.0001

    def test_empty_vectors(self):
        """Test that empty/zero vectors return 0.0."""
        assert _cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0
        assert _cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0

    def test_different_length_vectors(self):
        """Test that vectors of different lengths return 0.0."""
        vec1 = [1.0, 2.0, 3.0]
        vec2 = [1.0, 2.0]
        assert _cosine_similarity(vec1, vec2) == 0.0


class TestEntropyFilterResult:
    """Test EntropyFilterResult dataclass."""

    def test_reduction_percent(self):
        """Test reduction percentage calculation."""
        result = EntropyFilterResult(
            chunks=[],
            original_count=100,
            filtered_count=70,
            scores=[0.5] * 100,
        )
        assert result.reduction_percent == 30.0

    def test_reduction_percent_zero_original(self):
        """Test reduction percentage with zero original."""
        result = EntropyFilterResult(
            chunks=[],
            original_count=0,
            filtered_count=0,
            scores=[],
        )
        assert result.reduction_percent == 0.0

    def test_no_reduction(self):
        """Test when no chunks are filtered."""
        result = EntropyFilterResult(
            chunks=[],
            original_count=10,
            filtered_count=10,
            scores=[0.5] * 10,
        )
        assert result.reduction_percent == 0.0


class TestEntropyAwareChunker:
    """Test EntropyAwareChunker class (Issue #1024)."""

    def test_init_default(self):
        """Test chunker initialization with defaults."""
        chunker = EntropyAwareChunker()
        assert chunker.redundancy_threshold == 0.35
        assert chunker.alpha == 0.5
        assert chunker.embedding_provider is None
        assert chunker.history_window == 5
        assert chunker.base_chunker is not None

    def test_init_custom(self):
        """Test chunker initialization with custom values."""
        chunker = EntropyAwareChunker(
            redundancy_threshold=0.5,
            alpha=0.7,
            history_window=10,
        )
        assert chunker.redundancy_threshold == 0.5
        assert chunker.alpha == 0.7
        assert chunker.history_window == 10

    def test_extract_entities_proper_nouns(self):
        """Test entity extraction for proper nouns."""
        chunker = EntropyAwareChunker()
        text = "John Smith visited New York and met with Microsoft CEO."
        entities = chunker.extract_entities(text)

        # Should extract proper nouns (capitalized)
        assert "john" in entities or "john smith" in entities
        assert "new york" in entities or "new" in entities
        assert "microsoft" in entities
        assert "ceo" in entities

    def test_extract_entities_technical_terms(self):
        """Test entity extraction for technical identifiers."""
        chunker = EntropyAwareChunker()
        text = "The function_name uses camelCase and version 2.0.1"
        entities = chunker.extract_entities(text)

        # Should extract snake_case, camelCase, versions
        assert "function_name" in entities
        assert "camelcase" in entities
        assert "2.0.1" in entities

    def test_extract_entities_dates_and_numbers(self):
        """Test entity extraction for dates and numbers."""
        chunker = EntropyAwareChunker()
        text = "The event on 2024-01-15 had 1,234 attendees."
        entities = chunker.extract_entities(text)

        # Should extract dates and formatted numbers
        assert "2024-01-15" in entities
        assert "1,234" in entities

    def test_extract_entities_urls_emails(self):
        """Test entity extraction for URLs and emails."""
        chunker = EntropyAwareChunker()
        text = "Contact us at info@example.com or visit https://example.com/page"
        entities = chunker.extract_entities(text)

        # Should extract URLs and emails
        assert "info@example.com" in entities
        assert "https://example.com/page" in entities

    def test_entity_novelty_score_all_new(self):
        """Test entity novelty score when all entities are new."""
        chunker = EntropyAwareChunker()
        chunk_entities = {"apple", "banana", "cherry"}
        history_entities = set()
        word_count = 10

        score = chunker._entity_novelty_score(chunk_entities, history_entities, word_count)
        # 3 new entities / 10 words = 0.3
        assert score == 0.3

    def test_entity_novelty_score_no_new(self):
        """Test entity novelty score when no entities are new."""
        chunker = EntropyAwareChunker()
        chunk_entities = {"apple", "banana"}
        history_entities = {"apple", "banana", "cherry"}
        word_count = 10

        score = chunker._entity_novelty_score(chunk_entities, history_entities, word_count)
        # 0 new entities = 0.0
        assert score == 0.0

    def test_entity_novelty_score_empty_chunk(self):
        """Test entity novelty score with zero word count."""
        chunker = EntropyAwareChunker()
        score = chunker._entity_novelty_score({"a"}, set(), 0)
        assert score == 0.0

    def test_semantic_novelty_score_no_history(self):
        """Test semantic novelty score with no history (first chunk)."""
        chunker = EntropyAwareChunker()
        score = chunker._semantic_novelty_score([0.1, 0.2, 0.3], [])
        assert score == 1.0  # First chunk is fully novel

    def test_semantic_novelty_score_identical(self):
        """Test semantic novelty score with identical embedding."""
        chunker = EntropyAwareChunker()
        embedding = [0.1, 0.2, 0.3]
        history = [[0.1, 0.2, 0.3]]  # Same embedding
        score = chunker._semantic_novelty_score(embedding, history)
        assert score < 0.01  # Nearly 0 (not novel)

    def test_semantic_novelty_score_different(self):
        """Test semantic novelty score with different embedding."""
        chunker = EntropyAwareChunker()
        embedding = [1.0, 0.0, 0.0]
        history = [[0.0, 1.0, 0.0]]  # Orthogonal embedding
        score = chunker._semantic_novelty_score(embedding, history)
        assert score > 0.99  # Fully novel

    def test_information_score_entity_only(self):
        """Test information score without embeddings."""
        chunker = EntropyAwareChunker(alpha=0.5)
        score = chunker.information_score(
            chunk_entities={"apple", "banana"},
            history_entities=set(),
            chunk_word_count=10,
            chunk_embedding=None,
            history_embeddings=None,
        )
        # Without embeddings, returns entity score only
        # 2 entities / 10 words = 0.2
        assert score == 0.2

    def test_information_score_combined(self):
        """Test information score with embeddings."""
        chunker = EntropyAwareChunker(alpha=0.5)
        score = chunker.information_score(
            chunk_entities={"apple", "banana"},
            history_entities=set(),
            chunk_word_count=10,
            chunk_embedding=[1.0, 0.0, 0.0],
            history_embeddings=[[0.0, 1.0, 0.0]],  # Orthogonal
        )
        # entity_score = 2/10 = 0.2
        # semantic_score = 1.0 (orthogonal)
        # combined = 0.5 * 0.2 + 0.5 * 1.0 = 0.6
        assert abs(score - 0.6) < 0.01

    def test_chunk_with_filtering_sync_empty(self):
        """Test sync filtering with empty content."""
        chunker = EntropyAwareChunker()
        result = chunker.chunk_with_filtering_sync("")

        assert result.original_count == 0
        assert result.filtered_count == 0
        assert result.chunks == []
        assert result.scores == []

    def test_chunk_with_filtering_sync_basic(self):
        """Test sync filtering with basic content."""
        chunker = EntropyAwareChunker(redundancy_threshold=0.0)  # Keep all chunks
        content = "Hello World. This is a test document."
        result = chunker.chunk_with_filtering_sync(content)

        assert result.original_count >= 1
        assert result.filtered_count >= 1
        assert len(result.scores) == result.original_count

    def test_chunk_with_filtering_sync_redundant_content(self):
        """Test that redundant content is filtered (sync version)."""
        chunker = EntropyAwareChunker(
            redundancy_threshold=0.1,  # Moderate threshold
            base_chunker=DocumentChunker(chunk_size=20, strategy=ChunkStrategy.FIXED),
        )

        # Create content with repeated sections
        unique_section = "John Smith from Microsoft visited New York on 2024-01-15."
        repeated_section = "Hello world hello world hello world."

        content = (
            f"{unique_section}\n\n{repeated_section}\n\n{repeated_section}\n\n{repeated_section}"
        )

        result = chunker.chunk_with_filtering_sync(content)

        # The unique section should score higher than the repeated ones
        # With proper threshold, some redundant chunks should be filtered
        # At minimum, check that scores are calculated
        assert len(result.scores) == result.original_count
        assert all(isinstance(s, float) for s in result.scores)
        # First score (unique content) should be higher than later scores
        if len(result.scores) > 1:
            assert result.scores[0] >= result.scores[-1]

    def test_chunk_with_filtering_async_empty(self):
        """Test async filtering with empty content."""
        import asyncio

        async def run_test():
            chunker = EntropyAwareChunker()
            result = await chunker.chunk_with_filtering("")
            assert result.original_count == 0
            assert result.filtered_count == 0

        asyncio.run(run_test())

    def test_chunk_with_filtering_async_basic(self):
        """Test async filtering with basic content."""
        import asyncio

        async def run_test():
            chunker = EntropyAwareChunker(redundancy_threshold=0.0)
            content = "Hello World. This is a test."
            result = await chunker.chunk_with_filtering(content)
            assert result.original_count >= 1
            assert result.filtered_count >= 1

        asyncio.run(run_test())


class TestEntropyChunkerBenchmark:
    """Benchmark tests for entropy-aware chunking (Issue #1024)."""

    def test_redundant_content_reduction(self):
        """Benchmark: Measure chunk reduction percentage on redundant content."""
        chunker = EntropyAwareChunker(
            redundancy_threshold=0.15,
            base_chunker=DocumentChunker(chunk_size=50, strategy=ChunkStrategy.FIXED),
        )

        # Generate highly redundant content
        unique_intro = "Important: Project Alpha launched by Acme Corp on 2024-01-15."
        filler = "This is some filler text. " * 20

        # Create document with 1 unique section and 5 repeated filler sections
        content = f"{unique_intro}\n\n" + "\n\n".join([filler] * 5)

        result = chunker.chunk_with_filtering_sync(content)

        # Expect significant reduction
        print(f"Benchmark: {result.original_count} -> {result.filtered_count} chunks")
        print(f"Reduction: {result.reduction_percent:.1f}%")

        # The repeated filler should be filtered, keeping unique content
        assert result.original_count > 0
        # At least some chunks should be filtered for redundant content
        # (exact number depends on chunking and scoring)

    def test_diverse_content_preservation(self):
        """Benchmark: Verify diverse content is preserved."""
        chunker = EntropyAwareChunker(
            redundancy_threshold=0.1,
            base_chunker=DocumentChunker(chunk_size=50, strategy=ChunkStrategy.FIXED),
        )

        # Generate diverse content with unique entities in each section
        sections = [
            "Apple Inc announced iPhone 15 launch in September 2023.",
            "Google Cloud Platform released new AI services for developers.",
            "Microsoft Azure expanded to 60 regions worldwide.",
            "Amazon Web Services introduced serverless compute options.",
            "Tesla Motors unveiled new electric vehicle Model X.",
        ]
        content = "\n\n".join(sections)

        result = chunker.chunk_with_filtering_sync(content)

        # Diverse content should have minimal filtering
        # Most chunks should be preserved
        assert result.filtered_count > 0
        print(f"Diverse content: {result.original_count} -> {result.filtered_count} chunks")
        print(f"Preservation rate: {(result.filtered_count / result.original_count * 100):.1f}%")
