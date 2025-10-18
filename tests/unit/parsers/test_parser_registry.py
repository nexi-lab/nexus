"""Unit tests for ParserRegistry."""

from typing import Any

import pytest

from nexus.core.exceptions import ParserError
from nexus.parsers.base import Parser
from nexus.parsers.registry import ParserRegistry
from nexus.parsers.types import ParseResult


class MockParser(Parser):
    """Mock parser for testing."""

    def __init__(self, formats: list[str], priority: int = 0, name: str = "MockParser") -> None:
        self._formats = formats
        self._priority = priority
        self._name = name

    def can_parse(self, file_path: str, mime_type: str | None = None) -> bool:
        ext = self._get_file_extension(file_path)
        return ext in self._formats

    async def parse(self, content: bytes, metadata: dict[str, Any] | None = None) -> ParseResult:
        return ParseResult(
            text="Mock parsed content",
            metadata=metadata or {},
        )

    @property
    def supported_formats(self) -> list[str]:
        return self._formats

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority


def test_registry_initialization() -> None:
    """Test ParserRegistry initialization."""
    registry = ParserRegistry()

    assert len(registry.get_parsers()) == 0
    assert registry.get_supported_formats() == []


def test_register_parser() -> None:
    """Test registering a parser."""
    registry = ParserRegistry()
    parser = MockParser([".txt", ".md"])

    registry.register(parser)

    assert len(registry.get_parsers()) == 1
    assert parser in registry.get_parsers()
    assert ".txt" in registry.get_supported_formats()
    assert ".md" in registry.get_supported_formats()


def test_register_multiple_parsers() -> None:
    """Test registering multiple parsers."""
    registry = ParserRegistry()
    parser1 = MockParser([".txt"], name="Parser1")
    parser2 = MockParser([".pdf"], name="Parser2")
    parser3 = MockParser([".docx"], name="Parser3")

    registry.register(parser1)
    registry.register(parser2)
    registry.register(parser3)

    assert len(registry.get_parsers()) == 3
    assert set(registry.get_supported_formats()) == {".txt", ".pdf", ".docx"}


def test_register_invalid_parser() -> None:
    """Test that registering an invalid parser raises an error."""
    registry = ParserRegistry()

    with pytest.raises(ValueError, match="Parser must be an instance of Parser"):
        registry.register("not a parser")  # type: ignore


def test_get_parser_by_extension() -> None:
    """Test getting a parser by file extension."""
    registry = ParserRegistry()
    txt_parser = MockParser([".txt"], name="TxtParser")
    pdf_parser = MockParser([".pdf"], name="PdfParser")

    registry.register(txt_parser)
    registry.register(pdf_parser)

    # Get parser for .txt file
    parser = registry.get_parser("document.txt")
    assert parser == txt_parser

    # Get parser for .pdf file
    parser = registry.get_parser("document.pdf")
    assert parser == pdf_parser


def test_get_parser_not_found() -> None:
    """Test that getting a parser for unsupported format raises an error."""
    registry = ParserRegistry()
    parser = MockParser([".txt"])
    registry.register(parser)

    with pytest.raises(ParserError, match="No parser found"):
        registry.get_parser("document.xyz")


def test_parser_priority_ordering() -> None:
    """Test that parsers are selected by priority."""
    registry = ParserRegistry()

    # Register two parsers for the same format with different priorities
    low_priority = MockParser([".txt"], priority=10, name="LowPriority")
    high_priority = MockParser([".txt"], priority=100, name="HighPriority")

    registry.register(low_priority)
    registry.register(high_priority)

    # High priority parser should be selected
    parser = registry.get_parser("document.txt")
    assert parser == high_priority


def test_clear_registry() -> None:
    """Test clearing the registry."""
    registry = ParserRegistry()
    parser = MockParser([".txt"])

    registry.register(parser)
    assert len(registry.get_parsers()) == 1

    registry.clear()
    assert len(registry.get_parsers()) == 0
    assert registry.get_supported_formats() == []


def test_get_parser_case_insensitive() -> None:
    """Test that parser selection is case-insensitive."""
    registry = ParserRegistry()
    parser = MockParser([".txt"])
    registry.register(parser)

    # Both should work
    assert registry.get_parser("document.txt") == parser
    assert registry.get_parser("document.TXT") == parser


def test_registry_repr() -> None:
    """Test registry string representation."""
    registry = ParserRegistry()
    parser1 = MockParser([".txt"], name="Parser1")
    parser2 = MockParser([".pdf"], name="Parser2")

    registry.register(parser1)
    registry.register(parser2)

    repr_str = repr(registry)
    assert "ParserRegistry" in repr_str
    assert "Parser1" in repr_str
    assert "Parser2" in repr_str


def test_multiple_parsers_same_format() -> None:
    """Test handling multiple parsers for the same format."""
    registry = ParserRegistry()

    parser1 = MockParser([".txt"], priority=50, name="Parser1")
    parser2 = MockParser([".txt"], priority=100, name="Parser2")
    parser3 = MockParser([".txt"], priority=25, name="Parser3")

    registry.register(parser1)
    registry.register(parser2)
    registry.register(parser3)

    # Highest priority parser should be selected
    parser = registry.get_parser("test.txt")
    assert parser == parser2


def test_discover_parsers() -> None:
    """Test auto-discovery of parsers from package."""
    registry = ParserRegistry()

    # Discover parsers from nexus.parsers package
    count = registry.discover_parsers("nexus.parsers")

    # Should find at least MarkItDownParser
    assert count >= 1
    assert len(registry.get_parsers()) >= 1

    # Should be able to get a parser for a supported format
    parsers = registry.get_parsers()
    assert any(p.name == "MarkItDownParser" for p in parsers)


def test_discover_parsers_empty_package() -> None:
    """Test auto-discovery with non-existent package."""
    registry = ParserRegistry()

    # Try to discover from non-existent package
    count = registry.discover_parsers("nonexistent.package")

    # Should not find any parsers
    assert count == 0
    assert len(registry.get_parsers()) == 0


def test_discover_parsers_no_duplicates() -> None:
    """Test that auto-discovery doesn't register duplicates."""
    registry = ParserRegistry()

    # Discover twice
    count1 = registry.discover_parsers("nexus.parsers")
    count2 = registry.discover_parsers("nexus.parsers")

    # Should register the same number each time
    assert count1 == count2
    # Total parsers should be 2x the first count
    assert len(registry.get_parsers()) == count1 + count2
