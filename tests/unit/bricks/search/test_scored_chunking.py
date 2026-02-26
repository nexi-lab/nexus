"""Unit tests for ScoredBreakPointChunker (QMD-inspired scored break-point chunking).

Tests cover:
- BREAK_SCORES constant values
- Code fence protection (no splits inside code blocks)
- Basic markdown chunking at headings
- Overlap generation between adjacent chunks
- Empty/tiny document handling
"""

from __future__ import annotations

from nexus.bricks.search.chunking import BREAK_SCORES, ScoredBreakPointChunker

# =============================================================================
# BREAK_SCORES constant
# =============================================================================


class TestBreakPointScores:
    """Test the BREAK_SCORES constant values."""

    def test_break_point_scores(self) -> None:
        """H1=100, H2=90, H3=80, code_fence=80, H4=70, hr=60, paragraph=20, list_item=5, newline=1."""
        assert BREAK_SCORES["h1"] == 100
        assert BREAK_SCORES["h2"] == 90
        assert BREAK_SCORES["h3"] == 80
        assert BREAK_SCORES["code_fence"] == 80
        assert BREAK_SCORES["h4"] == 70
        assert BREAK_SCORES["hr"] == 60
        assert BREAK_SCORES["paragraph"] == 20
        assert BREAK_SCORES["list_item"] == 5
        assert BREAK_SCORES["newline"] == 1


# =============================================================================
# Code fence protection
# =============================================================================


class TestCodeFenceProtection:
    """Test that chunks do not split inside code blocks."""

    def test_code_fence_protection(self) -> None:
        """Chunks should not split inside fenced code blocks."""
        # Build a document with a large code block that the chunker might
        # want to split in the middle. Use a small chunk_size to force splitting.
        preamble = "# Introduction\n\nSome introductory text here.\n\n"
        code_block = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(80)) + "\n```\n\n"
        postamble = "# Conclusion\n\nSome concluding text.\n"

        doc = preamble + code_block + postamble

        chunker = ScoredBreakPointChunker(
            chunk_size=50,  # Very small to force splitting
        )
        chunks = chunker.chunk(doc)

        # Verify no chunk text contains an unmatched code fence
        # (i.e., a ``` open without a close, or vice versa)
        for chunk in chunks:
            fence_count = chunk.text.count("```")
            # Either 0 fences (not touching code) or even number (complete blocks)
            assert fence_count % 2 == 0, (
                f"Chunk {chunk.chunk_index} has {fence_count} fences (odd = split inside code): "
                f"{chunk.text[:100]}..."
            )


# =============================================================================
# Simple markdown chunking
# =============================================================================


class TestSimpleMarkdownChunking:
    """Test basic markdown document chunking."""

    def test_simple_markdown_chunking(self) -> None:
        """Basic markdown document gets chunked at heading boundaries."""
        # Create a document large enough to require multiple chunks
        sections = []
        for i in range(10):
            heading = f"## Section {i}"
            body = f"This is the body of section {i}. " * 20
            sections.append(f"{heading}\n\n{body}")

        doc = "\n\n".join(sections)

        chunker = ScoredBreakPointChunker(
            chunk_size=200,  # Medium size to get a few chunks
        )
        chunks = chunker.chunk(doc)

        # Should produce multiple chunks
        assert len(chunks) > 1

        # Chunks should be ordered by offset
        for i in range(len(chunks) - 1):
            assert chunks[i].start_offset < chunks[i + 1].start_offset

        # Chunk indices should be sequential
        for i, chunk in enumerate(chunks):
            assert chunk.chunk_index == i

        # All chunk text should be non-empty
        for chunk in chunks:
            assert chunk.text.strip()

        # Line numbers should be populated
        for chunk in chunks:
            assert chunk.line_start is not None
            assert chunk.line_end is not None
            assert chunk.line_start >= 1
            assert chunk.line_end >= chunk.line_start


# =============================================================================
# Overlap generation
# =============================================================================


class TestOverlapGeneration:
    """Test that adjacent chunks have overlapping content."""

    def test_overlap_generation(self) -> None:
        """Adjacent chunks should have overlapping content when overlap_fraction > 0."""
        # Build a large enough document to force multiple chunks
        sections = []
        for i in range(15):
            sections.append(f"## Topic {i}\n\n" + f"Content about topic {i}. " * 30)

        doc = "\n\n".join(sections)

        chunker = ScoredBreakPointChunker(
            chunk_size=150,
            overlap_fraction=0.15,
        )
        chunks = chunker.chunk(doc)

        # Need at least 2 chunks to test overlap
        assert len(chunks) >= 2

        # Check that at least some consecutive chunk pairs share content
        overlap_found = False
        for i in range(len(chunks) - 1):
            current_end = chunks[i].end_offset
            next_start = chunks[i + 1].start_offset

            # With overlap, the next chunk should start before the current one ends
            if next_start < current_end:
                overlap_found = True
                # Verify the overlapping text is identical in both chunks
                overlap_text_in_current = doc[next_start:current_end]
                assert overlap_text_in_current in chunks[i].text
                assert overlap_text_in_current in chunks[i + 1].text
                break

        assert overlap_found, "Expected at least one pair of overlapping chunks"


# =============================================================================
# Empty / tiny document
# =============================================================================


class TestScoredChunkingEdgeCases:
    """Test edge cases for ScoredBreakPointChunker."""

    def test_scored_chunking_empty_doc(self) -> None:
        """Empty or whitespace-only document returns no chunks."""
        chunker = ScoredBreakPointChunker()

        assert chunker.chunk("") == []
        assert chunker.chunk("   ") == []
        assert chunker.chunk("\n\n") == []

    def test_scored_chunking_tiny_doc(self) -> None:
        """Tiny document returns a single chunk."""
        chunker = ScoredBreakPointChunker(chunk_size=1024)
        doc = "# Hello\n\nThis is a tiny document."

        chunks = chunker.chunk(doc)
        assert len(chunks) == 1
        assert chunks[0].text == doc
        assert chunks[0].chunk_index == 0
        assert chunks[0].start_offset == 0
        assert chunks[0].end_offset == len(doc)
        assert chunks[0].tokens > 0
