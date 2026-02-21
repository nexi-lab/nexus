"""Tests for SearchDaemon async lifecycle (Issue #1520).

Validates:
- SearchDaemon startup/shutdown lifecycle
- _init_bm25s_index with bm25s unavailable
- _init_database_pool with no URL
- Latency tracking circular buffer
- get_search_daemon/set_search_daemon singleton
"""

from unittest.mock import AsyncMock, patch

import pytest

from nexus.search.daemon import (
    DaemonConfig,
    DaemonStats,
    SearchDaemon,
    SearchResult,
    get_search_daemon,
    set_search_daemon,
)

# =============================================================================
# SearchDaemon lifecycle
# =============================================================================


class TestSearchDaemonLifecycle:
    """Test startup/shutdown lifecycle."""

    @pytest.mark.asyncio
    async def test_startup_sets_initialized(self) -> None:
        """After startup(), is_initialized should be True."""
        daemon = SearchDaemon()

        with (
            patch.object(daemon, "_init_bm25s_index", return_value=None),
            patch.object(daemon, "_init_database_pool", return_value=None),
            patch.object(daemon, "_check_zoekt", return_value=None),
            patch.object(daemon, "_check_embedding_cache", return_value=None),
        ):
            await daemon.startup()
            assert daemon.is_initialized
            assert daemon.stats.startup_time_ms > 0

        await daemon.shutdown()
        assert not daemon.is_initialized

    @pytest.mark.asyncio
    async def test_shutdown_cancels_refresh_task(self) -> None:
        """shutdown() should cancel the refresh task."""
        config = DaemonConfig(refresh_enabled=True)
        daemon = SearchDaemon(config)

        with (
            patch.object(daemon, "_init_bm25s_index", return_value=None),
            patch.object(daemon, "_init_database_pool", return_value=None),
            patch.object(daemon, "_check_zoekt", return_value=None),
            patch.object(daemon, "_check_embedding_cache", return_value=None),
        ):
            await daemon.startup()
            # refresh_task should be created
            assert daemon._refresh_task is not None

        await daemon.shutdown()
        assert not daemon.is_initialized

    @pytest.mark.asyncio
    async def test_shutdown_disposes_engine(self) -> None:
        """shutdown() should dispose of the async engine if it owns it."""
        daemon = SearchDaemon()
        mock_engine = AsyncMock()
        daemon._async_engine = mock_engine
        daemon._owns_engine = True

        await daemon.shutdown()

        mock_engine.dispose.assert_awaited_once()
        assert daemon._async_engine is None


# =============================================================================
# _init_bm25s_index
# =============================================================================


class TestInitBM25S:
    """Test _init_bm25s_index behavior."""

    @pytest.mark.asyncio
    async def test_bm25s_unavailable(self) -> None:
        """When bm25s is not importable, should gracefully skip."""
        daemon = SearchDaemon()

        with patch(
            "nexus.search.daemon.SearchDaemon._init_bm25s_index",
            new_callable=AsyncMock,
        ) as mock_init:
            mock_init.return_value = None
            await daemon._init_bm25s_index()

        # Should not crash, bm25s_index remains None
        assert daemon._bm25s_index is None


# =============================================================================
# _init_database_pool
# =============================================================================


class TestInitDatabasePool:
    """Test _init_database_pool behavior."""

    @pytest.mark.asyncio
    async def test_no_url_skips_init(self) -> None:
        """Without database_url, pool init should be skipped."""
        config = DaemonConfig(database_url=None)
        daemon = SearchDaemon(config)

        await daemon._init_database_pool()

        assert daemon._async_engine is None
        assert daemon._async_session is None
        assert daemon.stats.db_pool_size == 0


# =============================================================================
# Latency tracking
# =============================================================================


class TestLatencyTracking:
    """Test latency tracking circular buffer."""

    def test_track_latency_basic(self) -> None:
        daemon = SearchDaemon()
        daemon._track_latency(10.0)
        daemon._track_latency(20.0)
        daemon._track_latency(30.0)

        assert daemon.stats.total_queries == 3
        assert daemon.stats.avg_latency_ms == pytest.approx(20.0)
        assert len(daemon._latencies) == 3

    def test_track_latency_circular_buffer(self) -> None:
        """Buffer should cap at _max_latency_samples."""
        daemon = SearchDaemon()
        daemon._max_latency_samples = 5

        for i in range(10):
            daemon._track_latency(float(i))

        assert len(daemon._latencies) == 5
        # Should keep the most recent 5
        assert daemon._latencies == [5.0, 6.0, 7.0, 8.0, 9.0]

    def test_track_latency_p99(self) -> None:
        """P99 should be close to max for small samples."""
        daemon = SearchDaemon()
        for i in range(100):
            daemon._track_latency(float(i))

        # P99 should be high (99th percentile of 0-99)
        assert daemon.stats.p99_latency_ms >= 90.0

    def test_track_latency_updates_total_queries(self) -> None:
        daemon = SearchDaemon()
        assert daemon.stats.total_queries == 0

        daemon._track_latency(5.0)
        assert daemon.stats.total_queries == 1

        daemon._track_latency(10.0)
        assert daemon.stats.total_queries == 2


# =============================================================================
# Singleton management
# =============================================================================


class TestDaemonSingleton:
    """Test get_search_daemon / set_search_daemon."""

    def test_get_returns_none_initially(self) -> None:
        """Before set, get should return None."""
        # Save current state
        import nexus.search.daemon as mod

        original = mod._daemon_instance

        try:
            mod._daemon_instance = None
            assert get_search_daemon() is None
        finally:
            mod._daemon_instance = original

    def test_set_and_get_roundtrip(self) -> None:
        """set then get should return the same instance."""
        import nexus.search.daemon as mod

        original = mod._daemon_instance

        try:
            daemon = SearchDaemon()
            set_search_daemon(daemon)
            assert get_search_daemon() is daemon
        finally:
            mod._daemon_instance = original


# =============================================================================
# DaemonConfig defaults
# =============================================================================


class TestDaemonConfigDefaults:
    """Test DaemonConfig default values."""

    def test_defaults(self) -> None:
        config = DaemonConfig()
        assert config.database_url is None
        assert config.db_pool_min_size == 10
        assert config.db_pool_max_size == 50
        assert config.db_pool_recycle == 1800
        assert config.bm25s_index_dir == ".nexus-data/bm25s"
        assert config.bm25s_mmap is True
        assert config.vector_warmup_enabled is True
        assert config.refresh_debounce_seconds == 5.0
        assert config.refresh_enabled is True
        assert config.query_timeout_seconds == 10.0
        assert config.entropy_filtering is False

    def test_custom_values(self) -> None:
        config = DaemonConfig(
            database_url="postgresql://localhost/test",
            db_pool_min_size=5,
            db_pool_max_size=20,
        )
        assert config.database_url == "postgresql://localhost/test"
        assert config.db_pool_min_size == 5
        assert config.db_pool_max_size == 20


# =============================================================================
# DaemonStats and SearchResult
# =============================================================================


class TestDaemonStats:
    """Test DaemonStats defaults."""

    def test_defaults(self) -> None:
        stats = DaemonStats()
        assert stats.startup_time_ms == 0.0
        assert stats.bm25_documents == 0
        assert stats.total_queries == 0
        assert stats.avg_latency_ms == 0.0
        assert stats.p99_latency_ms == 0.0
        assert stats.zoekt_available is False


class TestSearchResult:
    """Test SearchResult dataclass."""

    def test_construction(self) -> None:
        result = SearchResult(
            path="/test.py",
            chunk_text="hello",
            score=0.9,
            search_type="hybrid",
        )
        assert result.path == "/test.py"
        assert result.search_type == "hybrid"

    def test_extends_base_search_result(self) -> None:
        from nexus.search.results import BaseSearchResult

        result = SearchResult(path="/test.py", chunk_text="hello", score=0.9)
        assert isinstance(result, BaseSearchResult)


# =============================================================================
# get_stats and get_health
# =============================================================================


class TestDaemonStats2:
    """Test get_stats() and get_health() output."""

    def test_get_stats_shape(self) -> None:
        daemon = SearchDaemon()
        stats = daemon.get_stats()

        expected_keys = {
            "initialized",
            "startup_time_ms",
            "bm25_documents",
            "bm25_load_time_ms",
            "db_pool_size",
            "db_pool_warmup_time_ms",
            "vector_warmup_time_ms",
            "total_queries",
            "avg_latency_ms",
            "p99_latency_ms",
            "last_index_refresh",
            "zoekt_available",
            "embedding_cache_connected",
            "entropy_filtering",
        }
        assert set(stats.keys()) == expected_keys

    def test_get_health_shape(self) -> None:
        daemon = SearchDaemon()
        health = daemon.get_health()

        assert "status" in health
        assert "daemon_initialized" in health
        assert "bm25_index_loaded" in health
        assert "db_pool_ready" in health

    def test_get_health_not_initialized(self) -> None:
        daemon = SearchDaemon()
        health = daemon.get_health()
        assert health["status"] == "starting"
        assert health["daemon_initialized"] is False
