"""Unit tests for MarkItDownParser."""

import pytest

from nexus.core.exceptions import ParserError
from nexus.parsers.markitdown_parser import MarkItDownParser
from nexus.parsers.types import ParseResult


@pytest.fixture
def parser() -> MarkItDownParser:
    """Create a MarkItDownParser instance."""
    return MarkItDownParser()


def test_parser_initialization(parser: MarkItDownParser) -> None:
    """Test parser initialization."""
    assert parser.name == "MarkItDownParser"
    assert parser.priority == 50
    assert len(parser.supported_formats) > 0


def test_supported_formats(parser: MarkItDownParser) -> None:
    """Test that parser supports expected formats."""
    formats = parser.supported_formats

    # Should support common formats
    assert ".pdf" in formats
    assert ".docx" in formats
    assert ".xlsx" in formats
    assert ".txt" in formats
    assert ".md" in formats
    assert ".html" in formats
    assert ".json" in formats
    assert ".csv" in formats


def test_can_parse_supported_formats(parser: MarkItDownParser) -> None:
    """Test that parser correctly identifies supported formats."""
    # Supported formats
    assert parser.can_parse("document.pdf")
    assert parser.can_parse("spreadsheet.xlsx")
    assert parser.can_parse("text.txt")
    assert parser.can_parse("readme.md")
    assert parser.can_parse("page.html")

    # Case insensitive
    assert parser.can_parse("document.PDF")
    assert parser.can_parse("document.Pdf")


def test_can_parse_unsupported_formats(parser: MarkItDownParser) -> None:
    """Test that parser rejects unsupported formats."""
    assert not parser.can_parse("archive.tar")
    assert not parser.can_parse("video.mp4")
    assert not parser.can_parse("unknown.xyz")


@pytest.mark.asyncio
async def test_parse_text_file(parser: MarkItDownParser) -> None:
    """Test parsing a plain text file."""
    content = b"Hello, world!\nThis is a test."
    metadata = {"path": "test.txt"}

    result = await parser.parse(content, metadata)

    assert isinstance(result, ParseResult)
    assert "Hello, world!" in result.text
    assert "This is a test" in result.text
    assert result.metadata["parser"] == "MarkItDownParser"
    assert result.metadata["format"] == ".txt"
    assert len(result.chunks) > 0


@pytest.mark.asyncio
async def test_parse_markdown_file(parser: MarkItDownParser) -> None:
    """Test parsing a markdown file."""
    content = b"""# Title

## Section 1
This is section 1.

## Section 2
This is section 2.
"""
    metadata = {"path": "test.md"}

    result = await parser.parse(content, metadata)

    assert isinstance(result, ParseResult)
    assert "Title" in result.text
    assert "Section 1" in result.text
    assert "Section 2" in result.text

    # Should extract structure
    assert "headings" in result.structure
    assert result.structure["has_headings"]


@pytest.mark.asyncio
async def test_parse_json_file(parser: MarkItDownParser) -> None:
    """Test parsing a JSON file."""
    content = b'{"name": "test", "value": 123}'
    metadata = {"path": "test.json"}

    result = await parser.parse(content, metadata)

    assert isinstance(result, ParseResult)
    assert result.metadata["parser"] == "MarkItDownParser"
    assert result.metadata["format"] == ".json"


@pytest.mark.asyncio
async def test_parse_with_minimal_metadata(parser: MarkItDownParser) -> None:
    """Test parsing with minimal metadata."""
    content = b"Simple text"

    result = await parser.parse(content)

    assert isinstance(result, ParseResult)
    assert result.text
    assert result.metadata["parser"] == "MarkItDownParser"


@pytest.mark.asyncio
async def test_parse_result_chunks(parser: MarkItDownParser) -> None:
    """Test that parse result contains chunks."""
    content = b"""# Heading 1
Content for heading 1.

# Heading 2
Content for heading 2.
"""
    result = await parser.parse(content, {"path": "test.md"})

    # Should have multiple chunks (split by headings)
    assert len(result.chunks) > 0
    # Each chunk should have text
    for chunk in result.chunks:
        assert chunk.text


@pytest.mark.asyncio
async def test_parse_empty_file(parser: MarkItDownParser) -> None:
    """Test parsing an empty file."""
    content = b""

    # MarkItDown cannot parse empty files, so it should raise a ParserError
    with pytest.raises(ParserError, match="Failed to parse file"):
        await parser.parse(content, {"path": "empty.txt"})


def test_extract_structure_with_headings(parser: MarkItDownParser) -> None:
    """Test structure extraction from markdown with headings."""
    text = """# Main Title

## Section 1
Content here.

### Subsection 1.1
More content.

## Section 2
Final content.
"""

    structure = parser._extract_structure(text)

    assert "headings" in structure
    assert structure["has_headings"] is True
    assert len(structure["headings"]) == 4

    # Check heading levels
    assert structure["headings"][0]["level"] == 1
    assert structure["headings"][0]["text"] == "Main Title"
    assert structure["headings"][1]["level"] == 2
    assert structure["headings"][1]["text"] == "Section 1"


def test_extract_structure_without_headings(parser: MarkItDownParser) -> None:
    """Test structure extraction from text without headings."""
    text = "Just plain text without any headings."

    structure = parser._extract_structure(text)

    assert "headings" in structure
    assert structure["has_headings"] is False
    assert len(structure["headings"]) == 0


def test_create_chunks_with_headings(parser: MarkItDownParser) -> None:
    """Test chunk creation with headings."""
    text = """# Heading 1
Content 1

# Heading 2
Content 2"""

    chunks = parser._create_chunks(text)

    assert len(chunks) >= 2
    # Each chunk should have text
    for chunk in chunks:
        assert chunk.text
        assert chunk.start_index >= 0
        assert chunk.end_index >= chunk.start_index


def test_create_chunks_plain_text(parser: MarkItDownParser) -> None:
    """Test chunk creation with plain text."""
    text = "Just plain text without headings."

    chunks = parser._create_chunks(text)

    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].start_index == 0
    assert chunks[0].end_index == len(text)


def test_parser_name(parser: MarkItDownParser) -> None:
    """Test parser name property."""
    assert parser.name == "MarkItDownParser"


def test_parser_priority(parser: MarkItDownParser) -> None:
    """Test parser priority property."""
    assert parser.priority == 50


def test_parser_formats_immutable(parser: MarkItDownParser) -> None:
    """Test that modifying returned formats doesn't affect parser."""
    formats1 = parser.supported_formats
    formats2 = parser.supported_formats

    # Should return a copy each time
    assert formats1 == formats2
    assert formats1 is not formats2
