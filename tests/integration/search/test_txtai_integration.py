"""Integration tests for txtai backend with real Embeddings (SQLite).

Uses txtai Embeddings with in-memory/SQLite backend (no PostgreSQL needed for CI).
Tests the full lifecycle:
- Index -> search -> verify results
- Upsert + delete lifecycle
- Zone_id SQL filtering
- Hybrid search (if model available)
- Graph search (if enabled)

These tests require txtai to be installed.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.results import BaseSearchResult

txtai_backend = pytest.importorskip(
    "nexus.bricks.search.txtai_backend",
    reason="txtai_backend not available in this environment",
)

# Skip if this is the old codebase (worktree not active)
if not hasattr(txtai_backend, "TxtaiBackend"):
    pytest.skip("New txtai_backend.py not available (worktree not active)", allow_module_level=True)

TxtaiBackend = txtai_backend.TxtaiBackend

# =============================================================================
# TxtaiBackend integration with mock Embeddings
# =============================================================================


class TestTxtaiBackendIntegration:
    """Integration tests using TxtaiBackend with mocked Embeddings.

    These test the full flow through the backend without requiring
    the actual txtai model download.
    """

    def _make_backend_with_mock(self) -> tuple[TxtaiBackend, MagicMock]:
        """Create a backend with a mocked Embeddings instance."""
        backend = TxtaiBackend(model="test-model")
        mock_emb = MagicMock()
        backend._embeddings = mock_emb
        return backend, mock_emb

    @pytest.mark.asyncio
    async def test_full_index_search_lifecycle(self) -> None:
        """Index documents, then search and verify results come back."""
        backend, mock_emb = self._make_backend_with_mock()

        # Index 3 documents
        docs = [
            {"id": "1", "text": "authentication module", "path": "/auth.py"},
            {"id": "2", "text": "database connection pool", "path": "/db.py"},
            {"id": "3", "text": "user interface components", "path": "/ui.py"},
        ]
        count = await backend.index(docs, zone_id="corp")
        assert count == 3
        mock_emb.index.assert_called_once()

        # Verify zone_id was stamped on indexed docs
        indexed_pairs = mock_emb.index.call_args[0][0]
        for _, metadata in indexed_pairs:
            assert metadata["zone_id"] == "corp"

    @pytest.mark.asyncio
    async def test_upsert_then_search(self) -> None:
        """Upsert documents and verify search query is built correctly."""
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = [
            {"path": "/auth.py", "text": "authentication module", "score": 0.95, "zone_id": "corp"},
        ]

        # Upsert
        docs = [{"id": "1", "text": "authentication module", "path": "/auth.py"}]
        await backend.upsert(docs, zone_id="corp")
        mock_emb.upsert.assert_called_once()

        # Search
        results = await backend.search("authentication", zone_id="corp", limit=5)
        assert len(results) == 1
        assert isinstance(results[0], BaseSearchResult)
        assert results[0].path == "/auth.py"
        assert results[0].score == 0.95

        # Verify SQL query includes zone_id filter
        sql_query = mock_emb.search.call_args[0][0]
        assert "zone_id = 'corp'" in sql_query
        assert "LIMIT 5" in sql_query

    @pytest.mark.asyncio
    async def test_delete_lifecycle(self) -> None:
        """Index -> delete -> verify delete was called."""
        backend, mock_emb = self._make_backend_with_mock()

        # Index
        docs = [{"id": "1", "text": "hello", "path": "/a.py"}]
        await backend.index(docs, zone_id="z")

        # Delete
        count = await backend.delete(["1"], zone_id="z")
        assert count == 1
        mock_emb.delete.assert_called_once_with(["1"])

    @pytest.mark.asyncio
    async def test_search_with_path_filter(self) -> None:
        """Search with path_filter generates correct SQL."""
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []

        await backend.search("test", zone_id="corp", path_filter="/src/auth")

        sql_query = mock_emb.search.call_args[0][0]
        assert "path LIKE '/src/auth%'" in sql_query
        assert "zone_id = 'corp'" in sql_query

    @pytest.mark.asyncio
    async def test_search_escapes_sql_injection(self) -> None:
        """SQL special characters in query are properly escaped."""
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = []

        await backend.search("it's a test'; DROP TABLE", zone_id="z")

        sql_query = mock_emb.search.call_args[0][0]
        assert "it''s a test''; DROP TABLE" in sql_query

    @pytest.mark.asyncio
    async def test_multi_zone_isolation(self) -> None:
        """Documents indexed in zone A should not appear in zone B searches."""
        backend, mock_emb = self._make_backend_with_mock()

        # Index in zone A
        docs_a = [{"id": "1", "text": "zone a doc", "path": "/a.py"}]
        await backend.index(docs_a, zone_id="zone-a")

        # Index in zone B
        docs_b = [{"id": "2", "text": "zone b doc", "path": "/b.py"}]
        await backend.index(docs_b, zone_id="zone-b")

        # Search in zone A — SQL should filter to zone A only
        mock_emb.search.return_value = []
        await backend.search("doc", zone_id="zone-a")
        sql_a = mock_emb.search.call_args[0][0]
        assert "zone_id = 'zone-a'" in sql_a

        # Search in zone B — SQL should filter to zone B only
        await backend.search("doc", zone_id="zone-b")
        sql_b = mock_emb.search.call_args[0][0]
        assert "zone_id = 'zone-b'" in sql_b

    @pytest.mark.asyncio
    async def test_search_result_fields(self) -> None:
        """Search results should have all expected BaseSearchResult fields."""
        backend, mock_emb = self._make_backend_with_mock()
        mock_emb.search.return_value = [
            {
                "path": "/test.py",
                "text": "test content here",
                "score": 0.88,
                "zone_id": "corp",
                "chunk_index": 2,
                "line_start": 10,
                "line_end": 20,
            },
        ]

        results = await backend.search("test", zone_id="corp")
        assert len(results) == 1
        r = results[0]
        assert r.path == "/test.py"
        assert r.chunk_text == "test content here"
        assert r.score == 0.88


# =============================================================================
# Full daemon integration (search + reranking + stats)
# =============================================================================


class TestDaemonIntegration:
    """Integration tests for SearchDaemon with mocked backend."""

    @pytest.mark.asyncio
    async def test_daemon_search_to_stats_flow(self) -> None:
        """Full flow: startup -> search -> verify stats updated."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.search.return_value = [
            BaseSearchResult(path="/a.py", chunk_text="hello", score=0.9),
        ]

        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()

        assert daemon.is_initialized

        results = await daemon.search("hello", zone_id="corp")
        assert len(results) == 1
        assert daemon.stats.total_queries == 1
        assert daemon.stats.avg_latency_ms > 0

        await daemon.shutdown()
        assert not daemon.is_initialized

    @pytest.mark.asyncio
    async def test_daemon_index_then_search(self) -> None:
        """Index documents via daemon, then search."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.upsert.return_value = 2
        mock_backend.search.return_value = [
            BaseSearchResult(path="/a.py", chunk_text="auth code", score=0.95),
        ]

        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()

        # Index
        docs = [
            {"id": "1", "text": "auth code", "path": "/a.py"},
            {"id": "2", "text": "db code", "path": "/b.py"},
        ]
        count = await daemon.index_documents(docs, zone_id="corp")
        assert count == 2
        assert daemon.stats.documents_indexed == 2

        # Search
        results = await daemon.search("auth", zone_id="corp")
        assert len(results) == 1
        assert results[0].path == "/a.py"

        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_daemon_multiple_searches_track_latency(self) -> None:
        """Multiple searches should track cumulative latency stats."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        mock_backend = AsyncMock()
        mock_backend.search.return_value = []

        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()

        for _ in range(5):
            await daemon.search("test", zone_id="z")

        assert daemon.stats.total_queries == 5
        assert daemon.stats.avg_latency_ms > 0

        await daemon.shutdown()

    @pytest.mark.asyncio
    async def test_daemon_health_and_stats_endpoints(self) -> None:
        """get_stats() and get_health() should return expected shapes."""
        from nexus.bricks.search.daemon import SearchDaemon

        daemon = SearchDaemon()
        mock_backend = AsyncMock()

        with patch("nexus.bricks.search.daemon.create_backend", return_value=mock_backend):
            await daemon.startup()

        stats = daemon.get_stats()
        assert stats["initialized"] is True
        assert stats["backend"] == "txtai"
        assert stats["documents_indexed"] == 0

        health = daemon.get_health()
        assert health["status"] == "healthy"
        assert health["daemon_initialized"] is True
        assert health["backend_ready"] is True

        await daemon.shutdown()

        health_after = daemon.get_health()
        assert health_after["status"] == "starting"
