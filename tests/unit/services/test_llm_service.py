"""Tests for LLMService (Issue #1287, Decision 9A).

Tests cover:
- Initialization with various dependency configurations
- create_llm_reader: requires filesystem
- llm_read: mocked provider returns answer
- llm_read_detailed: returns full DocumentReadResult
- Error handling: missing filesystem, provider errors
- RPC decorators on public methods
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.llm_service import LLMService


class TestLLMServiceInit:
    """Test LLMService initialization."""

    def test_init_with_none(self):
        """Test service initialization with no dependencies."""
        service = LLMService(nexus_fs=None)
        assert service.nexus_fs is None

    def test_init_with_nexus_fs(self):
        """Test initialization with nexus_fs."""
        mock_fs = MagicMock()
        service = LLMService(nexus_fs=mock_fs)
        assert service.nexus_fs is mock_fs


class TestLLMServiceCreateReader:
    """Test LLMService.create_llm_reader() method."""

    def test_create_reader_requires_filesystem(self):
        """Test that create_llm_reader raises without filesystem."""
        service = LLMService(nexus_fs=None)
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            service.create_llm_reader()

    def test_create_reader_with_defaults(self):
        """Test creating reader with default configuration via _get_llm_reader."""
        mock_fs = MagicMock()
        mock_fs._semantic_search = None
        service = LLMService(nexus_fs=mock_fs)

        mock_reader = MagicMock()
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        reader = service.create_llm_reader()

        assert reader is mock_reader
        service._get_llm_reader.assert_called_once()

    def test_create_reader_passes_custom_params(self):
        """Test that create_reader forwards params to _get_llm_reader."""
        mock_fs = MagicMock()
        service = LLMService(nexus_fs=mock_fs)

        mock_reader = MagicMock()
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        service.create_llm_reader(
            model="claude-opus-4",
            system_prompt="custom",
            max_context_tokens=5000,
        )

        call_kwargs = service._get_llm_reader.call_args[1]
        assert call_kwargs["model"] == "claude-opus-4"
        assert call_kwargs["system_prompt"] == "custom"
        assert call_kwargs["max_context_tokens"] == 5000


class TestLLMServiceRead:
    """Test LLMService.llm_read() method."""

    @pytest.mark.asyncio
    async def test_llm_read_returns_answer(self):
        """Test llm_read returns answer string from reader."""
        mock_result = MagicMock()
        mock_result.answer = "The key findings are..."

        mock_reader = AsyncMock()
        mock_reader.read.return_value = mock_result

        service = LLMService(nexus_fs=MagicMock())
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        answer = await service.llm_read(path="/report.pdf", prompt="What are the findings?")

        assert answer == "The key findings are..."
        mock_reader.read.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_read_passes_parameters(self):
        """Test llm_read forwards all parameters to reader."""
        mock_result = MagicMock()
        mock_result.answer = "answer"
        mock_reader = AsyncMock()
        mock_reader.read.return_value = mock_result

        service = LLMService(nexus_fs=MagicMock())
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        await service.llm_read(
            path="/docs/*.md",
            prompt="question",
            model="claude-opus-4",
            max_tokens=2000,
            search_mode="hybrid",
        )

        call_kwargs = mock_reader.read.call_args[1]
        assert call_kwargs["path"] == "/docs/*.md"
        assert call_kwargs["prompt"] == "question"
        assert call_kwargs["model"] == "claude-opus-4"
        assert call_kwargs["max_tokens"] == 2000
        assert call_kwargs["search_mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_llm_read_raises_on_no_fs(self):
        """Test llm_read raises when no filesystem configured."""
        service = LLMService(nexus_fs=None)

        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            await service.llm_read(path="/test.txt", prompt="test")


class TestLLMServiceReadDetailed:
    """Test LLMService.llm_read_detailed() method."""

    @pytest.mark.asyncio
    async def test_llm_read_detailed_returns_result(self):
        """Test llm_read_detailed returns full DocumentReadResult."""
        mock_result = MagicMock()
        mock_result.answer = "detailed answer"
        mock_result.citations = [MagicMock(path="/doc.md", score=0.95)]

        mock_reader = AsyncMock()
        mock_reader.read.return_value = mock_result

        service = LLMService(nexus_fs=MagicMock())
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        result = await service.llm_read_detailed(
            path="/docs/**/*.md",
            prompt="How does auth work?",
            include_citations=True,
        )

        assert result.answer == "detailed answer"
        assert len(result.citations) == 1

    @pytest.mark.asyncio
    async def test_llm_read_detailed_passes_search_limit(self):
        """Test search_limit is forwarded to reader."""
        mock_result = MagicMock()
        mock_reader = AsyncMock()
        mock_reader.read.return_value = mock_result

        service = LLMService(nexus_fs=MagicMock())
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        await service.llm_read_detailed(path="/test.txt", prompt="q", search_limit=20)

        call_kwargs = mock_reader.read.call_args[1]
        assert call_kwargs["search_limit"] == 20


class TestLLMServiceStream:
    """Test LLMService.llm_read_stream() method."""

    @pytest.mark.asyncio
    async def test_llm_read_stream_yields_chunks(self):
        """Test llm_read_stream yields chunks from reader."""

        async def mock_stream(**kwargs):
            for chunk in ["Hello", " world", "!"]:
                yield chunk

        mock_reader = MagicMock()
        mock_reader.stream = mock_stream

        service = LLMService(nexus_fs=MagicMock())
        service._get_llm_reader = MagicMock(return_value=mock_reader)

        chunks = []
        async for chunk in service.llm_read_stream(path="/test.txt", prompt="summarize"):
            chunks.append(chunk)

        assert chunks == ["Hello", " world", "!"]


class TestLLMServiceRPCMethods:
    """Test that LLMService methods have @rpc_expose decorators."""

    def test_llm_read_is_rpc_exposed(self):
        service = LLMService(nexus_fs=None)
        assert hasattr(service.llm_read, "_rpc_exposed")

    def test_llm_read_detailed_is_rpc_exposed(self):
        service = LLMService(nexus_fs=None)
        assert hasattr(service.llm_read_detailed, "_rpc_exposed")

    def test_llm_read_stream_is_rpc_exposed(self):
        service = LLMService(nexus_fs=None)
        assert hasattr(service.llm_read_stream, "_rpc_exposed")

    def test_create_llm_reader_is_rpc_exposed(self):
        service = LLMService(nexus_fs=None)
        assert hasattr(service.create_llm_reader, "_rpc_exposed")
