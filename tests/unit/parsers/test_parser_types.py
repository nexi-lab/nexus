"""Unit tests for parser types and data structures."""

import pytest

from nexus.parsers.types import ImageData, ParseResult, TextChunk


def test_text_chunk_creation() -> None:
    """Test TextChunk creation."""
    chunk = TextChunk(
        text="Hello, world!",
        start_index=0,
        end_index=13,
        metadata={"section": "intro"},
    )

    assert chunk.text == "Hello, world!"
    assert chunk.start_index == 0
    assert chunk.end_index == 13
    assert chunk.metadata == {"section": "intro"}


def test_text_chunk_defaults() -> None:
    """Test TextChunk default values."""
    chunk = TextChunk(text="Sample text")

    assert chunk.text == "Sample text"
    assert chunk.start_index == 0
    assert chunk.end_index == 0
    assert chunk.metadata == {}


def test_image_data_creation() -> None:
    """Test ImageData creation."""
    image = ImageData(
        data=b"fake_image_data",
        format="png",
        width=800,
        height=600,
        metadata={"source": "document"},
    )

    assert image.data == b"fake_image_data"
    assert image.format == "png"
    assert image.width == 800
    assert image.height == 600
    assert image.metadata == {"source": "document"}


def test_image_data_defaults() -> None:
    """Test ImageData default values."""
    image = ImageData(data=b"data", format="jpg")

    assert image.data == b"data"
    assert image.format == "jpg"
    assert image.width is None
    assert image.height is None
    assert image.metadata == {}


def test_parse_result_creation() -> None:
    """Test ParseResult creation."""
    chunks = [
        TextChunk(text="Chunk 1", start_index=0, end_index=7),
        TextChunk(text="Chunk 2", start_index=8, end_index=15),
    ]

    result = ParseResult(
        text="Chunk 1 Chunk 2",
        metadata={"format": "txt"},
        structure={"headings": []},
        chunks=chunks,
        images=[],
        raw_content="Chunk 1 Chunk 2",
    )

    assert result.text == "Chunk 1 Chunk 2"
    assert result.metadata == {"format": "txt"}
    assert result.structure == {"headings": []}
    assert len(result.chunks) == 2
    assert result.images == []
    assert result.raw_content == "Chunk 1 Chunk 2"


def test_parse_result_defaults() -> None:
    """Test ParseResult default values."""
    result = ParseResult(text="Sample text")

    assert result.text == "Sample text"
    assert result.metadata == {}
    assert result.structure == {}
    assert result.images == []
    assert result.raw_content is None


def test_parse_result_auto_chunk_creation() -> None:
    """Test that ParseResult creates a default chunk if none provided."""
    result = ParseResult(text="Sample text")

    assert len(result.chunks) == 1
    assert result.chunks[0].text == "Sample text"
    assert result.chunks[0].start_index == 0
    assert result.chunks[0].end_index == len("Sample text")


def test_parse_result_preserves_custom_chunks() -> None:
    """Test that ParseResult preserves custom chunks if provided."""
    chunks = [
        TextChunk(text="Custom chunk", start_index=0, end_index=12),
    ]

    result = ParseResult(text="Sample text", chunks=chunks)

    assert len(result.chunks) == 1
    assert result.chunks[0].text == "Custom chunk"


def test_parse_result_validation_error() -> None:
    """Test that ParseResult validates text is a string."""
    with pytest.raises(ValueError, match="text must be a string"):
        ParseResult(text=123)  # type: ignore


def test_parse_result_empty_text() -> None:
    """Test ParseResult with empty text."""
    result = ParseResult(text="")

    assert result.text == ""
    assert len(result.chunks) == 0
