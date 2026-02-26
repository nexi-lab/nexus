"""Unit tests for reranker integration in daemon.py.

Tests cover:
- DaemonConfig reranking defaults
- Reranker initialization failure graceful degradation
- _rerank_results top_k limiting, error fallback, empty input
- get_pipeline_config_from_env environment variable reading
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
        """Defaults are used when env vars are not set."""
        # Clear all relevant env vars
        keys_to_remove = [
            "NEXUS_SEARCH_EXPANSION_ENABLED",
            "NEXUS_SEARCH_EXPANSION_PROVIDER",
            "NEXUS_SEARCH_EXPANSION_MODEL",
            "NEXUS_SEARCH_RERANKING_ENABLED",
            "NEXUS_SEARCH_RERANKER_MODEL",
            "NEXUS_SEARCH_RERANKING_TOP_K",
            "NEXUS_SEARCH_POSITION_BLENDING",
            "NEXUS_SEARCH_SCORED_CHUNKING",
        ]
        cleaned_env = {k: v for k, v in os.environ.items() if k not in keys_to_remove}

        with patch.dict(os.environ, cleaned_env, clear=True):
            config = get_pipeline_config_from_env()

        assert config["query_expansion_enabled"] is False
        assert config["expansion_provider"] == "openrouter"
        assert config["expansion_model"] == "deepseek/deepseek-chat"
        assert config["reranking_enabled"] is False
        assert config["reranker_model"] == "jina-tiny"
        assert config["reranking_top_k"] == 30
        assert config["position_aware_blending"] is True
        assert config["scored_chunking_enabled"] is False
