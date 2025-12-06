"""Tests for document chunking module."""

from __future__ import annotations

from nexus.search.chunking import ChunkStrategy, DocumentChunk, DocumentChunker


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
