"""Unit tests for the Hot Search Daemon (Issue #951).

Tests cover:
- Daemon initialization and startup
- Search methods (keyword, semantic, hybrid)
- Index refresh notifications
- Statistics and health checks
- Graceful shutdown
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.search.daemon import (
    DaemonConfig,
    DaemonStats,
    SearchDaemon,
    SearchResult,
    create_and_start_daemon,
    get_search_daemon,
    set_search_daemon,
)


class TestDaemonConfig:
    """Tests for DaemonConfig dataclass."""

    def test_default_values(self) -> None:
        """Test default configuration values."""
        config = DaemonConfig()

        assert config.database_url is None
        assert config.db_pool_min_size == 10
        assert config.db_pool_max_size == 50
        assert config.db_pool_recycle == 1800
        assert config.bm25s_index_dir == ".nexus-data/bm25s"
        assert config.bm25s_mmap is True
        assert config.vector_warmup_enabled is True
        assert config.vector_ef_search == 100
        assert config.refresh_debounce_seconds == 5.0
        assert config.refresh_enabled is True
        assert config.query_timeout_seconds == 10.0

    def test_custom_values(self) -> None:
        """Test custom configuration values."""
        config = DaemonConfig(
            database_url="postgresql://localhost/test",
            db_pool_min_size=5,
            db_pool_max_size=20,
            refresh_enabled=False,
        )

        assert config.database_url == "postgresql://localhost/test"
        assert config.db_pool_min_size == 5
        assert config.db_pool_max_size == 20
        assert config.refresh_enabled is False


class TestDaemonStats:
    """Tests for DaemonStats dataclass."""

    def test_default_values(self) -> None:
        """Test default statistics values."""
        stats = DaemonStats()

        assert stats.startup_time_ms == 0.0
        assert stats.bm25_documents == 0
        assert stats.total_queries == 0
        assert stats.avg_latency_ms == 0.0
        assert stats.p99_latency_ms == 0.0
        assert stats.zoekt_available is False
        assert stats.embedding_cache_connected is False


class TestSearchResult:
    """Tests for SearchResult dataclass."""

    def test_basic_result(self) -> None:
        """Test basic search result creation."""
        result = SearchResult(
            path="/test/file.py",
            chunk_text="def test_function():",
            score=0.95,
        )

        assert result.path == "/test/file.py"
        assert result.chunk_text == "def test_function():"
        assert result.score == 0.95
        assert result.chunk_index == 0
        assert result.search_type == "hybrid"

    def test_full_result(self) -> None:
        """Test search result with all fields."""
        result = SearchResult(
            path="/test/file.py",
            chunk_text="def test_function():",
            score=0.95,
            chunk_index=3,
            start_offset=100,
            end_offset=200,
            line_start=10,
            line_end=15,
            keyword_score=0.8,
            vector_score=0.9,
            search_type="hybrid",
        )

        assert result.chunk_index == 3
        assert result.start_offset == 100
        assert result.end_offset == 200
        assert result.line_start == 10
        assert result.line_end == 15
        assert result.keyword_score == 0.8
        assert result.vector_score == 0.9


class TestSearchDaemon:
    """Tests for SearchDaemon class."""

    @pytest.fixture
    def config(self) -> DaemonConfig:
        """Create test configuration."""
        return DaemonConfig(
            database_url=None,  # No DB for unit tests
            refresh_enabled=False,  # Disable background task
        )

    @pytest.fixture
    def daemon(self, config: DaemonConfig) -> SearchDaemon:
        """Create test daemon instance."""
        return SearchDaemon(config)

    def test_init(self, daemon: SearchDaemon) -> None:
        """Test daemon initialization."""
        assert daemon.config is not None
        assert daemon.stats is not None
        assert daemon._initialized is False
        assert daemon._shutting_down is False

    def test_is_initialized_property(self, daemon: SearchDaemon) -> None:
        """Test is_initialized property."""
        assert daemon.is_initialized is False

    @pytest.mark.asyncio
    async def test_startup_without_db(self, daemon: SearchDaemon) -> None:
        """Test daemon startup without database."""
        await daemon.startup()

        assert daemon.is_initialized is True
        assert daemon.stats.startup_time_ms > 0

    @pytest.mark.asyncio
    async def test_startup_idempotent(self, daemon: SearchDaemon) -> None:
        """Test that startup is idempotent."""
        await daemon.startup()
        startup_time_1 = daemon.stats.startup_time_ms

        await daemon.startup()  # Second call should be no-op
        startup_time_2 = daemon.stats.startup_time_ms

        # Startup time should remain the same
        assert startup_time_1 == startup_time_2

    @pytest.mark.asyncio
    async def test_shutdown(self, daemon: SearchDaemon) -> None:
        """Test daemon shutdown."""
        await daemon.startup()
        await daemon.shutdown()

        assert daemon._initialized is False
        assert daemon._shutting_down is True

    @pytest.mark.asyncio
    async def test_shutdown_idempotent(self, daemon: SearchDaemon) -> None:
        """Test that shutdown is idempotent."""
        await daemon.startup()
        await daemon.shutdown()
        await daemon.shutdown()  # Second call should be no-op

        assert daemon._shutting_down is True

    @pytest.mark.asyncio
    async def test_search_not_initialized(self, daemon: SearchDaemon) -> None:
        """Test that search raises error if not initialized."""
        with pytest.raises(RuntimeError, match="not initialized"):
            await daemon.search("test query")

    @pytest.mark.asyncio
    async def test_get_stats(self, daemon: SearchDaemon) -> None:
        """Test get_stats method."""
        await daemon.startup()

        stats = daemon.get_stats()

        assert "initialized" in stats
        assert "startup_time_ms" in stats
        assert "bm25_documents" in stats
        assert "total_queries" in stats
        assert "avg_latency_ms" in stats
        assert "p99_latency_ms" in stats
        assert stats["initialized"] is True

    @pytest.mark.asyncio
    async def test_get_health(self, daemon: SearchDaemon) -> None:
        """Test get_health method."""
        await daemon.startup()

        health = daemon.get_health()

        assert "status" in health
        assert "daemon_initialized" in health
        assert "bm25_index_loaded" in health
        assert "db_pool_ready" in health
        assert health["status"] == "healthy"
        assert health["daemon_initialized"] is True

    @pytest.mark.asyncio
    async def test_notify_file_change(self, daemon: SearchDaemon) -> None:
        """Test file change notification."""
        daemon.config.refresh_enabled = True
        await daemon.startup()

        await daemon.notify_file_change("/test/file.py", "update")

        assert "/test/file.py" in daemon._pending_refresh_paths

    @pytest.mark.asyncio
    async def test_notify_file_change_disabled(self, daemon: SearchDaemon) -> None:
        """Test file change notification when refresh is disabled."""
        daemon.config.refresh_enabled = False
        await daemon.startup()

        await daemon.notify_file_change("/test/file.py", "update")

        # Should not add to pending paths when disabled
        assert "/test/file.py" not in daemon._pending_refresh_paths

    def test_track_latency(self, daemon: SearchDaemon) -> None:
        """Test latency tracking."""
        daemon._track_latency(10.0)
        daemon._track_latency(20.0)
        daemon._track_latency(30.0)

        assert daemon.stats.total_queries == 3
        assert daemon.stats.avg_latency_ms == 20.0
        assert len(daemon._latencies) == 3

    def test_track_latency_overflow(self, daemon: SearchDaemon) -> None:
        """Test latency tracking with buffer overflow."""
        daemon._max_latency_samples = 5

        for i in range(10):
            daemon._track_latency(float(i))

        assert len(daemon._latencies) == 5
        assert daemon._latencies == [5.0, 6.0, 7.0, 8.0, 9.0]


class TestSearchDaemonWithMocks:
    """Tests for SearchDaemon with mocked components."""

    @pytest.fixture
    def config(self) -> DaemonConfig:
        """Create test configuration."""
        return DaemonConfig(
            database_url="postgresql://localhost/test",
            refresh_enabled=False,
        )

    @pytest.mark.asyncio
    async def test_startup_with_bm25s(self, config: DaemonConfig) -> None:
        """Test daemon startup with BM25S index."""
        daemon = SearchDaemon(config)

        mock_index = MagicMock()
        mock_index.initialize = AsyncMock(return_value=True)
        mock_index._corpus = ["doc1", "doc2", "doc3"]

        with (
            patch("nexus.search.bm25s_search.BM25SIndex", return_value=mock_index),
            patch("nexus.search.bm25s_search.is_bm25s_available", return_value=True),
        ):
            await daemon._init_bm25s_index()

        assert daemon._bm25s_index is not None
        assert daemon.stats.bm25_documents == 3

    @pytest.mark.asyncio
    async def test_search_bm25s(self, config: DaemonConfig) -> None:
        """Test BM25S search."""
        daemon = SearchDaemon(config)

        mock_result = MagicMock()
        mock_result.path = "/test/file.py"
        mock_result.content_preview = "test content"
        mock_result.score = 0.95

        mock_index = MagicMock()
        mock_index.search = AsyncMock(return_value=[mock_result])
        daemon._bm25s_index = mock_index

        results = await daemon._search_bm25s("test query", limit=10, path_filter=None)

        assert len(results) == 1
        assert results[0].path == "/test/file.py"
        assert results[0].score == 0.95

    @pytest.mark.asyncio
    async def test_keyword_search_priority(self, config: DaemonConfig) -> None:
        """Test keyword search tries Zoekt, then BM25S, then FTS."""
        daemon = SearchDaemon(config)
        daemon._initialized = True

        # Mock BM25S search
        mock_result = MagicMock()
        mock_result.path = "/test/file.py"
        mock_result.content_preview = "test content"
        mock_result.score = 0.95

        mock_index = MagicMock()
        mock_index.search = AsyncMock(return_value=[mock_result])
        daemon._bm25s_index = mock_index
        daemon.stats.zoekt_available = False  # Zoekt not available

        results = await daemon._keyword_search("test query", limit=10, path_filter=None)

        assert len(results) == 1
        assert results[0].path == "/test/file.py"


class TestGlobalDaemonAccessors:
    """Tests for global daemon accessor functions."""

    def test_get_set_search_daemon(self) -> None:
        """Test get_search_daemon and set_search_daemon."""
        # Initially None
        original = get_search_daemon()

        # Create and set daemon
        daemon = SearchDaemon(DaemonConfig())
        set_search_daemon(daemon)

        # Should return the set daemon
        assert get_search_daemon() is daemon

        # Cleanup - restore original state
        if original:
            set_search_daemon(original)


class TestCreateAndStartDaemon:
    """Tests for create_and_start_daemon helper."""

    @pytest.mark.asyncio
    async def test_create_and_start(self) -> None:
        """Test create_and_start_daemon helper function."""
        with patch.dict("os.environ", {"NEXUS_DATABASE_URL": ""}, clear=False):
            daemon = await create_and_start_daemon(
                database_url=None,
                bm25s_index_dir=".nexus-data/test-bm25s",
            )

            assert daemon.is_initialized is True
            assert daemon.config.bm25s_index_dir == ".nexus-data/test-bm25s"

            # Cleanup
            await daemon.shutdown()


class TestSearchDaemonIntegration:
    """Integration-style tests for SearchDaemon."""

    @pytest.mark.asyncio
    async def test_full_lifecycle(self) -> None:
        """Test full daemon lifecycle: startup -> search -> shutdown."""
        config = DaemonConfig(
            database_url=None,
            refresh_enabled=False,
        )
        daemon = SearchDaemon(config)

        # Startup
        await daemon.startup()
        assert daemon.is_initialized is True

        # Get stats
        stats = daemon.get_stats()
        assert stats["initialized"] is True

        # Get health
        health = daemon.get_health()
        assert health["status"] == "healthy"

        # Shutdown
        await daemon.shutdown()
        assert daemon._initialized is False

    @pytest.mark.asyncio
    async def test_concurrent_searches(self) -> None:
        """Test multiple concurrent search requests."""
        config = DaemonConfig(
            database_url=None,
            refresh_enabled=False,
        )
        daemon = SearchDaemon(config)
        await daemon.startup()

        # Mock the keyword search to return results
        async def mock_keyword_search(query: str, limit: int, path_filter: str | None) -> list:
            await asyncio.sleep(0.01)  # Simulate some work
            return []

        daemon._keyword_search = mock_keyword_search  # type: ignore

        # Run multiple searches concurrently
        tasks = [daemon.search(f"query_{i}", search_type="keyword", limit=10) for i in range(5)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 5
        assert daemon.stats.total_queries == 5

        await daemon.shutdown()
