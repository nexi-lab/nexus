"""Tests for MarkItDownParser — document-to-markdown conversion (Issue #1523)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.bricks.parsers.markitdown_parser import MarkItDownParser


class TestCanParse:
    def test_can_parse_supported_extension(self) -> None:
        p = MarkItDownParser()
        # Force availability without real markitdown
        p._available = True
        p._markitdown = MagicMock()
        assert p.can_parse("document.pdf") is True

    def test_cannot_parse_unsupported_extension(self) -> None:
        p = MarkItDownParser()
        p._available = True
        p._markitdown = MagicMock()
        assert p.can_parse("file.xyz") is False

    def test_cannot_parse_when_unavailable(self) -> None:
        p = MarkItDownParser()
        p._available = False
        assert p.can_parse("file.pdf") is False


class TestLazyInit:
    def test_not_initialized_at_construction(self) -> None:
        """MarkItDown should NOT be initialized until first use."""
        p = MarkItDownParser()
        assert p._available is None
        assert p._markitdown is None

    def test_initializes_on_can_parse(self) -> None:
        """Calling can_parse triggers lazy initialization."""
        p = MarkItDownParser()
        # _ensure_initialized will try to import markitdown and fail
        # but _available should be set (to False if markitdown not installed)
        p.can_parse("test.txt")
        assert p._available is not None


class TestMarkdownPassthrough:
    @pytest.mark.asyncio
    async def test_markdown_passthrough(self) -> None:
        """Markdown files should be returned as-is without conversion."""
        p = MarkItDownParser()
        p._available = True
        p._markitdown = MagicMock()

        content = b"# Hello\n\nWorld"
        result = await p.parse(content, {"path": "test.md"})
        assert result.text == "# Hello\n\nWorld"
        assert result.metadata["format"] == ".md"


class TestSupportedFormats:
    def test_supported_formats_returns_list(self) -> None:
        p = MarkItDownParser()
        formats = p.supported_formats
        assert isinstance(formats, list)
        assert ".pdf" in formats
        assert ".docx" in formats
        assert ".txt" in formats


class TestProperties:
    def test_name(self) -> None:
        p = MarkItDownParser()
        assert p.name == "MarkItDownParser"

    def test_priority(self) -> None:
        p = MarkItDownParser()
        assert p.priority == 50


class TestErrorHandling:
    @pytest.mark.asyncio
    async def test_parse_without_init_raises(self) -> None:
        p = MarkItDownParser()
        p._available = False
        p._markitdown = None
        from nexus.contracts.exceptions import ParserError

        with pytest.raises(ParserError, match="not initialized"):
            await p.parse(b"content", {"path": "test.pdf"})
