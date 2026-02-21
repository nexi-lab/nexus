"""Tests for nexus.parsers.utils — shared extract_structure & create_chunks."""

from __future__ import annotations

from nexus.bricks.parsers.utils import create_chunks, extract_structure


class TestExtractStructure:
    """Tests for the shared extract_structure() utility."""

    def test_basic_headings(self) -> None:
        text = "# Title\nContent\n## Section\nMore content"
        result = extract_structure(text)
        assert result["has_headings"] is True
        assert len(result["headings"]) == 2
        assert result["headings"][0] == {"level": 1, "text": "Title"}
        assert result["headings"][1] == {"level": 2, "text": "Section"}

    def test_empty_text(self) -> None:
        result = extract_structure("")
        assert result["has_headings"] is False
        assert result["headings"] == []
        assert result["line_count"] == 1  # Single empty line

    def test_no_headings(self) -> None:
        text = "Just plain text\nwith multiple lines."
        result = extract_structure(text)
        assert result["has_headings"] is False
        assert result["line_count"] == 2

    def test_empty_headings_filtered(self) -> None:
        """Headings that are just '#' with no text should be excluded (bug fix)."""
        text = "###\n# Real heading\n##  \nContent"
        result = extract_structure(text)
        assert len(result["headings"]) == 1
        assert result["headings"][0]["text"] == "Real heading"

    def test_line_count(self) -> None:
        text = "Line 1\nLine 2\nLine 3"
        result = extract_structure(text)
        assert result["line_count"] == 3


class TestCreateChunks:
    """Tests for the shared create_chunks() utility."""

    def test_single_chunk_no_headers(self) -> None:
        text = "Just plain text without any headers."
        chunks = create_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == text

    def test_splits_on_headers(self) -> None:
        text = "Preamble\n# Section 1\nContent 1\n# Section 2\nContent 2"
        chunks = create_chunks(text)
        assert len(chunks) == 3
        assert chunks[0].text == "Preamble"
        assert "Section 1" in chunks[1].text
        assert "Section 2" in chunks[2].text

    def test_empty_text(self) -> None:
        chunks = create_chunks("")
        assert len(chunks) == 1
        assert chunks[0].text == ""

    def test_chunks_have_indices(self) -> None:
        text = "Before\n# After"
        chunks = create_chunks(text)
        assert len(chunks) == 2
        assert chunks[0].start_index == 0
        assert chunks[0].end_index > 0

    def test_only_header(self) -> None:
        text = "# Only a header"
        chunks = create_chunks(text)
        assert len(chunks) == 1
        assert chunks[0].text == "# Only a header"
