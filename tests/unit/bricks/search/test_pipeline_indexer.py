"""Unit tests for PipelineIndexer (Issue #2075).

Tests the extracted PipelineIndexer class: file resolution, batch document
preparation, path_id lookup, and pipeline delegation.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus.bricks.search.pipeline_indexer import PipelineIndexer

# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_pipeline():
    """Create a mock IndexingPipeline."""
    pipeline = AsyncMock()
    pipeline.index_documents.return_value = [
        SimpleNamespace(path="/a.txt", chunks_indexed=3),
        SimpleNamespace(path="/b.txt", chunks_indexed=5),
    ]
    return pipeline


@pytest.fixture
def mock_session_factory():
    """Create a mock session factory that returns a context-managed session."""
    session = MagicMock()
    session.execute.return_value = iter([])  # empty by default

    factory = MagicMock()
    factory.return_value.__enter__ = MagicMock(return_value=session)
    factory.return_value.__exit__ = MagicMock(return_value=False)
    return factory


@pytest.fixture
def mock_metadata():
    """Create a mock metadata store with get_searchable_text_bulk."""
    meta = MagicMock()
    meta.get_searchable_text_bulk.return_value = {
        "/a.txt": "content a",
        "/b.txt": "content b",
    }
    return meta


@pytest.fixture
def mock_file_reader():
    """File reader that reads files or raises for directories."""

    def reader(path: str) -> bytes:
        if path.endswith("/"):
            raise IsADirectoryError(path)
        return f"raw content of {path}".encode()

    return reader


@pytest.fixture
def mock_file_lister():
    """File lister that returns a list of file paths."""

    def lister(path: str, recursive: bool) -> list[str]:  # noqa: ARG001
        return ["/a.txt", "/b.txt", "/dir/"]

    return lister


@pytest.fixture
def indexer(mock_pipeline, mock_session_factory, mock_metadata, mock_file_reader, mock_file_lister):
    """Create a PipelineIndexer with all mocks."""
    return PipelineIndexer(
        pipeline=mock_pipeline,
        session_factory=mock_session_factory,
        metadata=mock_metadata,
        file_reader=mock_file_reader,
        file_lister=mock_file_lister,
    )


# =============================================================================
# _resolve_files tests
# =============================================================================


class TestResolveFiles:
    """Tests for PipelineIndexer._resolve_files()."""

    @pytest.mark.asyncio
    async def test_single_file(self, indexer):
        """When path is a file (reader succeeds), return it as single-item list."""
        files = await indexer._resolve_files("/a.txt", recursive=True)
        assert files == ["/a.txt"]

    @pytest.mark.asyncio
    async def test_directory_lists_files(self, indexer):
        """When reader raises (path is a directory), list files."""
        # Force reader to raise for directory path
        indexer._file_reader = MagicMock(side_effect=IsADirectoryError)
        files = await indexer._resolve_files("/dir", recursive=True)
        # Should exclude "/dir/" (ends with /)
        assert "/a.txt" in files
        assert "/b.txt" in files
        assert "/dir/" not in files


# =============================================================================
# _prepare_documents tests
# =============================================================================


class TestPrepareDocuments:
    """Tests for PipelineIndexer._prepare_documents()."""

    def test_uses_bulk_searchable_text(self, indexer, mock_metadata, mock_session_factory):
        """Should use get_searchable_text_bulk for batch text lookup."""
        # Set up session to return path_ids
        mock_session = mock_session_factory.return_value.__enter__.return_value
        mock_session.execute.return_value = iter(
            [
                SimpleNamespace(virtual_path="/a.txt", path_id="id-a"),
                SimpleNamespace(virtual_path="/b.txt", path_id="id-b"),
            ]
        )

        docs = indexer._prepare_documents(["/a.txt", "/b.txt"])
        mock_metadata.get_searchable_text_bulk.assert_called_once_with(["/a.txt", "/b.txt"])
        assert len(docs) == 2
        assert docs[0] == ("/a.txt", "content a", "id-a")
        assert docs[1] == ("/b.txt", "content b", "id-b")

    def test_falls_back_to_file_reader(self, indexer, mock_metadata, mock_session_factory):
        """When bulk text doesn't include a file, fall back to file_reader."""
        mock_metadata.get_searchable_text_bulk.return_value = {
            "/a.txt": "content a",
        }

        mock_session = mock_session_factory.return_value.__enter__.return_value
        mock_session.execute.return_value = iter(
            [
                SimpleNamespace(virtual_path="/a.txt", path_id="id-a"),
                SimpleNamespace(virtual_path="/b.txt", path_id="id-b"),
            ]
        )

        docs = indexer._prepare_documents(["/a.txt", "/b.txt"])
        assert len(docs) == 2
        # /b.txt should have been read via file_reader
        assert docs[1][0] == "/b.txt"
        assert "raw content of /b.txt" in docs[1][1]

    def test_skips_files_without_path_id(self, indexer, mock_session_factory):
        """Files not found in DB should be excluded."""
        mock_session = mock_session_factory.return_value.__enter__.return_value
        # Only /a.txt has a path_id
        mock_session.execute.return_value = iter(
            [SimpleNamespace(virtual_path="/a.txt", path_id="id-a")]
        )

        docs = indexer._prepare_documents(["/a.txt", "/b.txt"])
        assert len(docs) == 1
        assert docs[0][0] == "/a.txt"

    def test_empty_files_list(self, indexer):
        """Should return empty list for empty input."""
        docs = indexer._prepare_documents([])
        assert docs == []

    def test_no_bulk_method_falls_back_to_individual(self, indexer, mock_session_factory):
        """When metadata lacks get_searchable_text_bulk, use individual calls."""
        del indexer._metadata.get_searchable_text_bulk
        indexer._metadata.get_searchable_text.side_effect = lambda p: f"text-{p}"

        mock_session = mock_session_factory.return_value.__enter__.return_value
        mock_session.execute.return_value = iter(
            [SimpleNamespace(virtual_path="/a.txt", path_id="id-a")]
        )

        docs = indexer._prepare_documents(["/a.txt"])
        assert len(docs) == 1
        assert docs[0][1] == "text-/a.txt"


# =============================================================================
# index_path tests
# =============================================================================


class TestIndexPath:
    """Tests for PipelineIndexer.index_path()."""

    @pytest.mark.asyncio
    async def test_indexes_files_and_returns_results(self, indexer, mock_pipeline):
        """Should resolve files, prepare docs, and delegate to pipeline."""
        # Need to patch _prepare_documents since it needs DB
        with patch.object(
            indexer,
            "_prepare_documents",
            return_value=[
                ("/a.txt", "content a", "id-a"),
                ("/b.txt", "content b", "id-b"),
            ],
        ):
            result = await indexer.index_path("/dir", recursive=True)

        mock_pipeline.index_documents.assert_awaited_once()
        assert result == {"/a.txt": 3, "/b.txt": 5}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_files(self, indexer):
        """Should return empty dict when no files to index."""
        indexer._file_reader = MagicMock(side_effect=IsADirectoryError)
        indexer._file_lister = MagicMock(return_value=[])
        result = await indexer.index_path("/empty-dir")
        assert result == {}

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_documents(self, indexer):
        """Should return empty dict when no docs prepared (no DB matches)."""
        with patch.object(indexer, "_prepare_documents", return_value=[]):
            result = await indexer.index_path("/dir")
        assert result == {}
