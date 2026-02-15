"""Unit tests for ProviderRegistry."""

from typing import Any
from unittest.mock import patch

import pytest

from nexus.core.exceptions import ParserError
from nexus.parsers.providers.base import ParseProvider, ProviderConfig
from nexus.parsers.providers.registry import ProviderRegistry
from nexus.parsers.types import ParseResult

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class StubProvider(ParseProvider):
    """Minimal concrete ParseProvider for testing."""

    def __init__(
        self,
        name: str = "stub",
        formats: list[str] | None = None,
        priority: int = 50,
        available: bool = True,
    ) -> None:
        self._stub_name = name
        self._formats = formats or [".txt"]
        self._available = available
        config = ProviderConfig(name=name, priority=priority)
        super().__init__(config)

    @property
    def name(self) -> str:
        return self._stub_name

    @property
    def default_formats(self) -> list[str]:
        return self._formats

    def is_available(self) -> bool:
        return self._available

    async def parse(
        self,
        content: bytes,
        file_path: str,
        metadata: dict[str, Any] | None = None,
    ) -> ParseResult:
        return ParseResult(text=content.decode(), metadata=metadata or {})


@pytest.fixture()
def registry() -> ProviderRegistry:
    return ProviderRegistry()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegister:
    def test_register_provider(self, registry: ProviderRegistry) -> None:
        p = StubProvider("test_prov")
        registry.register(p)
        assert registry.get_provider_by_name("test_prov") is p

    def test_register_sorted_by_priority(self, registry: ProviderRegistry) -> None:
        low = StubProvider("low", priority=10)
        high = StubProvider("high", priority=100)
        registry.register(low)
        registry.register(high)
        providers = registry.get_all_providers()
        assert providers[0].name == "high"
        assert providers[1].name == "low"

    def test_register_unavailable_skipped(self, registry: ProviderRegistry) -> None:
        p = StubProvider("gone", available=False)
        registry.register(p)
        assert registry.get_provider_by_name("gone") is None
        assert len(registry.get_all_providers()) == 0

    def test_register_invalid_type_raises(self, registry: ProviderRegistry) -> None:
        with pytest.raises(ValueError, match="ParseProvider"):
            registry.register("not a provider")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------


class TestGetProvider:
    def test_by_file_path(self, registry: ProviderRegistry) -> None:
        pdf = StubProvider("pdf", formats=[".pdf"])
        txt = StubProvider("txt", formats=[".txt"])
        registry.register(pdf)
        registry.register(txt)
        assert registry.get_provider("doc.pdf") is pdf
        assert registry.get_provider("doc.txt") is txt

    def test_highest_priority_wins(self, registry: ProviderRegistry) -> None:
        lo = StubProvider("lo", formats=[".pdf"], priority=10)
        hi = StubProvider("hi", formats=[".pdf"], priority=100)
        registry.register(lo)
        registry.register(hi)
        assert registry.get_provider("doc.pdf") is hi

    def test_no_provider_returns_none(self, registry: ProviderRegistry) -> None:
        assert registry.get_provider("doc.xyz") is None


class TestGetProviderByName:
    def test_found(self, registry: ProviderRegistry) -> None:
        p = StubProvider("myp")
        registry.register(p)
        assert registry.get_provider_by_name("myp") is p

    def test_missing(self, registry: ProviderRegistry) -> None:
        assert registry.get_provider_by_name("nope") is None


# ---------------------------------------------------------------------------
# Parse delegation
# ---------------------------------------------------------------------------


class TestParse:
    @pytest.mark.asyncio
    async def test_parse_auto_select(self, registry: ProviderRegistry) -> None:
        p = StubProvider("txt", formats=[".txt"])
        registry.register(p)
        result = await registry.parse("test.txt", b"hello")
        assert result.text == "hello"
        assert result.metadata["provider"] == "txt"

    @pytest.mark.asyncio
    async def test_parse_by_name(self, registry: ProviderRegistry) -> None:
        p = StubProvider("txt", formats=[".txt"])
        registry.register(p)
        result = await registry.parse("test.txt", b"data", provider_name="txt")
        assert result.text == "data"

    @pytest.mark.asyncio
    async def test_parse_unknown_name_raises(self, registry: ProviderRegistry) -> None:
        with pytest.raises(ParserError, match="not found"):
            await registry.parse("f.txt", b"", provider_name="nope")

    @pytest.mark.asyncio
    async def test_parse_no_provider_raises(self, registry: ProviderRegistry) -> None:
        with pytest.raises(ParserError, match="No provider available"):
            await registry.parse("f.xyz", b"")


# ---------------------------------------------------------------------------
# Listing / formats
# ---------------------------------------------------------------------------


class TestListing:
    def test_get_all_providers(self, registry: ProviderRegistry) -> None:
        a = StubProvider("a")
        b = StubProvider("b")
        registry.register(a)
        registry.register(b)
        all_p = registry.get_all_providers()
        assert len(all_p) == 2
        # returns a copy
        all_p.pop()
        assert len(registry.get_all_providers()) == 2

    def test_get_supported_formats(self, registry: ProviderRegistry) -> None:
        registry.register(StubProvider("a", formats=[".pdf", ".txt"]))
        registry.register(StubProvider("b", formats=[".docx", ".txt"]))
        fmts = registry.get_supported_formats()
        assert fmts == [".docx", ".pdf", ".txt"]


# ---------------------------------------------------------------------------
# Clear / repr
# ---------------------------------------------------------------------------


class TestClearRepr:
    def test_clear(self, registry: ProviderRegistry) -> None:
        registry.register(StubProvider("x"))
        registry.clear()
        assert len(registry.get_all_providers()) == 0
        assert registry.get_provider_by_name("x") is None

    def test_repr(self, registry: ProviderRegistry) -> None:
        registry.register(StubProvider("alpha"))
        r = repr(registry)
        assert "ProviderRegistry" in r
        assert "alpha" in r


# ---------------------------------------------------------------------------
# Auto-discover (with mocked env)
# ---------------------------------------------------------------------------


class TestAutoDiscover:
    def test_auto_discover_markitdown(self, registry: ProviderRegistry) -> None:
        try:
            import markitdown  # noqa: F401
        except ImportError:
            pytest.skip("markitdown not installed")
        count = registry.auto_discover()
        assert count >= 1
        assert registry.get_provider_by_name("markitdown") is not None

    def test_auto_discover_with_env_vars(self, registry: ProviderRegistry) -> None:
        with patch.dict(
            "os.environ",
            {"UNSTRUCTURED_API_KEY": "fake-key", "LLAMA_CLOUD_API_KEY": "fake-key"},
        ):
            count = registry.auto_discover()
            # Count depends on which packages are installed
            assert count >= 0
