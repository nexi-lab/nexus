"""Unit tests for SearchService semantic search methods.

Tests the semantic search methods (formerly SemanticSearchMixin, now inlined):
engine detection, search delegation, indexing, and statistics retrieval.

Issue #2132: Previously 0% test coverage.
Issue #2075: Updated for decomposed architecture.
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.bricks.search.search_service import SearchService

# =============================================================================
# Test harness: lightweight SearchService with only semantic attrs
# =============================================================================


def _make_svc(
    *,
    query_service: Any = None,
    indexing_service: Any = None,
    indexing_pipeline: Any = None,
    pipeline_indexer: Any = None,
    record_store: Any = None,
    gw_session_factory: Any = None,
) -> SearchService:
    """Create a SearchService instance with mocked metadata, setting semantic attrs."""
    svc = SearchService(
        metadata_store=MagicMock(),
        enforce_permissions=False,
        record_store=record_store,
    )
    svc._query_service = query_service
    svc._indexing_service = indexing_service
    svc._indexing_pipeline = indexing_pipeline
    svc._pipeline_indexer = pipeline_indexer
    # Override gateway session factory property via _gw mock
    if gw_session_factory is not None:
        gw = MagicMock()
        gw.session_factory = gw_session_factory
        svc._gw = gw
    return svc


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
def mock_pipeline_indexer():
    """Create a mock PipelineIndexer."""
    pi = AsyncMock()
    pi.index_path.return_value = {"/x.txt": 2}
    return pi


@pytest.fixture
def svc_with_engine(mock_query_service, mock_indexing_service):
    """Create a SearchService with query and indexing services."""
    return _make_svc(
        query_service=mock_query_service,
        indexing_service=mock_indexing_service,
    )


@pytest.fixture
def svc_no_engine():
    """Create a SearchService with no search engine."""
    return _make_svc()


# =============================================================================
# _has_search_engine tests
# =============================================================================


class TestHasSearchEngine:
    """Tests for SearchService._has_search_engine property."""

    def test_true_with_query_service(self, svc_with_engine):
        """Should return True when _query_service is set."""
        assert svc_with_engine._has_search_engine is True

    def test_false_when_nothing_set(self, svc_no_engine):
        """Should return False when no engine is configured."""
        assert svc_no_engine._has_search_engine is False

    def test_false_when_query_service_is_none(self):
        """Should return False when _query_service is explicitly None."""
        svc = _make_svc(query_service=None)
        assert svc._has_search_engine is False


# =============================================================================
# _require_search_engine tests
# =============================================================================


class TestRequireSearchEngine:
    """Tests for SearchService._require_search_engine()."""

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
    """Tests for SearchService.semantic_search()."""

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
    async def test_raises_when_no_engine(self, svc_no_engine):
        """semantic_search should raise ValueError without engine or record store."""
        with pytest.raises(ValueError, match="not available"):
            await svc_no_engine.semantic_search(query="test")

    @pytest.mark.asyncio
    async def test_raises_when_query_service_none(self):
        """Should raise when _query_service is None and no record_store."""
        svc = _make_svc(query_service=None)
        with pytest.raises(ValueError, match="not available"):
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

    @pytest.mark.asyncio
    async def test_empty_results(self, svc_with_engine, mock_query_service):
        """Should return empty list when no results found."""
        mock_query_service.search.return_value = []
        results = await svc_with_engine.semantic_search(query="nothing")
        assert results == []


# =============================================================================
# semantic_search_index() tests
# =============================================================================


class TestSemanticSearchIndex:
    """Tests for SearchService.semantic_search_index()."""

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
    async def test_returns_empty_when_no_engine(self, svc_no_engine):
        """semantic_search_index returns empty dict without engine (no raise)."""
        result = await svc_no_engine.semantic_search_index(path="/doc.txt")
        assert result == {}

    @pytest.mark.asyncio
    async def test_falls_back_to_pipeline_indexer(self, mock_pipeline_indexer):
        """When no IndexingService, should fall back to PipelineIndexer."""
        svc = _make_svc(
            query_service=MagicMock(),
            pipeline_indexer=mock_pipeline_indexer,
        )

        result = await svc.semantic_search_index(path="/x.txt")
        mock_pipeline_indexer.index_path.assert_awaited_once_with("/x.txt", True)
        assert result == {"/x.txt": 2}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_indexer_and_no_pipeline(self):
        """Should return empty dict when neither IndexingService nor PipelineIndexer."""
        svc = _make_svc(query_service=MagicMock())
        result = await svc.semantic_search_index(path="/orphan.txt")
        assert result == {}


# =============================================================================
# semantic_search_stats() tests
# =============================================================================


class TestSemanticSearchStats:
    """Tests for SearchService.semantic_search_stats()."""

    @pytest.mark.asyncio
    async def test_delegates_to_indexing_service(self, svc_with_engine, mock_indexing_service):
        """Should delegate to IndexingService.get_index_stats()."""
        result = await svc_with_engine.semantic_search_stats()
        mock_indexing_service.get_index_stats.assert_awaited_once()
        assert result["total_documents"] == 10
        assert result["total_chunks"] == 100

    @pytest.mark.asyncio
    async def test_raises_when_no_engine(self, svc_no_engine):
        """semantic_search_stats should raise ValueError without engine or record store."""
        with pytest.raises(ValueError, match="not available"):
            await svc_no_engine.semantic_search_stats()

    @pytest.mark.asyncio
    async def test_raises_when_no_indexing_service(self):
        """Should raise when IndexingService is None and no record_store."""
        svc = _make_svc(query_service=MagicMock())
        with pytest.raises(ValueError, match="not available"):
            await svc.semantic_search_stats()


# =============================================================================
# ainitialize_semantic_search() tests
# =============================================================================


class TestAinitializeSemanticSearch:
    """Tests for SearchService.ainitialize_semantic_search()."""

    @pytest.mark.asyncio
    async def test_raises_without_record_store(self):
        """Should raise RuntimeError when record_store is None."""
        svc = _make_svc()
        with pytest.raises(RuntimeError, match="RecordStore"):
            await svc.ainitialize_semantic_search(nx=MagicMock(), record_store_engine=None)

    @pytest.mark.asyncio
    async def test_delegates_to_factory(self, monkeypatch):
        """Should call create_semantic_search_components and assign results."""
        components = SimpleNamespace(
            query_service=MagicMock(),
            indexing_service=MagicMock(),
            indexing_pipeline=MagicMock(),
            pipeline_indexer=None,
        )
        mock_create = AsyncMock(return_value=components)

        # Import and patch at module level
        import nexus.factory._semantic_search as factory_mod

        monkeypatch.setattr(factory_mod, "create_semantic_search_components", mock_create)

        svc = _make_svc(record_store=MagicMock())
        await svc.ainitialize_semantic_search(nx=MagicMock(), record_store_engine=None)

        assert svc._query_service is components.query_service
        assert svc._indexing_service is components.indexing_service
        assert svc._indexing_pipeline is components.indexing_pipeline


# =============================================================================
# initialize_semantic_search() (RPC path) tests
# =============================================================================


class TestInitializeSemanticSearch:
    """Tests for SearchService.initialize_semantic_search() (RPC path)."""

    @pytest.mark.asyncio
    async def test_raises_without_record_store(self):
        """Should raise RuntimeError when record_store is None."""
        svc = _make_svc()
        with pytest.raises(RuntimeError, match="RecordStore"):
            await svc.initialize_semantic_search()

    @pytest.mark.asyncio
    async def test_delegates_to_factory_with_rpc_params(self, monkeypatch):
        """Should call factory with RPC-path extras (session_factory, metadata, etc.)."""
        components = SimpleNamespace(
            query_service=MagicMock(),
            indexing_service=None,
            indexing_pipeline=MagicMock(),
            pipeline_indexer=MagicMock(),
        )
        mock_create = AsyncMock(return_value=components)

        import nexus.factory._semantic_search as factory_mod

        monkeypatch.setattr(factory_mod, "create_semantic_search_components", mock_create)

        mock_rs = MagicMock()
        svc = _make_svc(
            record_store=mock_rs,
            gw_session_factory=MagicMock(),
        )
        await svc.initialize_semantic_search(embedding_provider="openai")

        assert svc._query_service is components.query_service
        assert svc._pipeline_indexer is components.pipeline_indexer
        # Verify factory was called with the right embedding_provider
        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["embedding_provider"] == "openai"
        assert call_kwargs["record_store"] is mock_rs
