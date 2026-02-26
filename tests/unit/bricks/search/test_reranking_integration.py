"""Unit tests for reranker integration in daemon.py.

Tests cover:
- DaemonConfig reranking defaults
- Reranker initialization failure graceful degradation
- _rerank_results top_k limiting, error fallback, empty input
- get_pipeline_config_from_env environment variable reading
- API-based reranker providers (Jina, Cohere) — mock httpx
- Factory routing for ModelProvider.API
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.daemon import DaemonConfig, SearchDaemon, get_pipeline_config_from_env

# Only run anyio tests with asyncio backend (trio not installed)
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio tests to asyncio backend only (trio not installed)."""
    return "asyncio"


# =============================================================================
# DaemonConfig defaults
# =============================================================================


class TestRerankerConfigDefaults:
    """Test DaemonConfig reranking default values."""

    def test_reranker_config_defaults(self) -> None:
        """DaemonConfig reranking defaults are all False/sensible."""
        config = DaemonConfig()

        assert config.reranking_enabled is False
        assert config.query_expansion_enabled is False
        assert config.reranker_provider == "local"
        assert config.reranker_model == "jina-tiny"
        assert config.reranking_top_k == 30
        assert config.position_aware_blending is True
        assert config.scored_chunking_enabled is False


# =============================================================================
# Reranker init failure
# =============================================================================


def _make_daemon_with_reranker(mock_reranker: AsyncMock, top_k: int = 3) -> SearchDaemon:
    """Create a daemon with a mock reranker attached."""
    config = DaemonConfig(reranking_enabled=True, reranking_top_k=top_k)
    daemon = SearchDaemon(config)
    daemon._reranker = mock_reranker
    return daemon


async def test_reranker_init_failure_degrades() -> None:
    """When reranker init fails, runtime flag disables reranking (config unchanged)."""
    config = DaemonConfig(reranking_enabled=True)
    daemon = SearchDaemon(config)

    mock_chunker_cls = MagicMock(return_value=MagicMock())
    mock_pipeline_cls = MagicMock(return_value=MagicMock())

    # Mock out the heavy startup methods so we only test reranker init
    with (
        patch.object(daemon, "_init_bm25s_index", new_callable=AsyncMock),
        patch.object(daemon, "_init_database_pool", new_callable=AsyncMock),
        patch.object(daemon, "_check_zoekt", new_callable=AsyncMock),
        patch.object(daemon, "_check_embedding_cache", new_callable=AsyncMock),
        patch(
            "nexus.bricks.search.chunking.DocumentChunker",
            mock_chunker_cls,
        ),
        patch(
            "nexus.bricks.search.indexing.IndexingPipeline",
            mock_pipeline_cls,
        ),
        patch(
            "nexus.bricks.search.mobile_config.RERANKER_MODELS",
            {"jina-tiny": {"name": "test"}},
        ),
        patch(
            "nexus.bricks.search.mobile_providers.create_reranker_provider",
            new_callable=AsyncMock,
            side_effect=RuntimeError("GPU not available"),
        ),
    ):
        await daemon.startup()

    # Config is NOT mutated — still reflects original intent
    assert config.reranking_enabled is True
    # Runtime flag is False — reranking degraded gracefully
    assert daemon._reranking_active is False
    assert daemon._reranker is None

    # Clean up
    daemon._initialized = False


# =============================================================================
# _rerank_results
# =============================================================================


async def test_rerank_results_top_k() -> None:
    """Only top reranking_top_k candidates are sent to reranker; tail is preserved."""
    mock_reranker = AsyncMock()
    # rerank returns (original_index, score) tuples
    mock_reranker.rerank.return_value = [
        (0, 0.95),
        (1, 0.80),
        (2, 0.60),
    ]

    daemon = _make_daemon_with_reranker(mock_reranker, top_k=3)

    # Pass 5 results but top_k=3, so only first 3 go to reranker
    # Use path:chunk_index as key (id_key=None in _rerank_results)
    results = [
        {
            "path": f"/file{i}.py",
            "chunk_index": 0,
            "score": 1.0 - i * 0.1,
            "chunk_text": f"text {i}",
        }
        for i in range(5)
    ]

    reranked, scores = await daemon._rerank_results(results, "test query")

    # Reranker should receive exactly 3 documents (top_k=3)
    call_args = mock_reranker.rerank.call_args
    documents_arg = call_args[0][1]  # Second positional arg
    assert len(documents_arg) == 3

    # Returned results: top_k candidates + tail (5 total, 3 reranked + 2 tail)
    assert len(reranked) == 5
    assert len(scores) == 3
    # Tail results (indices 3-4) are preserved in original order
    assert reranked[3]["path"] == "/file3.py"
    assert reranked[4]["path"] == "/file4.py"


async def test_rerank_results_error_fallback() -> None:
    """When reranker raises an error, original results are returned."""
    mock_reranker = AsyncMock()
    mock_reranker.rerank.side_effect = RuntimeError("Model crashed")

    daemon = _make_daemon_with_reranker(mock_reranker, top_k=3)

    results = [
        {"path": "/file0.py", "chunk_index": 0, "score": 0.9, "chunk_text": "text 0"},
        {"path": "/file1.py", "chunk_index": 0, "score": 0.8, "chunk_text": "text 1"},
    ]

    reranked, scores = await daemon._rerank_results(results, "test query")

    # Should return original results unchanged
    assert reranked == results
    assert scores == {}


async def test_rerank_results_empty() -> None:
    """Empty results returns empty tuple."""
    mock_reranker = AsyncMock()
    daemon = _make_daemon_with_reranker(mock_reranker, top_k=3)

    reranked, scores = await daemon._rerank_results([], "test query")

    assert reranked == []
    assert scores == {}
    # Reranker should not be called
    mock_reranker.rerank.assert_not_called()


# =============================================================================
# get_pipeline_config_from_env
# =============================================================================


class TestPipelineConfigFromEnv:
    """Tests for get_pipeline_config_from_env()."""

    def test_pipeline_config_from_env(self) -> None:
        """get_pipeline_config_from_env reads env vars correctly."""
        env_vars = {
            "NEXUS_SEARCH_EXPANSION_ENABLED": "true",
            "NEXUS_SEARCH_EXPANSION_PROVIDER": "local",
            "NEXUS_SEARCH_EXPANSION_MODEL": "my-model.gguf",
            "NEXUS_SEARCH_RERANKING_ENABLED": "1",
            "NEXUS_SEARCH_RERANKER_MODEL": "bge-base",
            "NEXUS_SEARCH_RERANKING_TOP_K": "50",
            "NEXUS_SEARCH_POSITION_BLENDING": "false",
            "NEXUS_SEARCH_SCORED_CHUNKING": "yes",
        }

        with patch.dict(os.environ, env_vars, clear=False):
            config = get_pipeline_config_from_env()

        assert config["query_expansion_enabled"] is True
        assert config["expansion_provider"] == "local"
        assert config["expansion_model"] == "my-model.gguf"
        assert config["reranking_enabled"] is True
        assert config["reranker_model"] == "bge-base"
        assert config["reranking_top_k"] == 50
        assert config["position_aware_blending"] is False
        assert config["scored_chunking_enabled"] is True

    def test_pipeline_config_defaults(self) -> None:
        """Defaults are used when env vars are not set (no API keys → local)."""
        # Clear all relevant env vars including API keys
        keys_to_remove = [
            "NEXUS_SEARCH_EXPANSION_ENABLED",
            "NEXUS_SEARCH_EXPANSION_PROVIDER",
            "NEXUS_SEARCH_EXPANSION_MODEL",
            "NEXUS_SEARCH_RERANKING_ENABLED",
            "NEXUS_SEARCH_RERANKER_PROVIDER",
            "NEXUS_SEARCH_RERANKER_MODEL",
            "NEXUS_SEARCH_RERANKING_TOP_K",
            "NEXUS_SEARCH_POSITION_BLENDING",
            "NEXUS_SEARCH_SCORED_CHUNKING",
            "JINA_API_KEY",
            "COHERE_API_KEY",
        ]
        cleaned_env = {k: v for k, v in os.environ.items() if k not in keys_to_remove}

        with patch.dict(os.environ, cleaned_env, clear=True):
            config = get_pipeline_config_from_env()

        assert config["query_expansion_enabled"] is False
        assert config["expansion_provider"] == "openrouter"
        assert config["expansion_model"] == "deepseek/deepseek-chat"
        assert config["reranking_enabled"] is False
        assert config["reranker_provider"] == "local"
        assert config["reranker_model"] == "jina-tiny"
        assert config["reranking_top_k"] == 30
        assert config["position_aware_blending"] is True
        assert config["scored_chunking_enabled"] is False

    def test_pipeline_config_auto_detects_jina_key(self) -> None:
        """When JINA_API_KEY is set, auto-detect defaults to jina provider."""
        keys_to_remove = [
            "NEXUS_SEARCH_RERANKER_PROVIDER",
            "NEXUS_SEARCH_RERANKER_MODEL",
            "COHERE_API_KEY",
        ]
        cleaned_env = {k: v for k, v in os.environ.items() if k not in keys_to_remove}
        cleaned_env["JINA_API_KEY"] = "test-key"

        with patch.dict(os.environ, cleaned_env, clear=True):
            config = get_pipeline_config_from_env()

        assert config["reranker_provider"] == "jina"
        assert config["reranker_model"] == "jina-reranker-v3"

    def test_pipeline_config_auto_detects_cohere_key(self) -> None:
        """When COHERE_API_KEY is set (no JINA), auto-detect defaults to cohere."""
        keys_to_remove = [
            "NEXUS_SEARCH_RERANKER_PROVIDER",
            "NEXUS_SEARCH_RERANKER_MODEL",
            "JINA_API_KEY",
        ]
        cleaned_env = {k: v for k, v in os.environ.items() if k not in keys_to_remove}
        cleaned_env["COHERE_API_KEY"] = "test-key"

        with patch.dict(os.environ, cleaned_env, clear=True):
            config = get_pipeline_config_from_env()

        assert config["reranker_provider"] == "cohere"
        assert config["reranker_model"] == "cohere-rerank-v3.5"


# =============================================================================
# API Reranker Providers (Jina + Cohere) — unit tests with mocked httpx
# =============================================================================


class TestJinaAPIRerankerProvider:
    """Tests for JinaAPIRerankerProvider."""

    async def test_jina_reranker_success(self) -> None:
        """Jina API reranker parses response correctly."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import JinaAPIRerankerProvider

        config = RERANKER_MODELS["jina-reranker-v3"]

        with patch.dict(os.environ, {"JINA_API_KEY": "test-key"}):
            provider = JinaAPIRerankerProvider(config)

        # Mock httpx client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.95},
                {"index": 0, "relevance_score": 0.70},
                {"index": 2, "relevance_score": 0.50},
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        provider._client = mock_client
        provider._loaded = True

        results = await provider.rerank("test query", ["doc0", "doc1", "doc2"], top_k=2)

        # Should sort by score descending
        assert results[0] == (1, 0.95)
        assert results[1] == (0, 0.70)
        assert results[2] == (2, 0.50)

        # Verify API call format
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["model"] == config.name
        assert payload["query"] == "test query"
        assert payload["documents"] == ["doc0", "doc1", "doc2"]
        assert payload["top_n"] == 2
        assert payload["return_documents"] is False

    async def test_jina_reranker_timeout_degrades(self) -> None:
        """Jina API timeout returns empty list (graceful degradation)."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import JinaAPIRerankerProvider

        config = RERANKER_MODELS["jina-reranker-v3"]

        with patch.dict(os.environ, {"JINA_API_KEY": "test-key"}):
            provider = JinaAPIRerankerProvider(config)

        import httpx

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("Connection timed out")

        provider._client = mock_client
        provider._loaded = True

        results = await provider.rerank("test query", ["doc0", "doc1"])

        assert results == []

    async def test_jina_reranker_missing_key_skips(self) -> None:
        """Without JINA_API_KEY, load() raises RuntimeError."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import JinaAPIRerankerProvider

        config = RERANKER_MODELS["jina-reranker-v3"]

        with patch.dict(os.environ, {}, clear=True):
            provider = JinaAPIRerankerProvider(config)

        with pytest.raises(RuntimeError, match="JINA_API_KEY not set"):
            await provider.load()

    async def test_jina_reranker_empty_docs(self) -> None:
        """Empty documents list returns empty result without API call."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import JinaAPIRerankerProvider

        config = RERANKER_MODELS["jina-reranker-v3"]

        with patch.dict(os.environ, {"JINA_API_KEY": "test-key"}):
            provider = JinaAPIRerankerProvider(config)

        mock_client = AsyncMock()
        provider._client = mock_client
        provider._loaded = True

        results = await provider.rerank("test query", [])
        assert results == []
        mock_client.post.assert_not_called()


class TestCohereAPIRerankerProvider:
    """Tests for CohereAPIRerankerProvider."""

    async def test_cohere_reranker_success(self) -> None:
        """Cohere API reranker parses response correctly."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import CohereAPIRerankerProvider

        config = RERANKER_MODELS["cohere-rerank-v3.5"]

        with patch.dict(os.environ, {"COHERE_API_KEY": "test-key"}):
            provider = CohereAPIRerankerProvider(config)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {
            "results": [
                {"index": 2, "relevance_score": 0.99},
                {"index": 0, "relevance_score": 0.85},
            ]
        }

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        provider._client = mock_client
        provider._loaded = True

        results = await provider.rerank("test query", ["doc0", "doc1", "doc2"])

        assert results[0] == (2, 0.99)
        assert results[1] == (0, 0.85)

        # Verify model name in payload
        call_kwargs = mock_client.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["model"] == "rerank-v3.5"
        assert payload["query"] == "test query"

    async def test_cohere_reranker_error_degrades(self) -> None:
        """Cohere API error returns empty list (graceful degradation)."""
        from nexus.bricks.search.mobile_config import RERANKER_MODELS
        from nexus.bricks.search.mobile_providers import CohereAPIRerankerProvider

        config = RERANKER_MODELS["cohere-rerank-v3.5"]

        with patch.dict(os.environ, {"COHERE_API_KEY": "test-key"}):
            provider = CohereAPIRerankerProvider(config)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("Internal Server Error")

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response

        provider._client = mock_client
        provider._loaded = True

        results = await provider.rerank("test query", ["doc0"])
        assert results == []


# =============================================================================
# Factory routing for ModelProvider.API
# =============================================================================


class TestFactoryAPIRouting:
    """Tests for _get_reranker_provider_class with API providers."""

    def test_factory_routes_api_to_jina(self) -> None:
        """ModelProvider.API + api_provider=jina routes to JinaAPIRerankerProvider."""
        from nexus.bricks.search.mobile_config import ModelProvider, RerankerModelConfig
        from nexus.bricks.search.mobile_providers import (
            JinaAPIRerankerProvider,
            _get_reranker_provider_class,
        )

        config = RerankerModelConfig(
            name="test",
            provider=ModelProvider.API,
            size_mb=0,
            metadata={"api_provider": "jina"},
        )
        assert _get_reranker_provider_class(config) is JinaAPIRerankerProvider

    def test_factory_routes_api_to_cohere(self) -> None:
        """ModelProvider.API + api_provider=cohere routes to CohereAPIRerankerProvider."""
        from nexus.bricks.search.mobile_config import ModelProvider, RerankerModelConfig
        from nexus.bricks.search.mobile_providers import (
            CohereAPIRerankerProvider,
            _get_reranker_provider_class,
        )

        config = RerankerModelConfig(
            name="test",
            provider=ModelProvider.API,
            size_mb=0,
            metadata={"api_provider": "cohere"},
        )
        assert _get_reranker_provider_class(config) is CohereAPIRerankerProvider

    def test_factory_raises_on_unknown_api_provider(self) -> None:
        """ModelProvider.API with unknown api_provider raises ValueError."""
        from nexus.bricks.search.mobile_config import ModelProvider, RerankerModelConfig
        from nexus.bricks.search.mobile_providers import _get_reranker_provider_class

        config = RerankerModelConfig(
            name="test",
            provider=ModelProvider.API,
            size_mb=0,
            metadata={"api_provider": "unknown"},
        )
        with pytest.raises(ValueError, match="Unsupported API reranker: unknown"):
            _get_reranker_provider_class(config)
