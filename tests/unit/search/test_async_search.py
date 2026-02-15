"""Tests for AsyncSemanticSearch (Issue #1520).

Comprehensive tests for the async semantic search implementation covering:
- create_async_engine_from_url() URL conversion
- AsyncSemanticSearch initialization
- search() mode dispatching (keyword/semantic/hybrid)
- Adaptive k (Issue #1021)
- _keyword_search() backend selection (Zoekt, BM25S, pg BM25, ts_rank, FTS5)
- index_document() chunking and embedding
- get_stats() and error handling
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from nexus.search.async_search import AsyncSearchResult, create_async_engine_from_url
from nexus.search.chunking import DocumentChunk

# =============================================================================
# create_async_engine_from_url tests
# =============================================================================


class TestCreateAsyncEngineFromUrl:
    """Tests for URL conversion logic (5 tests)."""

    @patch("nexus.search.async_search.create_async_engine")
    def test_postgresql_url_conversion(self, mock_create: Mock) -> None:
        """postgresql:// should become postgresql+asyncpg://."""
        create_async_engine_from_url("postgresql://localhost/db")
        url_arg = str(mock_create.call_args[0][0])
        assert "postgresql+asyncpg://" in url_arg

    @patch("nexus.search.async_search.create_async_engine")
    def test_sqlite_triple_slash_url_conversion(self, mock_create: Mock) -> None:
        """sqlite:///path should become sqlite+aiosqlite:///path."""
        create_async_engine_from_url("sqlite:///test.db")
        url_arg = str(mock_create.call_args[0][0])
        assert "sqlite+aiosqlite:///" in url_arg

    @patch("nexus.search.async_search.create_async_engine")
    def test_sqlite_double_slash_url_conversion(self, mock_create: Mock) -> None:
        """sqlite://path should become sqlite+aiosqlite://path."""
        create_async_engine_from_url("sqlite://test.db")
        url_arg = str(mock_create.call_args[0][0])
        assert "sqlite+aiosqlite://" in url_arg

    @patch("nexus.search.async_search.create_async_engine")
    def test_postgresql_gets_pool_params(self, mock_create: Mock) -> None:
        """PostgreSQL should get connection pool parameters."""
        create_async_engine_from_url("postgresql://localhost/db")
        kwargs = mock_create.call_args[1]
        assert kwargs["pool_pre_ping"] is True
        assert kwargs["pool_size"] == 20
        assert kwargs["max_overflow"] == 30

    @patch("nexus.search.async_search.create_async_engine")
    def test_sqlite_no_pool_params(self, mock_create: Mock) -> None:
        """SQLite should NOT get pool parameters (uses NullPool)."""
        create_async_engine_from_url("sqlite:///test.db")
        kwargs = mock_create.call_args[1]
        assert "pool_size" not in kwargs


# =============================================================================
# AsyncSemanticSearch.__init__ tests
# =============================================================================


class TestInit:
    """Tests for __init__() (4 tests)."""

    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    def test_postgresql_db_type(self, _sm: Mock, _ce: Mock) -> None:
        """PostgreSQL URL should set db_type='postgresql'."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        assert search.db_type == "postgresql"

    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    def test_sqlite_db_type(self, _sm: Mock, _ce: Mock) -> None:
        """SQLite URL should set db_type='sqlite'."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("sqlite:///test.db")
        assert search.db_type == "sqlite"

    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    def test_bm25_not_available_by_default(self, _sm: Mock, _ce: Mock) -> None:
        """BM25 should not be available by default."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        assert search.bm25_available is False

    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    def test_entropy_filtering_creates_chunker(self, _sm: Mock, _ce: Mock) -> None:
        """entropy_filtering=True should create an EntropyAwareChunker."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch(
            "postgresql://localhost/db",
            entropy_filtering=True,
            entropy_threshold=0.4,
        )
        assert search._entropy_chunker is not None


# =============================================================================
# search() mode dispatching tests
# =============================================================================


class TestSearch:
    """Tests for search() mode dispatching (6 tests)."""

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_keyword_mode_dispatches(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search(mode='keyword') should call _keyword_search."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db")

        mock_results: list[Any] = [AsyncSearchResult(path="/test.py", chunk_text="test", score=0.9)]
        with patch.object(
            search, "_keyword_search", new_callable=AsyncMock, return_value=mock_results
        ) as mock_kw:
            results = await search.search("test", limit=5, search_mode="keyword")

        mock_kw.assert_called_once()
        assert results == mock_results

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_semantic_mode_dispatches(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search(mode='semantic') should call _vector_search."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        mock_provider = AsyncMock()
        mock_provider.embed_text.return_value = [0.1] * 1536
        search = AsyncSemanticSearch("postgresql://localhost/db", embedding_provider=mock_provider)

        mock_results: list[Any] = [
            AsyncSearchResult(path="/test.py", chunk_text="test", score=0.92)
        ]
        with patch.object(
            search, "_vector_search", new_callable=AsyncMock, return_value=mock_results
        ) as mock_vs:
            results = await search.search("test", limit=5, search_mode="semantic")

        mock_provider.embed_text.assert_called_once_with("test")
        mock_vs.assert_called_once()
        assert results == mock_results

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_semantic_without_provider_raises(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search(mode='semantic') without provider should raise ValueError."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db", embedding_provider=None)

        with pytest.raises(ValueError, match="Semantic search requires embedding provider"):
            await search.search("test", search_mode="semantic")

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_hybrid_mode_dispatches(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search(mode='hybrid') should call _hybrid_search with params."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db")

        mock_results: list[Any] = []
        with patch.object(
            search, "_hybrid_search", new_callable=AsyncMock, return_value=mock_results
        ) as mock_hy:
            await search.search(
                "test",
                limit=5,
                search_mode="hybrid",
                alpha=0.6,
                fusion_method="rrf",
                rrf_k=60,
            )

        mock_hy.assert_called_once()
        call_kwargs = mock_hy.call_args[1]
        assert call_kwargs["alpha"] == 0.6
        assert call_kwargs["fusion_method"] == "rrf"
        assert call_kwargs["rrf_k"] == 60

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_adaptive_k_modifies_limit(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search(adaptive_k=True) should dynamically adjust limit."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db")

        # Pre-set a mock ContextBuilder to avoid lazy import
        mock_cb = MagicMock()
        mock_cb.calculate_k_dynamic.return_value = 20
        search._context_builder = mock_cb

        with patch.object(
            search, "_keyword_search", new_callable=AsyncMock, return_value=[]
        ) as mock_kw:
            await search.search("test", limit=10, search_mode="keyword", adaptive_k=True)

        mock_cb.calculate_k_dynamic.assert_called_once_with("test", k_base=10)
        # Verify _keyword_search received the adjusted limit (20)
        call_args = mock_kw.call_args[0]
        assert call_args[2] == 20  # limit arg

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_path_filter_passed_through(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """search() should pass path_filter to underlying methods."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db")

        with patch.object(
            search, "_keyword_search", new_callable=AsyncMock, return_value=[]
        ) as mock_kw:
            await search.search("test", limit=10, path_filter="/src/", search_mode="keyword")

        call_args = mock_kw.call_args[0]
        assert call_args[3] == "/src/"  # path_filter arg


# =============================================================================
# _keyword_search backend selection tests
# =============================================================================


class TestKeywordSearch:
    """Tests for _keyword_search() backend selection (4 tests)."""

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_zoekt_takes_priority(self, _sm: Mock, _ce: Mock) -> None:
        """Zoekt results should short-circuit database FTS."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        mock_session = AsyncMock()

        zoekt_result = AsyncSearchResult(path="/test.py", chunk_text="zoekt hit", score=0.9)
        with patch.object(
            search,
            "_try_keyword_search_with_zoekt",
            new_callable=AsyncMock,
            return_value=[zoekt_result],
        ):
            results = await search._keyword_search(mock_session, "test", 10, None)

        assert results == [zoekt_result]
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_bm25s_second_priority(self, _sm: Mock, _ce: Mock) -> None:
        """BM25S should be tried when Zoekt returns None."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        mock_session = AsyncMock()

        bm25s_result = AsyncSearchResult(path="/test.py", chunk_text="bm25s hit", score=0.8)
        with (
            patch.object(
                search,
                "_try_keyword_search_with_zoekt",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                search,
                "_try_keyword_search_with_bm25s",
                new_callable=AsyncMock,
                return_value=[bm25s_result],
            ),
        ):
            results = await search._keyword_search(mock_session, "test", 10, None)

        assert results == [bm25s_result]
        mock_session.execute.assert_not_called()

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_pg_bm25_when_available(self, _sm: Mock, _ce: Mock) -> None:
        """PostgreSQL with bm25_available should use BM25 query."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        search.bm25_available = True
        mock_session = AsyncMock()

        mock_row = MagicMock()
        mock_row.virtual_path = "/test.py"
        mock_row.chunk_index = 0
        mock_row.chunk_text = "content"
        mock_row.score = -0.5
        mock_row.start_offset = 0
        mock_row.end_offset = 10
        mock_row.line_start = 1
        mock_row.line_end = 1
        mock_result = MagicMock()
        mock_result.__iter__ = Mock(return_value=iter([mock_row]))
        mock_session.execute.return_value = mock_result

        with (
            patch.object(
                search,
                "_try_keyword_search_with_zoekt",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                search,
                "_try_keyword_search_with_bm25s",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            results = await search._keyword_search(mock_session, "test", 10, None)

        assert len(results) == 1
        assert results[0].path == "/test.py"
        # BM25 scores are negative; should use abs()
        assert results[0].score == 0.5

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_unknown_mode_defaults_to_hybrid(self, mock_sm: Mock, mock_ce: Mock) -> None:
        """Unrecognized search_mode should fall through to hybrid."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        mock_provider = AsyncMock()
        search = AsyncSemanticSearch("postgresql://localhost/db", embedding_provider=mock_provider)

        with patch.object(
            search, "_hybrid_search", new_callable=AsyncMock, return_value=[]
        ) as mock_hy:
            await search.search("test", limit=10, search_mode="unknown_mode")

        mock_hy.assert_called_once()


# =============================================================================
# index_document tests
# =============================================================================


class TestIndexDocument:
    """Tests for index_document() (3 tests)."""

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_basic_indexing(self, _sm: Mock, _ce: Mock) -> None:
        """index_document() should chunk content and bulk-insert."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        mock_chunks = [
            DocumentChunk(text="chunk 1", chunk_index=0, tokens=10, start_offset=0, end_offset=7),
            DocumentChunk(text="chunk 2", chunk_index=1, tokens=10, start_offset=8, end_offset=15),
        ]

        with (
            patch.object(search.chunker, "chunk", return_value=mock_chunks),
            patch.object(search, "_bulk_insert_chunks", new_callable=AsyncMock) as mock_insert,
        ):
            count = await search.index_document("/test.py", "chunk 1\nchunk 2", "path-123")

        assert count == 2
        mock_insert.assert_called_once()
        assert mock_insert.call_args[0][0] == "path-123"

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_indexing_with_embeddings(self, _sm: Mock, _ce: Mock) -> None:
        """index_document() should generate embeddings when provider exists."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_provider = AsyncMock()
        mock_provider.embed_texts_batched.return_value = [[0.1] * 1536, [0.2] * 1536]
        search = AsyncSemanticSearch(
            "postgresql://localhost/db",
            embedding_provider=mock_provider,
            batch_size=50,
        )

        mock_chunks = [
            DocumentChunk(text="chunk 1", chunk_index=0, tokens=10, start_offset=0, end_offset=7),
            DocumentChunk(text="chunk 2", chunk_index=1, tokens=10, start_offset=8, end_offset=15),
        ]

        with (
            patch.object(search.chunker, "chunk", return_value=mock_chunks),
            patch.object(search, "_bulk_insert_chunks", new_callable=AsyncMock),
        ):
            await search.index_document("/test.py", "chunk 1\nchunk 2", "path-123")

        mock_provider.embed_texts_batched.assert_called_once()
        call_kwargs = mock_provider.embed_texts_batched.call_args[1]
        assert call_kwargs["batch_size"] == 50
        assert call_kwargs["parallel"] is True

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_empty_content_returns_zero(self, _sm: Mock, _ce: Mock) -> None:
        """index_document() with no chunks should return 0."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")

        with patch.object(search.chunker, "chunk", return_value=[]):
            count = await search.index_document("/test.py", "", "path-123")

        assert count == 0


# =============================================================================
# get_stats tests
# =============================================================================


class TestGetStats:
    """Tests for get_stats() (2 tests)."""

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_returns_expected_fields(self, _sm: Mock, _ce: Mock) -> None:
        """get_stats() should return dict with expected keys."""
        from contextlib import asynccontextmanager

        from nexus.search.async_search import AsyncSemanticSearch

        mock_session = AsyncMock()
        mock_row = MagicMock()
        mock_row.total_chunks = 100
        mock_row.indexed_files = 10
        mock_result = MagicMock()
        mock_result.one.return_value = mock_row
        mock_session.execute.return_value = mock_result

        search = AsyncSemanticSearch("postgresql://localhost/db")

        @asynccontextmanager
        async def _mock_session():
            yield mock_session

        search.async_session = _mock_session  # type: ignore[assignment]

        stats = await search.get_stats()

        assert stats["total_chunks"] == 100
        assert stats["indexed_files"] == 10
        assert stats["db_type"] == "postgresql"

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_includes_bm25s_stats(self, _sm: Mock, _ce: Mock) -> None:
        """get_stats() should include BM25S stats when enabled."""
        from contextlib import asynccontextmanager

        from nexus.search.async_search import AsyncSemanticSearch

        mock_session = AsyncMock()
        mock_row = MagicMock()
        mock_row.total_chunks = 100
        mock_row.indexed_files = 10
        mock_result = MagicMock()
        mock_result.one.return_value = mock_row
        mock_session.execute.return_value = mock_result

        search = AsyncSemanticSearch("postgresql://localhost/db")

        @asynccontextmanager
        async def _mock_session():
            yield mock_session

        search.async_session = _mock_session  # type: ignore[assignment]
        search._bm25s_enabled = True
        search._bm25s_index = AsyncMock()
        search._bm25s_index.get_stats.return_value = {"indexed_docs": 10, "vocab_size": 1000}

        stats = await search.get_stats()

        assert "bm25s" in stats
        assert stats["bm25s"]["indexed_docs"] == 10


# =============================================================================
# Error handling tests
# =============================================================================


class TestErrorHandling:
    """Tests for error paths (3 tests)."""

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_connection_failure_propagates(self, _sm: Mock, _ce: Mock) -> None:
        """Database connection failure should propagate."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("Connection refused")

        with (
            patch.object(
                search,
                "_try_keyword_search_with_zoekt",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                search,
                "_try_keyword_search_with_bm25s",
                new_callable=AsyncMock,
                return_value=None,
            ),
            pytest.raises(Exception, match="Connection refused"),
        ):
            await search._keyword_search(mock_session, "test", 10, None)

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_missing_provider_for_semantic(self, mock_sm: Mock, _ce: Mock) -> None:
        """Semantic mode without provider should raise ValueError."""
        from nexus.search.async_search import AsyncSemanticSearch

        mock_sm.return_value = MagicMock(return_value=AsyncMock())
        search = AsyncSemanticSearch("postgresql://localhost/db", embedding_provider=None)

        with pytest.raises(ValueError, match="Semantic search requires embedding provider"):
            await search.search("test", search_mode="semantic")

    @pytest.mark.asyncio
    @patch("nexus.search.async_search.create_async_engine_from_url")
    @patch("nexus.search.async_search.async_sessionmaker")
    async def test_shutdown_disposes_engine(self, _sm: Mock, _ce: Mock) -> None:
        """shutdown() should dispose the engine."""
        from nexus.search.async_search import AsyncSemanticSearch

        search = AsyncSemanticSearch("postgresql://localhost/db")
        search.engine = AsyncMock()

        await search.shutdown()

        search.engine.dispose.assert_called_once()


# =============================================================================
# Protocol conformance tests
# =============================================================================


class TestProtocolConformance:
    """Verify AsyncSemanticSearch has all protocol methods (2 tests)."""

    def test_has_all_protocol_methods(self) -> None:
        """AsyncSemanticSearch should have all SearchBrickProtocol methods."""
        from nexus.search.async_search import AsyncSemanticSearch

        for method in [
            "search",
            "index_document",
            "get_stats",
            "initialize",
            "shutdown",
            "verify_imports",
        ]:
            assert hasattr(AsyncSemanticSearch, method), f"Missing {method}"
            assert callable(getattr(AsyncSemanticSearch, method))

    def test_verify_imports_returns_dict(self) -> None:
        """verify_imports() should return a dict with bool values."""
        from nexus.search.async_search import AsyncSemanticSearch

        with (
            patch("nexus.search.async_search.create_async_engine_from_url"),
            patch("nexus.search.async_search.async_sessionmaker"),
        ):
            search = AsyncSemanticSearch("postgresql://localhost/db")
            result = search.verify_imports()

        assert isinstance(result, dict)
        assert all(isinstance(v, bool) for v in result.values())
        assert "nexus.search.fusion" in result
