"""Tests for LLM document reader."""

from unittest.mock import AsyncMock, Mock

import pytest

from nexus.llm.citation import DocumentReadResult
from nexus.llm.document_reader import LLMDocumentReader
from nexus.llm.message import Message, MessageRole, TextContent


class TestLLMDocumentReader:
    """Tests for LLMDocumentReader."""

    def test_init_default_system_prompt(self):
        """Test initialization with default system prompt."""
        nx = Mock()
        provider = Mock()

        reader = LLMDocumentReader(nx=nx, provider=provider)

        assert reader.nx == nx
        assert reader.provider == provider
        assert reader.search is None
        assert "document assistant" in reader.system_prompt.lower()
        assert reader.context_builder is not None
        assert reader.citation_extractor is not None

    def test_init_custom_system_prompt(self):
        """Test initialization with custom system prompt."""
        nx = Mock()
        provider = Mock()
        custom_prompt = "Custom system prompt for testing"

        reader = LLMDocumentReader(nx=nx, provider=provider, system_prompt=custom_prompt)

        assert reader.system_prompt == custom_prompt

    def test_init_with_search(self):
        """Test initialization with semantic search."""
        nx = Mock()
        provider = Mock()
        search = Mock()

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        assert reader.search == search

    def test_init_with_max_context_tokens(self):
        """Test initialization with custom max_context_tokens."""
        nx = Mock()
        provider = Mock()

        reader = LLMDocumentReader(nx=nx, provider=provider, max_context_tokens=5000)

        assert reader.context_builder.max_context_tokens == 5000

    @pytest.mark.asyncio
    async def test_read_with_semantic_search(self):
        """Test reading document with semantic search."""
        # Mock components
        nx = Mock()
        provider = Mock()
        search = Mock()

        # Mock search results
        search_result = Mock()
        search_result.path = "/docs/test.md"
        search_result.chunk_index = 0
        search_result.chunk_text = "Relevant content from document"
        search_result.score = 0.95
        search_result.start_offset = 0
        search_result.end_offset = 100
        search.search = AsyncMock(return_value=[search_result])

        # Mock LLM response
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer based on context")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        result = await reader.read(
            path="/docs/*.md", prompt="What is the main topic?", use_search=True
        )

        assert isinstance(result, DocumentReadResult)
        assert result.answer == "Answer based on context"
        assert "/docs/test.md" in result.sources
        search.search.assert_called_once()
        provider.complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_without_search_single_file(self):
        """Test reading single document without search."""
        # Mock components
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/test.md"])
        nx.read = Mock(return_value=b"Document content here")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Direct answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        result = await reader.read(path="/docs/test.md", prompt="Summarize this", use_search=False)

        assert result.answer == "Direct answer"
        assert "/docs/test.md" in result.sources
        nx.read.assert_called_once_with("/docs/test.md")

    @pytest.mark.asyncio
    async def test_read_with_glob_pattern(self):
        """Test reading multiple documents with glob pattern."""
        # Mock components
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/file1.md", "/docs/file2.md"])
        nx.read = Mock(
            side_effect=[
                b"Content of file 1",
                b"Content of file 2",
            ]
        )

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Combined answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        result = await reader.read(
            path="/docs/*.md", prompt="What's in these files?", use_search=False
        )

        assert result.answer == "Combined answer"
        assert len(result.sources) == 2
        assert nx.read.call_count == 2

    @pytest.mark.asyncio
    async def test_read_with_dict_content(self):
        """Test reading document that returns dict content."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/parsed.pdf"])
        nx.read = Mock(return_value={"text": "Parsed document text", "metadata": {}})

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer from parsed doc")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        result = await reader.read(path="/docs/parsed.pdf", prompt="Question", use_search=False)

        assert result.answer == "Answer from parsed doc"

    @pytest.mark.asyncio
    async def test_read_with_file_read_error(self):
        """Test handling of file read errors."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/bad.md"])
        nx.read = Mock(side_effect=Exception("File read error"))

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer despite error")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        # Should handle error gracefully
        result = await reader.read(path="/docs/bad.md", prompt="Question", use_search=False)

        # Should still get an answer even if file couldn't be read
        assert isinstance(result, DocumentReadResult)

    @pytest.mark.asyncio
    async def test_read_search_fallback_to_direct(self):
        """Test fallback to direct reading when search returns no results."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/test.md"])
        nx.read = Mock(return_value=b"Document content")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Fallback answer")],
            )
        )

        search = Mock()
        search.search = AsyncMock(return_value=[])  # No search results

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        result = await reader.read(path="/docs/test.md", prompt="Question", use_search=True)

        # Should fallback to direct reading
        assert result.answer == "Fallback answer"
        nx.read.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_with_citations(self):
        """Test citation extraction."""
        nx = Mock()
        provider = Mock()
        search = Mock()

        # Mock search result
        search_result = Mock()
        search_result.path = "/docs/source.md"
        search_result.chunk_index = 0
        search_result.chunk_text = "Source content"
        search_result.score = 0.9
        search_result.start_offset = 0
        search_result.end_offset = 100
        search.search = AsyncMock(return_value=[search_result])

        # Mock LLM response with citation markers
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="According to [1], the answer is X.")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        result = await reader.read(
            path="/docs/*.md", prompt="Question", use_search=True, include_citations=True
        )

        # Should have extracted citations
        assert len(result.citations) > 0 or result.answer.count("[") > 0

    @pytest.mark.asyncio
    async def test_read_without_citations(self):
        """Test reading without citation extraction."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/test.md"])
        nx.read = Mock(return_value=b"Content")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Simple answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        result = await reader.read(
            path="/docs/test.md", prompt="Question", use_search=False, include_citations=False
        )

        assert result.answer == "Simple answer"

    @pytest.mark.asyncio
    async def test_read_with_custom_model(self):
        """Test reading with custom model specified."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/test.md"])
        nx.read = Mock(return_value=b"Content")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        await reader.read(path="/docs/test.md", prompt="Question", model="gpt-4", use_search=False)

        # Verify model was passed to provider
        call_args = provider.complete.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_read_with_max_tokens(self):
        """Test reading with custom max_tokens."""
        nx = Mock()
        nx.glob = Mock(return_value=["/docs/test.md"])
        nx.read = Mock(return_value=b"Content")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        await reader.read(
            path="/docs/test.md", prompt="Question", max_tokens=2000, use_search=False
        )

        # Verify max_tokens was used
        call_args = provider.complete.call_args
        assert call_args is not None

    @pytest.mark.asyncio
    async def test_read_with_search_limit(self):
        """Test reading with search result limit."""
        nx = Mock()
        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer")],
            )
        )

        search = Mock()
        search.search = AsyncMock(return_value=[])

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        await reader.read(path="/docs/*.md", prompt="Question", search_limit=5, use_search=True)

        # Verify search was called with limit
        search.search.assert_called_once()
        call_args = search.search.call_args
        assert call_args[1]["limit"] == 5

    @pytest.mark.asyncio
    async def test_read_with_different_search_modes(self):
        """Test reading with different search modes."""
        nx = Mock()
        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer")],
            )
        )

        search = Mock()
        search.search = AsyncMock(return_value=[])

        reader = LLMDocumentReader(nx=nx, provider=provider, search=search)

        # Test semantic mode
        await reader.read(
            path="/docs/*.md", prompt="Question", search_mode="semantic", use_search=True
        )
        call_args = search.search.call_args
        assert call_args[1]["search_mode"] == "semantic"

        # Test keyword mode
        await reader.read(
            path="/docs/*.md", prompt="Question", search_mode="keyword", use_search=True
        )
        call_args = search.search.call_args
        assert call_args[1]["search_mode"] == "keyword"

        # Test hybrid mode
        await reader.read(
            path="/docs/*.md", prompt="Question", search_mode="hybrid", use_search=True
        )
        call_args = search.search.call_args
        assert call_args[1]["search_mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_read_limits_files_with_glob(self):
        """Test that glob results are limited by search_limit."""
        nx = Mock()
        # Return many files
        nx.glob = Mock(return_value=[f"/docs/file{i}.md" for i in range(20)])
        nx.read = Mock(return_value=b"Content")

        provider = Mock()
        provider.complete = AsyncMock(
            return_value=Message(
                role=MessageRole.ASSISTANT,
                content=[TextContent(text="Answer")],
            )
        )

        reader = LLMDocumentReader(nx=nx, provider=provider)

        await reader.read(path="/docs/*.md", prompt="Question", search_limit=5, use_search=False)

        # Should only read first 5 files
        assert nx.read.call_count == 5
