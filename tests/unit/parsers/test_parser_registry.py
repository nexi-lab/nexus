"""Tests for ParserRegistry — extension-based parser selection (Issue #1523)."""

from unittest.mock import MagicMock

import pytest

from nexus.contracts.exceptions import ParserError
from nexus.parsers.base import Parser
from nexus.parsers.registry import ParserRegistry


def _make_parser(
    name: str = "test",
    formats: list[str] | None = None,
    priority: int = 0,
    can: bool = True,
) -> Parser:
    """Create a mock Parser with controllable properties."""
    p = MagicMock(spec=Parser)
    p.name = name
    p.supported_formats = formats or [".txt"]
    p.priority = priority
    p.can_parse = MagicMock(return_value=can)
    # Make isinstance check pass
    p.__class__ = type(
        name,
        (Parser,),
        {
            "can_parse": lambda self, *a, **k: can,
            "parse": lambda self, *a, **k: None,
            "supported_formats": property(lambda self: formats or [".txt"]),
            "name": property(lambda self: name),
            "priority": property(lambda self: priority),
        },
    )
    return p


class TestRegister:
    def test_register_valid_parser(self) -> None:
        reg = ParserRegistry()
        p = _make_parser("md", [".md"])
        reg.register(p)
        assert len(reg) >= 1

    def test_register_non_parser_raises(self) -> None:
        reg = ParserRegistry()
        with pytest.raises(ValueError, match="must be an instance of Parser"):
            reg.register("not a parser")  # type: ignore[arg-type]


class TestGetParser:
    def test_get_parser_by_extension(self) -> None:
        reg = ParserRegistry()
        p = _make_parser("txt_parser", [".txt"], priority=50)
        reg.register(p)
        result = reg.get_parser("doc.txt")
        assert result.name == "txt_parser"

    def test_get_parser_priority_ordering(self) -> None:
        reg = ParserRegistry()
        low = _make_parser("low", [".pdf"], priority=10)
        high = _make_parser("high", [".pdf"], priority=90)
        reg.register(low)
        reg.register(high)
        result = reg.get_parser("file.pdf")
        assert result.name == "high"

    def test_get_parser_unsupported_format_raises(self) -> None:
        reg = ParserRegistry()
        with pytest.raises(ParserError):
            reg.get_parser("file.xyz")


class TestSupportedFormats:
    def test_get_supported_formats(self) -> None:
        reg = ParserRegistry()
        p = _make_parser("multi", [".pdf", ".doc", ".txt"])
        reg.register(p)
        formats = reg.get_supported_formats()
        assert ".pdf" in formats
        assert ".doc" in formats

    def test_formats_deduplicated(self) -> None:
        reg = ParserRegistry()
        p1 = _make_parser("p1", [".txt"])
        p2 = _make_parser("p2", [".txt", ".md"])
        reg.register(p1)
        reg.register(p2)
        formats = reg.get_supported_formats()
        assert formats.count(".txt") == 1


class TestUnregister:
    def test_unregister_removes_parser(self) -> None:
        reg = ParserRegistry()
        p = _make_parser("removable", [".csv"])
        reg.register(p)
        removed = reg.unregister("removable")
        assert removed is not None
        assert "removable" not in reg


class TestClear:
    def test_clear_removes_all(self) -> None:
        reg = ParserRegistry()
        reg.register(_make_parser("a", [".txt"]))
        reg.register(_make_parser("b", [".md"]))
        reg.clear()
        assert len(reg) == 0
        assert reg.get_parsers() == []
