"""Tests for parser data types — ParseResult, TextChunk, ImageData (Issue #1523)."""


import pytest

from nexus.parsers.types import ImageData, ParseResult, TextChunk


class TestTextChunk:
    def test_create_basic(self) -> None:
        chunk = TextChunk(text="hello", start_index=0, end_index=5)
        assert chunk.text == "hello"
        assert chunk.start_index == 0
        assert chunk.end_index == 5

    def test_default_metadata(self) -> None:
        chunk = TextChunk(text="hi")
        assert chunk.metadata == {}

    def test_with_metadata(self) -> None:
        chunk = TextChunk(text="hi", metadata={"lang": "en"})
        assert chunk.metadata["lang"] == "en"


class TestImageData:
    def test_create_basic(self) -> None:
        img = ImageData(data=b"\x89PNG", format="png")
        assert img.data == b"\x89PNG"
        assert img.format == "png"

    def test_optional_dimensions(self) -> None:
        img = ImageData(data=b"", format="jpg", width=100, height=200)
        assert img.width == 100
        assert img.height == 200

    def test_default_dimensions_none(self) -> None:
        img = ImageData(data=b"", format="png")
        assert img.width is None
        assert img.height is None


class TestParseResult:
    def test_create_basic(self) -> None:
        result = ParseResult(text="hello world")
        assert result.text == "hello world"
        assert isinstance(result.metadata, dict)
        assert isinstance(result.chunks, list)

    def test_auto_chunk_creation(self) -> None:
        result = ParseResult(text="some text")
        assert len(result.chunks) == 1
        assert result.chunks[0].text == "some text"

    def test_explicit_chunks_preserved(self) -> None:
        chunks = [TextChunk(text="a"), TextChunk(text="b")]
        result = ParseResult(text="a b", chunks=chunks)
        assert len(result.chunks) == 2

    def test_invalid_text_type_raises(self) -> None:
        with pytest.raises(ValueError, match="text must be a string"):
            ParseResult(text=123)  # type: ignore[arg-type]

    def test_metadata_default_empty(self) -> None:
        result = ParseResult(text="x")
        assert result.metadata == {}

    def test_structure_default_empty(self) -> None:
        result = ParseResult(text="x")
        assert result.structure == {}

    def test_raw_content_default_none(self) -> None:
        result = ParseResult(text="x")
        assert result.raw_content is None
