"""Unit tests for LLMService.

Tests initialization, provider cache, LLM reader creation,
and error handling for missing dependencies.

All async service methods are tested via asyncio.run().
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.services.llm.llm_service import LLMService

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_nexus_fs():
    """Create a mock NexusFS for LLM service."""
    fs = MagicMock()
    fs.config = MagicMock()
    fs.config.oauth = None
    return fs


@pytest.fixture
def mock_search_engine():
    """Create a mock semantic search engine."""
    engine = MagicMock()
    engine.search = AsyncMock(return_value=[])
    return engine


@pytest.fixture
def service(mock_nexus_fs):
    """Create an LLMService with mock nexus_fs."""
    return LLMService(nexus_fs=mock_nexus_fs)


@pytest.fixture
def service_with_search(mock_nexus_fs, mock_search_engine):
    """Create an LLMService with mock nexus_fs and search engine."""
    return LLMService(nexus_fs=mock_nexus_fs, semantic_search_engine=mock_search_engine)


# =============================================================================
# Initialization
# =============================================================================


class TestLLMServiceInit:
    """Tests for LLMService construction."""

    def test_init_stores_nexus_fs(self, mock_nexus_fs):
        """Service stores nexus_fs dependency."""
        svc = LLMService(nexus_fs=mock_nexus_fs)
        assert svc.nexus_fs is mock_nexus_fs

    def test_init_creates_empty_provider_cache(self, mock_nexus_fs):
        """Provider cache is initialized as empty dict."""
        svc = LLMService(nexus_fs=mock_nexus_fs)
        assert len(svc._provider_cache) == 0

    def test_init_stores_semantic_search_engine(self, mock_nexus_fs, mock_search_engine):
        """Semantic search engine is stored when provided."""
        svc = LLMService(nexus_fs=mock_nexus_fs, semantic_search_engine=mock_search_engine)
        assert svc._semantic_search_engine is mock_search_engine

    def test_init_defaults_search_engine_to_none(self, mock_nexus_fs):
        """Semantic search engine defaults to None."""
        svc = LLMService(nexus_fs=mock_nexus_fs)
        assert svc._semantic_search_engine is None

    def test_init_minimal(self):
        """Service can be created with no arguments."""
        svc = LLMService()
        assert svc.nexus_fs is None
        assert len(svc._provider_cache) == 0
        assert svc._semantic_search_engine is None


# =============================================================================
# Provider cache
# =============================================================================


class TestProviderCache:
    """Tests for provider caching in _get_llm_reader."""

    def test_provider_cache_populated_on_reader_creation(self, service):
        """Creating a reader populates the provider cache."""
        with (
            patch("nexus.services.llm.llm_document_reader.LLMDocumentReader"),
            patch("nexus.bricks.llm.provider.LiteLLMProvider"),
            patch("nexus.bricks.llm.config.LLMConfig"),
        ):
            service._get_llm_reader(model="claude-sonnet-4")
            assert len(service._provider_cache) == 1

    def test_provider_cache_reuses_existing(self, service):
        """Subsequent calls with same config reuse cached provider."""
        mock_provider = MagicMock()
        cache_key = "claude-sonnet-4:" + str(hash(None))
        service._provider_cache[cache_key] = mock_provider

        with patch("nexus.services.llm.llm_document_reader.LLMDocumentReader") as mock_reader_cls:
            service._get_llm_reader(model="claude-sonnet-4")
            # Should use the cached provider, not create a new one
            call_kwargs = mock_reader_cls.call_args
            assert call_kwargs[1]["provider"] is mock_provider


# =============================================================================
# _get_llm_reader error handling
# =============================================================================


class TestGetLLMReader:
    """Tests for _get_llm_reader helper."""

    def test_raises_when_nexus_fs_is_none(self):
        """Raises RuntimeError when nexus_fs is not configured."""
        svc = LLMService()
        with pytest.raises(RuntimeError, match="NexusFS not configured"):
            svc._get_llm_reader()

    def test_creates_reader_with_nexus_fs(self, service):
        """Successfully creates reader when nexus_fs is set."""
        with (
            patch("nexus.services.llm.llm_document_reader.LLMDocumentReader") as mock_reader_cls,
            patch("nexus.bricks.llm.provider.LiteLLMProvider"),
            patch("nexus.bricks.llm.config.LLMConfig"),
        ):
            service._get_llm_reader()
            mock_reader_cls.assert_called_once()

    def test_passes_custom_provider(self, service):
        """Custom provider is passed directly without caching."""
        custom_provider = MagicMock()
        with patch("nexus.services.llm.llm_document_reader.LLMDocumentReader") as mock_reader_cls:
            service._get_llm_reader(provider=custom_provider)
            call_kwargs = mock_reader_cls.call_args[1]
            assert call_kwargs["provider"] is custom_provider

    def test_passes_system_prompt(self, service):
        """Custom system prompt is forwarded to reader."""
        with (
            patch("nexus.services.llm.llm_document_reader.LLMDocumentReader") as mock_reader_cls,
            patch("nexus.bricks.llm.provider.LiteLLMProvider"),
            patch("nexus.bricks.llm.config.LLMConfig"),
        ):
            service._get_llm_reader(system_prompt="You are a helpful expert.")
            call_kwargs = mock_reader_cls.call_args[1]
            assert call_kwargs["system_prompt"] == "You are a helpful expert."

    def test_passes_max_context_tokens(self, service):
        """Custom max_context_tokens is forwarded to reader."""
        with (
            patch("nexus.services.llm.llm_document_reader.LLMDocumentReader") as mock_reader_cls,
            patch("nexus.bricks.llm.provider.LiteLLMProvider"),
            patch("nexus.bricks.llm.config.LLMConfig"),
        ):
            service._get_llm_reader(max_context_tokens=5000)
            call_kwargs = mock_reader_cls.call_args[1]
            assert call_kwargs["max_context_tokens"] == 5000

    def test_openrouter_model_uses_custom_provider(self, service):
        """OpenRouter models (anthropic/*) use custom_llm_provider='openrouter'."""
        with (
            patch("nexus.services.llm.llm_document_reader.LLMDocumentReader"),
            patch("nexus.bricks.llm.provider.LiteLLMProvider"),
            patch("nexus.bricks.llm.config.LLMConfig") as mock_config,
        ):
            service._get_llm_reader(model="anthropic/claude-3-opus")
            call_kwargs = mock_config.call_args[1]
            assert call_kwargs.get("custom_llm_provider") == "openrouter"


# =============================================================================
# llm_read
# =============================================================================


class TestLLMRead:
    """Tests for the llm_read method."""

    def test_returns_answer_string(self, service):
        """llm_read returns the answer string from the reader result."""
        mock_result = MagicMock()
        mock_result.answer = "The answer is 42."
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=mock_result)

        with patch.object(service, "_get_llm_reader", return_value=mock_reader):
            answer = asyncio.run(service.llm_read(path="/test.txt", prompt="What is the answer?"))
            assert answer == "The answer is 42."

    def test_passes_parameters_to_reader(self, service):
        """llm_read forwards all parameters to reader.read()."""
        mock_result = MagicMock()
        mock_result.answer = "result"
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=mock_result)

        with patch.object(service, "_get_llm_reader", return_value=mock_reader):
            asyncio.run(
                service.llm_read(
                    path="/docs/*.md",
                    prompt="Summarize",
                    model="claude-opus-4",
                    max_tokens=2000,
                    use_search=False,
                    search_mode="keyword",
                )
            )
            mock_reader.read.assert_called_once_with(
                path="/docs/*.md",
                prompt="Summarize",
                model="claude-opus-4",
                max_tokens=2000,
                use_search=False,
                search_mode="keyword",
                context=None,
            )


# =============================================================================
# llm_read_detailed
# =============================================================================


class TestLLMReadDetailed:
    """Tests for the llm_read_detailed method."""

    def test_returns_full_result(self, service):
        """llm_read_detailed returns the full DocumentReadResult."""
        mock_result = MagicMock()
        mock_result.answer = "Detailed answer"
        mock_result.citations = []
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=mock_result)

        with patch.object(service, "_get_llm_reader", return_value=mock_reader):
            result = asyncio.run(service.llm_read_detailed(path="/test.txt", prompt="Explain"))
            assert result is mock_result

    def test_passes_citation_params(self, service):
        """llm_read_detailed forwards include_citations and search_limit."""
        mock_result = MagicMock()
        mock_reader = MagicMock()
        mock_reader.read = AsyncMock(return_value=mock_result)

        with patch.object(service, "_get_llm_reader", return_value=mock_reader):
            asyncio.run(
                service.llm_read_detailed(
                    path="/test.txt",
                    prompt="Q",
                    include_citations=True,
                    search_limit=20,
                )
            )
            call_kwargs = mock_reader.read.call_args[1]
            assert call_kwargs["include_citations"] is True
            assert call_kwargs["search_limit"] == 20


# =============================================================================
# create_llm_reader
# =============================================================================


class TestCreateLLMReader:
    """Tests for the create_llm_reader factory method."""

    def test_returns_reader_instance(self, service):
        """create_llm_reader returns an LLMDocumentReader."""
        mock_reader = MagicMock()
        with patch.object(service, "_get_llm_reader", return_value=mock_reader):
            result = service.create_llm_reader(model="claude-sonnet-4")
            assert result is mock_reader

    def test_passes_all_params(self, service):
        """create_llm_reader forwards all kwargs to _get_llm_reader."""
        custom_provider = MagicMock()
        with patch.object(service, "_get_llm_reader") as mock_get:
            service.create_llm_reader(
                provider=custom_provider,
                model="claude-opus-4",
                api_key="sk-test",
                system_prompt="Be precise.",
                max_context_tokens=8000,
            )
            mock_get.assert_called_once_with(
                provider=custom_provider,
                model="claude-opus-4",
                api_key="sk-test",
                system_prompt="Be precise.",
                max_context_tokens=8000,
            )
