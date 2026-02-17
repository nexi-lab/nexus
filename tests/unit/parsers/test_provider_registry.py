"""Tests for ProviderRegistry — provider selection and parse delegation (Issue #1523)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.contracts.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider
from nexus.parsers.providers.registry import ProviderRegistry
from nexus.parsers.types import ParseResult


def _make_provider(
    name: str = "test",
    formats: list[str] | None = None,
    priority: int = 50,
    available: bool = True,
    can_parse: bool = True,
) -> ParseProvider:
    """Create a mock ParseProvider."""
    p = MagicMock(spec=ParseProvider)
    p.name = name
    p.supported_formats = formats or [".pdf"]
    p.priority = priority
    p.is_available = MagicMock(return_value=available)
    p.can_parse = MagicMock(return_value=can_parse)
    p.parse = AsyncMock(return_value=ParseResult(text="parsed", metadata={}))
    # Make isinstance check pass
    p.__class__ = type(name, (ParseProvider,), {
        "name": property(lambda self: name),
        "priority": property(lambda self: priority),
        "supported_formats": property(lambda self: formats or [".pdf"]),
        "is_available": lambda self: available,
        "can_parse": lambda self, path: can_parse,
        "parse": AsyncMock(return_value=ParseResult(text="parsed", metadata={})),
    })
    return p


class TestRegister:
    def test_register_available_provider(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("good", available=True)
        reg.register(p)
        assert len(reg) == 1

    def test_skip_unavailable_provider(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("bad", available=False)
        reg.register(p)
        assert len(reg) == 0

    def test_register_non_provider_raises(self) -> None:
        reg = ProviderRegistry()
        with pytest.raises(ValueError, match="must be a ParseProvider"):
            reg.register("not a provider")  # type: ignore[arg-type]


class TestPriorityOrdering:
    def test_higher_priority_first(self) -> None:
        reg = ProviderRegistry()
        low = _make_provider("low", priority=10)
        high = _make_provider("high", priority=90)
        reg.register(low)
        reg.register(high)
        providers = reg.get_all_providers()
        assert providers[0].name == "high"


class TestGetProvider:
    def test_get_provider_returns_match(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("pdf_provider", [".pdf"])
        reg.register(p)
        result = reg.get_provider("doc.pdf")
        assert result is not None

    def test_get_provider_returns_none_for_no_match(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("pdf_only", [".pdf"], can_parse=False)
        reg.register(p)
        result = reg.get_provider("doc.xyz")
        assert result is None


class TestParse:
    @pytest.mark.asyncio
    async def test_parse_delegation(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("test_parser")
        reg.register(p)
        result = await reg.parse("file.pdf", b"content")
        assert result.text == "parsed"

    @pytest.mark.asyncio
    async def test_parse_no_provider_raises(self) -> None:
        reg = ProviderRegistry()
        with pytest.raises(ParserError, match="No provider available"):
            await reg.parse("file.xyz", b"content")

    @pytest.mark.asyncio
    async def test_parse_with_specific_provider(self) -> None:
        reg = ProviderRegistry()
        p = _make_provider("specific")
        reg.register(p)
        result = await reg.parse("file.pdf", b"content", provider_name="specific")
        assert result.text == "parsed"


class TestClear:
    def test_clear_removes_all(self) -> None:
        reg = ProviderRegistry()
        reg.register(_make_provider("a"))
        reg.register(_make_provider("b"))
        reg.clear()
        assert len(reg) == 0
        assert reg.get_all_providers() == []
