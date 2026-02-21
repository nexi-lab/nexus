"""Unit tests for parse provider implementations.

Tests _extract_structure(), _create_chunks(), is_available(), and parse()
for MarkItDownProvider, LlamaParseProvider, and UnstructuredProvider
with mocked external dependencies.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.contracts.exceptions import ParserError
from nexus.parsers.providers.base import ProviderConfig

# ── MarkItDownProvider ────────────────────────────────────────────


class TestMarkItDownProviderExtractStructure:
    """Tests for MarkItDownProvider._extract_structure()."""

    def setup_method(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        self.provider = MarkItDownProvider(ProviderConfig(name="markitdown"))

    def test_basic_headings(self) -> None:
        text = "# Title\n\nSome text\n\n## Section\n\nMore text"
        result = self.provider._extract_structure(text)
        assert result["has_headings"] is True
        assert len(result["headings"]) == 2
        assert result["headings"][0] == {"level": 1, "text": "Title"}
        assert result["headings"][1] == {"level": 2, "text": "Section"}

    def test_no_headings(self) -> None:
        text = "Just plain text\nwith no headings"
        result = self.provider._extract_structure(text)
        assert result["has_headings"] is False
        assert result["headings"] == []

    def test_empty_heading_text_skipped(self) -> None:
        """MarkItDownProvider skips bare # lines (has 'if heading_text:' guard)."""
        text = "#\n## Real Heading\n###   "
        result = self.provider._extract_structure(text)
        assert len(result["headings"]) == 1
        assert result["headings"][0]["text"] == "Real Heading"

    def test_line_count(self) -> None:
        text = "line1\nline2\nline3"
        result = self.provider._extract_structure(text)
        assert result["line_count"] == 3

    def test_empty_text(self) -> None:
        result = self.provider._extract_structure("")
        assert result["line_count"] == 1  # empty string split gives [""]
        assert result["has_headings"] is False


class TestMarkItDownProviderCreateChunks:
    """Tests for MarkItDownProvider._create_chunks()."""

    def setup_method(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        self.provider = MarkItDownProvider(ProviderConfig(name="markitdown"))

    def test_splits_on_headers(self) -> None:
        text = "Intro text\n\n# Header\n\nContent after header"
        chunks = self.provider._create_chunks(text)
        assert len(chunks) == 2
        assert "Intro text" in chunks[0].text
        assert "Header" in chunks[1].text

    def test_single_chunk_no_headers(self) -> None:
        text = "Just text\nMore text"
        chunks = self.provider._create_chunks(text)
        assert len(chunks) == 1

    def test_empty_text_returns_single_chunk(self) -> None:
        chunks = self.provider._create_chunks("")
        assert len(chunks) == 1
        assert chunks[0].text == ""


class TestMarkItDownProviderIsAvailable:
    """Tests for MarkItDownProvider.is_available()."""

    def test_available_when_installed(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider()
        with patch.dict("sys.modules", {"markitdown": MagicMock()}):
            assert provider.is_available() is True

    def test_unavailable_when_not_installed(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider()
        with patch.dict("sys.modules", {"markitdown": None}):
            # Import will raise ImportError for None modules
            assert provider.is_available() is False


class TestMarkItDownProviderParse:
    """Tests for MarkItDownProvider.parse() with mocked markitdown."""

    @pytest.mark.asyncio
    async def test_parse_markdown_passthrough(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider(ProviderConfig(name="markitdown"))
        content = b"# Hello\n\nWorld"
        result = await provider.parse(content, "test.md")
        assert result.text == "# Hello\n\nWorld"
        assert result.metadata["parser"] == "markitdown"
        assert result.metadata["format"] == ".md"

    @pytest.mark.asyncio
    async def test_parse_txt_passthrough(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider(ProviderConfig(name="markitdown"))
        content = b"Plain text content"
        result = await provider.parse(content, "test.txt")
        assert result.text == "Plain text content"

    @pytest.mark.asyncio
    async def test_parse_binary_uses_markitdown(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider(ProviderConfig(name="markitdown"))

        mock_result = MagicMock()
        mock_result.text_content = "Converted PDF content"

        mock_markitdown = MagicMock()
        mock_markitdown.convert_stream.return_value = mock_result
        provider._markitdown = mock_markitdown

        result = await provider.parse(b"fake pdf bytes", "document.pdf")
        assert result.text == "Converted PDF content"
        mock_markitdown.convert_stream.assert_called_once()

    @pytest.mark.asyncio
    async def test_parse_error_wrapping(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        provider = MarkItDownProvider(ProviderConfig(name="markitdown"))

        mock_markitdown = MagicMock()
        mock_markitdown.convert_stream.side_effect = RuntimeError("boom")
        provider._markitdown = mock_markitdown

        with pytest.raises(ParserError, match="Failed to parse with MarkItDown"):
            await provider.parse(b"data", "test.pdf")


# ── LlamaParseProvider ────────────────────────────────────────────


class TestLlamaParseProviderExtractStructure:
    """Tests for LlamaParseProvider._extract_structure()."""

    def setup_method(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        self.provider = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="fake-key"))

    def test_basic_headings(self) -> None:
        text = "# Title\n## Subtitle"
        result = self.provider._extract_structure(text)
        assert len(result["headings"]) == 2

    def test_empty_heading_skipped(self) -> None:
        """LlamaParseProvider has the 'if heading_text:' guard."""
        text = "#\n## Real"
        result = self.provider._extract_structure(text)
        assert len(result["headings"]) == 1


class TestLlamaParseProviderIsAvailable:
    """Tests for LlamaParseProvider.is_available()."""

    def test_unavailable_without_api_key(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        provider = LlamaParseProvider(ProviderConfig(name="llamaparse"))
        assert provider.is_available() is False

    def test_unavailable_without_package(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        provider = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="key"))
        with patch.dict("sys.modules", {"llama_parse": None}):
            assert provider.is_available() is False


class TestLlamaParseProviderParse:
    """Tests for LlamaParseProvider.parse() with mocked llama_parse."""

    @pytest.mark.asyncio
    async def test_parse_with_async_api(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        provider = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="fake-key"))

        mock_doc = MagicMock()
        mock_doc.text = "Parsed document content"
        mock_doc.doc_id = "doc-1"

        mock_parser = MagicMock()
        mock_parser.aload_data = AsyncMock(return_value=[mock_doc])
        provider._parser = mock_parser

        result = await provider.parse(b"pdf data", "test.pdf")
        assert result.text == "Parsed document content"
        assert result.metadata["page_count"] == 1

    @pytest.mark.asyncio
    async def test_parse_empty_result(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        provider = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="key"))

        mock_parser = MagicMock()
        mock_parser.aload_data = AsyncMock(return_value=[])
        provider._parser = mock_parser

        result = await provider.parse(b"data", "test.pdf")
        assert result.text == ""
        assert "warning" in result.metadata

    @pytest.mark.asyncio
    async def test_parse_error_wrapping(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        provider = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="key"))

        mock_parser = MagicMock()
        mock_parser.aload_data = AsyncMock(side_effect=RuntimeError("API error"))
        provider._parser = mock_parser

        with pytest.raises(ParserError, match="Failed to parse with LlamaParse"):
            await provider.parse(b"data", "test.pdf")


# ── UnstructuredProvider ──────────────────────────────────────────


class TestUnstructuredProviderExtractStructure:
    """Tests for UnstructuredProvider._extract_structure() (element-based, not text-based)."""

    def setup_method(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        self.provider = UnstructuredProvider(
            ProviderConfig(name="unstructured", api_key="fake-key")
        )

    def test_title_and_header_elements(self) -> None:
        elements = [
            {"type": "Title", "text": "Main Title"},
            {"type": "NarrativeText", "text": "Some body text"},
            {"type": "Header", "text": "Sub Header"},
        ]
        result = self.provider._extract_structure(elements)
        assert len(result["headings"]) == 2
        assert result["headings"][0] == {"level": 1, "text": "Main Title"}
        assert result["headings"][1] == {"level": 2, "text": "Sub Header"}
        assert result["element_count"] == 3
        assert result["element_types"]["Title"] == 1
        assert result["element_types"]["NarrativeText"] == 1

    def test_no_headings(self) -> None:
        elements = [{"type": "NarrativeText", "text": "Just text"}]
        result = self.provider._extract_structure(elements)
        assert result["has_headings"] is False

    def test_empty_elements(self) -> None:
        result = self.provider._extract_structure([])
        assert result["element_count"] == 0
        assert result["has_headings"] is False


class TestUnstructuredProviderIsAvailable:
    """Tests for UnstructuredProvider.is_available()."""

    def test_unavailable_without_api_key(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        provider = UnstructuredProvider(ProviderConfig(name="unstructured"))
        assert provider.is_available() is False

    def test_unavailable_without_httpx(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        provider = UnstructuredProvider(ProviderConfig(name="unstructured", api_key="key"))
        with patch.dict("sys.modules", {"httpx": None}):
            assert provider.is_available() is False


class TestUnstructuredProviderParse:
    """Tests for UnstructuredProvider.parse() with mocked httpx.

    httpx is imported locally inside parse(), so we mock via sys.modules.
    """

    def _make_mock_httpx(self, response_status: int, response_data: object) -> MagicMock:
        """Build a mock httpx module with AsyncClient that returns given response."""
        mock_response = MagicMock()
        mock_response.status_code = response_status
        mock_response.json.return_value = response_data
        mock_response.text = str(response_data)

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx = MagicMock()
        mock_httpx.AsyncClient.return_value = mock_client
        mock_httpx.TimeoutException = type("TimeoutException", (Exception,), {})
        mock_httpx.HTTPError = type("HTTPError", (Exception,), {})
        return mock_httpx

    @pytest.mark.asyncio
    async def test_parse_success(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        provider = UnstructuredProvider(ProviderConfig(name="unstructured", api_key="key"))

        response_data = [
            {"type": "Title", "text": "Document Title", "element_id": "1"},
            {"type": "NarrativeText", "text": "Body text", "element_id": "2"},
        ]
        mock_httpx = self._make_mock_httpx(200, response_data)

        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = await provider.parse(b"content", "doc.pdf")

        assert "Document Title" in result.text
        assert "Body text" in result.text
        assert result.metadata["element_count"] == 2

    @pytest.mark.asyncio
    async def test_parse_api_error(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        provider = UnstructuredProvider(ProviderConfig(name="unstructured", api_key="key"))
        mock_httpx = self._make_mock_httpx(500, {"detail": "Internal Server Error"})

        with (
            patch.dict("sys.modules", {"httpx": mock_httpx}),
            pytest.raises(ParserError, match="Unstructured API error: 500"),
        ):
            await provider.parse(b"content", "doc.pdf")

    @pytest.mark.asyncio
    async def test_parse_empty_elements_skipped(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        provider = UnstructuredProvider(ProviderConfig(name="unstructured", api_key="key"))

        response_data = [
            {"type": "NarrativeText", "text": ""},  # empty text, should be skipped
            {"type": "NarrativeText", "text": "Real content"},
        ]
        mock_httpx = self._make_mock_httpx(200, response_data)

        with patch.dict("sys.modules", {"httpx": mock_httpx}):
            result = await provider.parse(b"content", "doc.pdf")

        assert "Real content" in result.text
        assert len(result.chunks) == 1  # only non-empty element


# ── Cross-provider tests ─────────────────────────────────────────


class TestProviderProperties:
    """Test common properties across all providers."""

    def test_markitdown_name_and_formats(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        p = MarkItDownProvider()
        assert p.name == "markitdown"
        assert ".pdf" in p.default_formats
        assert ".md" in p.default_formats

    def test_llamaparse_name_and_formats(self) -> None:
        from nexus.parsers.providers.llamaparse_provider import LlamaParseProvider

        p = LlamaParseProvider(ProviderConfig(name="llamaparse", api_key="k"))
        assert p.name == "llamaparse"
        assert ".pdf" in p.default_formats

    def test_unstructured_name_and_formats(self) -> None:
        from nexus.parsers.providers.unstructured_provider import UnstructuredProvider

        p = UnstructuredProvider(ProviderConfig(name="unstructured", api_key="k"))
        assert p.name == "unstructured"
        assert ".pdf" in p.default_formats
        assert ".eml" in p.default_formats  # unique to unstructured

    def test_supported_formats_uses_config_override(self) -> None:
        from nexus.parsers.providers.markitdown_provider import MarkItDownProvider

        config = ProviderConfig(name="markitdown", supported_formats=[".pdf"])
        p = MarkItDownProvider(config)
        assert p.supported_formats == [".pdf"]
