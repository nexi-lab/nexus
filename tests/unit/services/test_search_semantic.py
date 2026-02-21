"""Unit tests for SemanticSearchMixin.

Tests the semantic search mixin methods: engine detection, search delegation,
indexing, and statistics retrieval.

Issue #2132: Previously 0% test coverage.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.search.search_semantic import SemanticSearchMixin

# =============================================================================
# Test harness: concrete class that uses the mixin
# =============================================================================


class _StubSearchService(SemanticSearchMixin):
    """Minimal host class providing the attributes SemanticSearchMixin expects.

    Configurable via constructor kwargs to simulate various engine states.
    """

    def __init__(
        self,
        *,
        query_service: Any = None,
        indexing_service: Any = None,
        indexing_pipeline: Any = None,
        async_search: Any = None,
        semantic_search: Any = None,
        record_store: Any = None,
        gw_session_factory: Any = None,
        gw_backend: Any = None,
    ):
        self._query_service = query_service
        self._indexing_service = indexing_service
        self._indexing_pipeline = indexing_pipeline
        self._async_search = async_search
        self._semantic_search = semantic_search
        self._record_store = record_store
        self._gw_session_factory = gw_session_factory
        self._gw_backend = gw_backend
        self.metadata = MagicMock()
        self._read = MagicMock()
        self.list = MagicMock()


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_query_service():
    """Create a mock QueryService."""
    qs = AsyncMock()
    qs.search.return_value = [
        SimpleNamespace(
            path="/doc.txt",
            chunk_index=0,
            chunk_text="hello world",
            score=0.95,
            start_offset=0,
            end_offset=11,
            line_start=1,
            line_end=1,
        ),
    ]
    return qs


@pytest.fixture
def mock_indexing_service():
    """Create a mock IndexingService."""
    idxs = AsyncMock()
    idxs.index_document.return_value = 5
    idxs.index_directory.return_value = {
        "/dir/a.txt": SimpleNamespace(chunks_indexed=3),
        "/dir/b.txt": SimpleNamespace(chunks_indexed=7),
    }
    idxs.get_index_stats.return_value = {
        "total_documents": 10,
        "total_chunks": 100,
    }
    return idxs


@pytest.fixture
def svc_with_engine(mock_query_service, mock_indexing_service):
    """Create a _StubSearchService with query and indexing services."""
    return _StubSearchService(
        query_service=mock_query_service,
        indexing_service=mock_indexing_service,
    )


@pytest.fixture
def svc_no_engine():
    """Create a _StubSearchService with no search engine."""
    return _StubSearchService()


@pytest.fixture
def svc_legacy_engine():
    """Create a _StubSearchService with only legacy _async_search."""
    legacy = AsyncMock()
    legacy.search.return_value = [
        SimpleNamespace(
            path="/legacy.txt",
            chunk_index=0,
            chunk_text="legacy result",
            score=0.80,
            start_offset=0,
            end_offset=13,
            line_start=1,
            line_end=1,
        ),
    ]
    legacy.get_stats.return_value = {"total_documents": 5}
    return _StubSearchService(async_search=legacy)


# =============================================================================
# _has_search_engine tests
# =============================================================================


class TestHasSearchEngine:
    """Tests for SemanticSearchMixin._has_search_engine property."""

    def test_true_with_query_service(self, svc_with_engine):
        """Should return True when _query_service is set."""
        assert svc_with_engine._has_search_engine is True

    def test_true_with_async_search(self, svc_legacy_engine):
        """Should return True when _async_search is set (legacy)."""
        assert svc_legacy_engine._has_search_engine is True

    def test_true_with_semantic_search(self):
        """Should return True when _semantic_search is set (legacy)."""
        svc = _StubSearchService(semantic_search=MagicMock())
        assert svc._has_search_engine is True

    def test_false_when_nothing_set(self, svc_no_engine):
        """Should return False when no engine is configured."""
        assert svc_no_engine._has_search_engine is False


# =============================================================================
# _require_search_engine tests
# =============================================================================


class TestRequireSearchEngine:
    """Tests for SemanticSearchMixin._require_search_engine()."""

    def test_raises_when_no_engine(self, svc_no_engine):
        """Should raise ValueError when no engine is initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            svc_no_engine._require_search_engine()

    def test_no_raise_when_engine_present(self, svc_with_engine):
        """Should not raise when engine is available."""
        svc_with_engine._require_search_engine()  # Should not raise


# =============================================================================
# semantic_search() tests
# =============================================================================


class TestSemanticSearch:
    """Tests for SemanticSearchMixin.semantic_search()."""

    @pytest.mark.asyncio
    async def test_delegates_to_query_service(self, svc_with_engine, mock_query_service):
        """semantic_search should delegate to QueryService.search()."""
        results = await svc_with_engine.semantic_search(
            query="hello",
            path="/docs",
            limit=5,
            search_mode="semantic",
        )

        mock_query_service.search.assert_awaited_once_with(
            query="hello",
            path="/docs",
            limit=5,
            search_mode="semantic",
            adaptive_k=False,
        )
        assert len(results) == 1
        assert results[0]["path"] == "/doc.txt"
        assert results[0]["chunk_text"] == "hello world"
        assert results[0]["score"] == 0.95
        assert results[0]["chunk_index"] == 0

    @pytest.mark.asyncio
    async def test_returns_all_result_fields(self, svc_with_engine):
        """Each result should contain all expected fields."""
        results = await svc_with_engine.semantic_search(query="test")
        assert len(results) == 1
        expected_keys = {
            "path",
            "chunk_index",
            "chunk_text",
            "score",
            "start_offset",
            "end_offset",
            "line_start",
            "line_end",
        }
        assert set(results[0].keys()) == expected_keys

    @pytest.mark.asyncio
    async def test_fallback_to_legacy_async_search(self, svc_legacy_engine):
        """When no QueryService, should fall back to _async_search."""
        results = await svc_legacy_engine.semantic_search(query="legacy query")
        assert len(results) == 1
        assert results[0]["path"] == "/legacy.txt"
        assert results[0]["chunk_text"] == "legacy result"

    @pytest.mark.asyncio
    async def test_raises_when_no_engine(self, svc_no_engine):
        """semantic_search should raise ValueError without engine."""
        with pytest.raises(ValueError, match="not initialized"):
            await svc_no_engine.semantic_search(query="test")

    @pytest.mark.asyncio
    async def test_raises_when_query_service_none_and_no_fallback(self):
        """Should raise when QueryService is None and no legacy fallback."""
        svc = _StubSearchService(semantic_search=MagicMock())
        # _has_search_engine is True (semantic_search is set), but
        # both _query_service and _async_search are None -> last raise
        with pytest.raises(ValueError, match="not properly initialized"):
            await svc.semantic_search(query="test")

    @pytest.mark.asyncio
    async def test_adaptive_k_passed_through(self, svc_with_engine, mock_query_service):
        """adaptive_k parameter should be passed to QueryService."""
        await svc_with_engine.semantic_search(
            query="test",
            adaptive_k=True,
        )
        call_kwargs = mock_query_service.search.call_args.kwargs
        assert call_kwargs["adaptive_k"] is True


# =============================================================================
# semantic_search_index() tests
# =============================================================================


class TestSemanticSearchIndex:
    """Tests for SemanticSearchMixin.semantic_search_index()."""

    @pytest.mark.asyncio
    async def test_indexes_single_document(self, svc_with_engine, mock_indexing_service):
        """Should index a single document when IndexingService is available."""
        result = await svc_with_engine.semantic_search_index(path="/doc.txt")
        mock_indexing_service.index_document.assert_awaited_once_with("/doc.txt")
        assert result == {"/doc.txt": 5}

    @pytest.mark.asyncio
    async def test_indexes_directory_recursively(self, svc_with_engine, mock_indexing_service):
        """Should index directory when single-file index raises ValueError."""
        mock_indexing_service.index_document.side_effect = ValueError("not a file")

        result = await svc_with_engine.semantic_search_index(
            path="/dir",
            recursive=True,
        )
        mock_indexing_service.index_directory.assert_awaited_once_with("/dir")
        assert result == {"/dir/a.txt": 3, "/dir/b.txt": 7}

    @pytest.mark.asyncio
    async def test_non_recursive_returns_empty_on_directory(
        self, svc_with_engine, mock_indexing_service
    ):
        """When recursive=False and path is a directory, should return empty."""
        mock_indexing_service.index_document.side_effect = ValueError("not a file")

        result = await svc_with_engine.semantic_search_index(
            path="/dir",
            recursive=False,
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_raises_when_no_engine(self, svc_no_engine):
        """semantic_search_index should raise ValueError without engine."""
        with pytest.raises(ValueError, match="not initialized"):
            await svc_no_engine.semantic_search_index(path="/doc.txt")

    @pytest.mark.asyncio
    async def test_falls_back_to_pipeline_index(self):
        """When no IndexingService, should fall back to _pipeline_index_documents."""
        svc = _StubSearchService(query_service=MagicMock())
        svc._indexing_service = None

        # Mock _pipeline_index_documents
        svc._pipeline_index_documents = AsyncMock(return_value={"/x.txt": 2})

        result = await svc.semantic_search_index(path="/x.txt")
        svc._pipeline_index_documents.assert_awaited_once_with("/x.txt", True)
        assert result == {"/x.txt": 2}


# =============================================================================
# semantic_search_stats() tests
# =============================================================================


class TestSemanticSearchStats:
    """Tests for SemanticSearchMixin.semantic_search_stats()."""

    @pytest.mark.asyncio
    async def test_delegates_to_indexing_service(self, svc_with_engine, mock_indexing_service):
        """Should delegate to IndexingService.get_index_stats()."""
        result = await svc_with_engine.semantic_search_stats()
        mock_indexing_service.get_index_stats.assert_awaited_once()
        assert result["total_documents"] == 10
        assert result["total_chunks"] == 100

    @pytest.mark.asyncio
    async def test_fallback_to_legacy_stats(self, svc_legacy_engine):
        """Should fall back to legacy _async_search.get_stats()."""
        result = await svc_legacy_engine.semantic_search_stats()
        assert result["total_documents"] == 5

    @pytest.mark.asyncio
    async def test_raises_when_no_engine(self, svc_no_engine):
        """semantic_search_stats should raise ValueError without engine."""
        with pytest.raises(ValueError, match="not initialized"):
            await svc_no_engine.semantic_search_stats()

    @pytest.mark.asyncio
    async def test_raises_when_no_indexing_and_no_legacy(self):
        """Should raise when IndexingService is None and no legacy fallback."""
        svc = _StubSearchService(query_service=MagicMock())
        svc._indexing_service = None
        with pytest.raises(ValueError, match="not properly initialized"):
            await svc.semantic_search_stats()
