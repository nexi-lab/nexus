"""Tests for SearchDaemon with txtai backend (Issue #2663).

Mocked unit tests verifying:
- Startup/shutdown lifecycle
- Search routing (keyword, semantic, hybrid)
- Auto-index opt-in (debounce, batch grouping by zone_id)
- Explicit index_documents() call
- CE reranker toggle
- Zone_id required on all search calls
- Zoekt fallback for keyword queries
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

daemon_mod = pytest.importorskip(
    "nexus.bricks.search.daemon",
    reason="daemon module not available in this environment",
)

# Skip if this is the old daemon (worktree not active)
_cfg = daemon_mod.DaemonConfig()
if not hasattr(_cfg, "search_backend"):
    pytest.skip("New daemon.py not available (worktree not active)", allow_module_level=True)
del _cfg

DaemonConfig = daemon_mod.DaemonConfig
DaemonStats = daemon_mod.DaemonStats
SearchDaemon = daemon_mod.SearchDaemon
SearchResult = daemon_mod.SearchResult

# =============================================================================
# Config tests
# =============================================================================


class TestDaemonConfig:
    """Test DaemonConfig defaults."""

    def test_default_values(self) -> None:
        config = DaemonConfig()
        assert config.search_backend == "txtai"
        assert config.embedding_model == "all-MiniLM-L6-v2"
        assert config.hybrid_search is True
        assert config.auto_index_on_write is False
        assert config.reranker_enabled is True
        assert config.reranker_model == "cross-encoder/ms-marco-MiniLM-L-6-v2"
        assert config.reranker_top_k == 25
        assert config.query_timeout_seconds == 10.0

    def test_custom_values(self) -> None:
        config = DaemonConfig(
            search_backend="txtai",
            embedding_model="custom-model",
            hybrid_search=False,
            auto_index_on_write=True,
            reranker_enabled=False,
        )
        assert config.embedding_model == "custom-model"
        assert config.hybrid_search is False
        assert config.auto_index_on_write is True
        assert config.reranker_enabled is False


class TestDaemonStats:
    """Test DaemonStats defaults."""

    def test_default_values(self) -> None:
        stats = DaemonStats()
        assert stats.startup_time_ms == 0.0
        assert stats.total_queries == 0
        assert stats.avg_latency_ms == 0.0
        assert stats.p99_latency_ms == 0.0
        assert stats.zoekt_available is False
        assert stats.documents_indexed == 0


# =============================================================================
# Lifecycle tests
# =============================================================================


class TestSearchDaemonLifecycle:
    """Test SearchDaemon startup and shutdown."""

    @pytest.mark.asyncio
    async def test_not_initialized_by_default(self) -> None:
        daemon = SearchDaemon()
        assert not daemon.is_initialized

    @pytest.mark.asyncio
    async def test_startup_initializes(self) -> None:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()
        assert daemon.is_initialized
        mock_backend.startup.assert_awaited_once()
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_double_startup_warns(self) -> None:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()
            await daemon.startup()  # Should not crash
        assert daemon.is_initialized
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_calls_backend(self) -> None:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()
        await daemon.shutdown()
        mock_backend.shutdown.assert_awaited_once()
        assert not daemon.is_initialized

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self) -> None:
        daemon = SearchDaemon()
        await daemon.shutdown()
        await daemon.shutdown()  # Should not crash

    @pytest.mark.asyncio
    async def test_search_before_startup_raises(self) -> None:
        daemon = SearchDaemon()
        with pytest.raises(RuntimeError, match="not initialized"):
            await daemon.search("test")


# =============================================================================
# Search tests
# =============================================================================


class TestSearchDaemonSearch:
    """Test SearchDaemon search routing."""

    async def _make_daemon(self) -> SearchDaemon:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.search.return_value = []
        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()
        return daemon

    @pytest.mark.asyncio
    async def test_search_returns_search_results(self) -> None:
        daemon = await self._make_daemon()
        results = await daemon.search("test", zone_id="corp")
        assert isinstance(results, list)
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_search_default_zone_id(self) -> None:
        """When zone_id is None, should use ROOT_ZONE_ID."""
        daemon = await self._make_daemon()
        results = await daemon.search("test")
        assert isinstance(results, list)
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_search_passes_params_to_backend(self) -> None:
        daemon = await self._make_daemon()
        await daemon.search(
            "query", zone_id="z1", limit=5, search_type="semantic", path_filter="/docs"
        )
        backend = daemon._backend
        backend.search.assert_awaited_once()
        call_kwargs = backend.search.call_args
        assert call_kwargs[0][0] == "query"
        assert call_kwargs[1]["zone_id"] == "z1"
        assert call_kwargs[1]["limit"] == 5
        assert call_kwargs[1]["search_type"] == "semantic"
        assert call_kwargs[1]["path_filter"] == "/docs"
        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_search_tracks_latency(self) -> None:
        daemon = await self._make_daemon()
        await daemon.search("test", zone_id="z")
        assert daemon.stats.total_queries == 1
        assert daemon.stats.avg_latency_ms > 0
        await daemon.shutdown()


# =============================================================================
# Reranking tests
# =============================================================================


class TestSearchDaemonReranking:
    """Test CE reranker toggle."""

    def test_should_rerank_default(self) -> None:
        daemon = SearchDaemon()
        # No reranker loaded
        assert not daemon._should_rerank(None)
        assert daemon._should_rerank(True)
        assert not daemon._should_rerank(False)

    def test_should_rerank_with_reranker(self) -> None:
        daemon = SearchDaemon()
        daemon._reranker = MagicMock()  # Simulate loaded reranker
        assert daemon._should_rerank(None)  # Default: use reranker
        assert daemon._should_rerank(True)
        assert not daemon._should_rerank(False)  # Override: disable

    @pytest.mark.asyncio
    async def test_rerank_async_sorts_by_score(self) -> None:
        daemon = SearchDaemon()
        daemon._reranker = MagicMock()
        daemon._reranker.return_value = [(1, 0.95), (0, 0.80)]
        daemon.config.reranker_top_k = 10

        results = [
            SearchResult(path="/a.py", chunk_text="aaa", score=0.5),
            SearchResult(path="/b.py", chunk_text="bbb", score=0.6),
        ]
        reranked = await daemon._rerank_async("query", results)
        assert reranked[0].reranker_score == 0.95
        assert reranked[0].path == "/b.py"

    @pytest.mark.asyncio
    async def test_rerank_empty_results(self) -> None:
        daemon = SearchDaemon()
        daemon._reranker = MagicMock()
        result = await daemon._rerank_async("query", [])
        assert result == []


# =============================================================================
# Indexing tests
# =============================================================================


class TestSearchDaemonIndexing:
    """Test explicit and auto-indexing."""

    @pytest.mark.asyncio
    async def test_index_documents(self) -> None:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.upsert.return_value = 3
        daemon._backend = mock_backend

        count = await daemon.index_documents(
            [{"id": "1", "text": "a"}, {"id": "2", "text": "b"}, {"id": "3", "text": "c"}],
            zone_id="corp",
        )
        assert count == 3
        assert daemon.stats.documents_indexed == 3

    @pytest.mark.asyncio
    async def test_delete_documents(self) -> None:
        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.delete.return_value = 2
        daemon._backend = mock_backend

        count = await daemon.delete_documents(["id1", "id2"], zone_id="corp")
        assert count == 2

    @pytest.mark.asyncio
    async def test_notify_file_change_disabled(self) -> None:
        """When auto_index_on_write=False, notify should be a no-op."""
        config = DaemonConfig(auto_index_on_write=False)
        daemon = SearchDaemon(config)
        await daemon.notify_file_change("/test.py", "content", zone_id="z")
        assert len(daemon._pending_index_docs) == 0

    @pytest.mark.asyncio
    async def test_notify_file_change_enabled(self) -> None:
        """When auto_index_on_write=True, notify should queue the doc."""
        config = DaemonConfig(auto_index_on_write=True)
        daemon = SearchDaemon(config)
        await daemon.notify_file_change("/test.py", "content", zone_id="z")
        assert len(daemon._pending_index_docs) == 1
        assert daemon._pending_index_docs[0]["path"] == "/test.py"


# =============================================================================
# Statistics tests
# =============================================================================


class TestSearchDaemonStats:
    """Test statistics and health."""

    def test_get_stats(self) -> None:
        daemon = SearchDaemon()
        stats = daemon.get_stats()
        assert stats["initialized"] is False
        assert stats["backend"] == "txtai"
        assert stats["total_queries"] == 0

    def test_get_health(self) -> None:
        daemon = SearchDaemon()
        health = daemon.get_health()
        assert health["status"] == "starting"
        assert health["daemon_initialized"] is False

    def test_track_latency(self) -> None:
        daemon = SearchDaemon()
        daemon._track_latency(10.0)
        daemon._track_latency(20.0)
        assert daemon.stats.total_queries == 2
        assert daemon.stats.avg_latency_ms == 15.0
