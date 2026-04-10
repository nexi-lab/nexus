"""Tests for txtai backend reranker + daemon backend delegation (Issue #2663).

Covers:
- TxtaiBackend reranker init and search reranking
- Over-fetch when reranker is configured
- SearchDaemon._backend delegation
- Daemon timing tracking (rerank_ms, backend_ms)
- Graph search via daemon._backend
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.results import BaseSearchResult

txtai_backend = pytest.importorskip(
    "nexus.bricks.search.txtai_backend",
    reason="txtai_backend not available",
)
TxtaiBackend = txtai_backend.TxtaiBackend

daemon_mod = pytest.importorskip(
    "nexus.bricks.search.daemon",
    reason="daemon module not available",
)
DaemonConfig = daemon_mod.DaemonConfig
SearchDaemon = daemon_mod.SearchDaemon
SearchResult = daemon_mod.SearchResult


# =============================================================================
# TxtaiBackend reranker tests
# =============================================================================


class TestTxtaiReranker:
    """Test reranker wiring in TxtaiBackend."""

    def test_reranker_model_stored(self) -> None:
        backend = TxtaiBackend(reranker_model="cross-encoder/test")
        assert backend._reranker_model == "cross-encoder/test"
        assert backend._reranker is None  # Not initialized until startup

    def test_no_reranker_by_default(self) -> None:
        backend = TxtaiBackend()
        assert backend._reranker_model is None
        assert backend._reranker is None

    @pytest.mark.asyncio
    async def test_search_overfetches_when_reranker_set(self) -> None:
        """When reranker is configured, search should fetch 2x limit."""
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_emb.search.return_value = []
        backend._embeddings = mock_emb
        backend._reranker = MagicMock()  # Simulate loaded reranker

        await backend.search("test", zone_id="z", limit=10)

        call_sql = mock_emb.search.call_args[0][0]
        assert "LIMIT 20" in call_sql  # 10 * 2

    @pytest.mark.asyncio
    async def test_search_normal_limit_without_reranker(self) -> None:
        """Without reranker, search should use exact limit."""
        backend = TxtaiBackend()
        mock_emb = MagicMock()
        mock_emb.search.return_value = []
        backend._embeddings = mock_emb

        await backend.search("test", zone_id="z", limit=10)

        call_sql = mock_emb.search.call_args[0][0]
        assert "LIMIT 10" in call_sql

    @pytest.mark.asyncio
    async def test_rerank_results(self) -> None:
        """Test that reranker re-sorts results by cross-encoder score."""
        backend = TxtaiBackend(reranker_model="cross-encoder/test")
        mock_emb = MagicMock()
        mock_emb.search.return_value = [
            {"path": "/a.py", "text": "low quality", "score": 0.9},
            {"path": "/b.py", "text": "high quality", "score": 0.5},
            {"path": "/c.py", "text": "medium", "score": 0.7},
        ]
        backend._embeddings = mock_emb

        # Reranker returns (index, score) sorted by score desc
        mock_reranker = MagicMock()
        mock_reranker.return_value = [(1, 0.95), (2, 0.80), (0, 0.40)]
        backend._reranker = mock_reranker

        results = await backend.search("test", zone_id="z", limit=2)

        # Should be reranked: /b.py first (0.95), /c.py second (0.80)
        assert len(results) == 2
        assert results[0].path == "/b.py"
        assert results[0].reranker_score == 0.95
        assert results[1].path == "/c.py"
        assert results[1].reranker_score == 0.80
        assert backend.last_rerank_ms > 0

    @pytest.mark.asyncio
    async def test_rerank_empty_results(self) -> None:
        backend = TxtaiBackend(reranker_model="cross-encoder/test")
        mock_emb = MagicMock()
        mock_emb.search.return_value = []
        backend._embeddings = mock_emb
        backend._reranker = MagicMock()

        results = await backend.search("test", zone_id="z")
        assert results == []

    def test_sparse_config_stored(self) -> None:
        backend = TxtaiBackend(sparse=True)
        assert backend._sparse is True

        backend2 = TxtaiBackend(sparse="naver/splade-cocondenser-ensembledistil")
        assert backend2._sparse == "naver/splade-cocondenser-ensembledistil"


# =============================================================================
# SearchDaemon backend delegation tests
# =============================================================================


class TestDaemonBackendDelegation:
    """Test SearchDaemon delegates to _backend."""

    def test_daemon_has_backend_field(self) -> None:
        daemon = SearchDaemon()
        assert daemon._backend is None
        assert daemon.last_search_timing == {}

    def test_daemon_config_has_txtai_fields(self) -> None:
        config = DaemonConfig(
            txtai_model="nomic-ai/nomic-embed-text-v1.5",
            txtai_reranker="cross-encoder/ms-marco-MiniLM-L-2-v2",
            txtai_sparse=True,
            txtai_graph=False,
        )
        assert config.txtai_model == "nomic-ai/nomic-embed-text-v1.5"
        assert config.txtai_reranker == "cross-encoder/ms-marco-MiniLM-L-2-v2"
        assert config.txtai_sparse is True
        assert config.txtai_graph is False

    @pytest.mark.asyncio
    async def test_search_delegates_to_backend(self) -> None:
        """When _backend is set, search should delegate to it."""
        daemon = SearchDaemon()
        daemon._initialized = True

        mock_backend = AsyncMock()
        mock_backend.search.return_value = [
            BaseSearchResult(path="/a.py", chunk_text="hello", score=0.9),
        ]
        mock_backend.last_rerank_ms = 5.0
        daemon._backend = mock_backend

        results = await daemon.search("test query", zone_id="corp")

        mock_backend.search.assert_awaited_once()
        call_kwargs = mock_backend.search.call_args[1]
        assert call_kwargs["zone_id"] == "corp"
        assert call_kwargs["search_type"] == "hybrid"

        assert len(results) == 1
        assert results[0].path == "/a.py"
        assert isinstance(results[0], SearchResult)

    @pytest.mark.asyncio
    async def test_search_timing_tracked(self) -> None:
        daemon = SearchDaemon()
        daemon._initialized = True

        mock_backend = AsyncMock()
        mock_backend.search.return_value = []
        mock_backend.last_rerank_ms = 3.5
        daemon._backend = mock_backend

        await daemon.search("test", zone_id="z")

        assert "backend_ms" in daemon.last_search_timing
        assert daemon.last_search_timing["rerank_ms"] == 3.5

    @pytest.mark.asyncio
    async def test_keyword_tries_zoekt_first(self) -> None:
        """Keyword search should try Zoekt before txtai backend."""
        daemon = SearchDaemon()
        daemon._initialized = True
        daemon.stats.zoekt_available = True

        mock_zoekt = AsyncMock()
        mock_zoekt.is_available.return_value = True
        mock_zoekt.search.return_value = [
            MagicMock(file="/a.py", content="match", score=1.0, line=5),
        ]
        daemon._zoekt_client = mock_zoekt

        mock_backend = AsyncMock()
        daemon._backend = mock_backend

        results = await daemon.search("test", search_type="keyword", zone_id="z")

        # Zoekt was used, backend was not
        assert len(results) == 1
        mock_backend.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_keyword_falls_to_backend_when_no_zoekt(self) -> None:
        """When Zoekt unavailable, keyword search falls to txtai backend."""
        daemon = SearchDaemon()
        daemon._initialized = True
        daemon.stats.zoekt_available = False

        mock_backend = AsyncMock()
        mock_backend.search.return_value = [
            BaseSearchResult(path="/a.py", chunk_text="hello", score=0.8),
        ]
        mock_backend.last_rerank_ms = 0.0
        daemon._backend = mock_backend

        results = await daemon.search("test", search_type="keyword", zone_id="z")

        assert len(results) == 1
        mock_backend.search.assert_awaited_once()
        assert mock_backend.search.call_args[1]["search_type"] == "keyword"

    @pytest.mark.asyncio
    async def test_graph_search_finds_backend(self) -> None:
        """graph_search_service.py looks for daemon._backend — verify it exists."""
        daemon = SearchDaemon()
        daemon._initialized = True

        mock_backend = AsyncMock()
        mock_backend.graph_search.return_value = [
            BaseSearchResult(path="/a.py", chunk_text="entity", score=0.9),
        ]
        daemon._backend = mock_backend

        # Simulate what graph_search_service.py does
        backend = getattr(daemon, "_backend", None)
        assert backend is not None
        graph_search_fn = getattr(backend, "graph_search", None)
        assert graph_search_fn is not None

        results = await graph_search_fn("test", zone_id="corp", limit=5)
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_get_stats_includes_backend(self) -> None:
        daemon = SearchDaemon()
        stats = daemon.get_stats()
        assert stats["backend"] == "legacy"  # No backend set

        daemon._backend = MagicMock()
        stats = daemon.get_stats()
        assert stats["backend"] == "txtai"
        assert stats["txtai_model"] == "sentence-transformers/all-MiniLM-L6-v2"

    @pytest.mark.asyncio
    async def test_get_health_includes_backend(self) -> None:
        daemon = SearchDaemon()
        health = daemon.get_health()
        assert health["backend"] == "legacy"

        daemon._backend = MagicMock()
        health = daemon.get_health()
        assert health["backend"] == "txtai"

    @pytest.mark.asyncio
    async def test_shutdown_calls_backend_shutdown(self) -> None:
        daemon = SearchDaemon()
        daemon._initialized = True

        mock_backend = AsyncMock()
        daemon._backend = mock_backend

        await daemon.shutdown()
        mock_backend.shutdown.assert_awaited_once()
        assert daemon._backend is None
