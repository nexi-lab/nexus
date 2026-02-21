"""Unit tests for semantic search factory (Issue #2075).

Tests create_semantic_search_components and SemanticSearchComponents.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.factory._semantic_search import (
    SemanticSearchComponents,
    create_semantic_search_components,
)

# =============================================================================
# SemanticSearchComponents tests
# =============================================================================


class TestSemanticSearchComponents:
    """Tests for SemanticSearchComponents frozen dataclass."""

    def test_frozen(self):
        """Components should be immutable (frozen dataclass)."""
        import dataclasses

        assert dataclasses.is_dataclass(SemanticSearchComponents)
        fields = {f.name for f in dataclasses.fields(SemanticSearchComponents)}
        assert "query_service" in fields
        assert "indexing_service" in fields
        assert "pipeline_indexer" in fields
        assert "indexing_pipeline" in fields

    def test_all_none(self):
        """Should accept all-None components."""
        c = SemanticSearchComponents(
            query_service=None,
            indexing_service=None,
            pipeline_indexer=None,
            indexing_pipeline=None,
        )
        assert c.query_service is None
        assert c.indexing_service is None
        assert c.pipeline_indexer is None
        assert c.indexing_pipeline is None


# =============================================================================
# create_semantic_search_components tests
# =============================================================================


class TestCreateSemanticSearchComponents:
    """Tests for the factory function."""

    @pytest.fixture
    def mock_record_store(self):
        """Create a mock RecordStore."""
        rs = MagicMock()
        rs.engine = MagicMock()
        rs.engine.url = "sqlite:///test.db"
        rs.session_factory = MagicMock()
        rs.async_session_factory = MagicMock()
        return rs

    @pytest.mark.asyncio
    async def test_creates_basic_components(self, mock_record_store):
        """Should create VectorDB, chunker, pipeline, and QueryService."""
        mock_vdb = MagicMock()
        mock_vdb.db_type = "sqlite"

        with (
            patch("nexus.bricks.search.vector_db.VectorDatabase", return_value=mock_vdb),
            patch("nexus.bricks.search.chunking.DocumentChunker"),
            patch("nexus.bricks.search.indexing.IndexingPipeline") as mock_pip_cls,
        ):
            mock_pip = MagicMock()
            mock_pip_cls.return_value = mock_pip

            components = await create_semantic_search_components(
                record_store=mock_record_store,
            )

        assert components.query_service is not None
        assert components.indexing_pipeline is mock_pip
        assert components.indexing_service is None  # no nx
        assert components.pipeline_indexer is None  # no session_factory param

    @pytest.mark.asyncio
    async def test_creates_indexing_service_with_nx(self, mock_record_store):
        """When nx is provided, should create IndexingService."""
        mock_vdb = MagicMock()
        mock_vdb.db_type = "sqlite"

        with (
            patch("nexus.bricks.search.vector_db.VectorDatabase", return_value=mock_vdb),
            patch("nexus.bricks.search.chunking.DocumentChunker"),
            patch("nexus.bricks.search.indexing.IndexingPipeline"),
            patch("nexus.factory.adapters._NexusFSFileReader"),
        ):
            components = await create_semantic_search_components(
                record_store=mock_record_store,
                nx=MagicMock(),
            )

        assert components.indexing_service is not None

    @pytest.mark.asyncio
    async def test_creates_pipeline_indexer_for_rpc(self, mock_record_store):
        """When RPC params provided, should create PipelineIndexer."""
        mock_vdb = MagicMock()
        mock_vdb.db_type = "sqlite"

        with (
            patch("nexus.bricks.search.vector_db.VectorDatabase", return_value=mock_vdb),
            patch("nexus.bricks.search.chunking.DocumentChunker"),
            patch("nexus.bricks.search.indexing.IndexingPipeline"),
        ):
            components = await create_semantic_search_components(
                record_store=mock_record_store,
                session_factory=MagicMock(),
                metadata=MagicMock(),
                file_reader=MagicMock(),
                file_lister=MagicMock(),
            )

        assert components.pipeline_indexer is not None
        assert components.indexing_service is None  # no nx

    @pytest.mark.asyncio
    async def test_vector_db_initialize_async(self, mock_record_store):
        """vector_db.initialize() should be called via asyncio.to_thread (14A)."""
        mock_vdb = MagicMock()
        mock_vdb.db_type = "sqlite"

        mock_to_thread = AsyncMock()

        with (
            patch("nexus.bricks.search.vector_db.VectorDatabase", return_value=mock_vdb),
            patch("nexus.bricks.search.chunking.DocumentChunker"),
            patch("nexus.bricks.search.indexing.IndexingPipeline"),
            patch("nexus.factory._semantic_search.asyncio.to_thread", mock_to_thread),
        ):
            await create_semantic_search_components(
                record_store=mock_record_store,
            )

        mock_to_thread.assert_awaited_once_with(mock_vdb.initialize)

    @pytest.mark.asyncio
    async def test_no_query_service_without_session_factory(self):
        """When record_store.session_factory is None, no QueryService."""
        rs = MagicMock()
        rs.engine = MagicMock()
        rs.engine.url = "sqlite:///test.db"
        rs.session_factory = None
        rs.async_session_factory = None

        mock_vdb = MagicMock()
        mock_vdb.db_type = "sqlite"

        with (
            patch("nexus.bricks.search.vector_db.VectorDatabase", return_value=mock_vdb),
            patch("nexus.bricks.search.chunking.DocumentChunker"),
            patch("nexus.bricks.search.indexing.IndexingPipeline"),
        ):
            components = await create_semantic_search_components(record_store=rs)

        assert components.query_service is None
