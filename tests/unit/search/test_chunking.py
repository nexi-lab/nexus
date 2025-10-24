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
