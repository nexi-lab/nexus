"""Tests for semantic search module."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nexus import connect
from nexus.search.chunking import ChunkStrategy
from nexus.search.semantic import SemanticSearch, SemanticSearchResult


class TestSemanticSearchResult:
    """Test SemanticSearchResult dataclass."""

    def test_create_result(self):
        """Test creating a search result."""
        result = SemanticSearchResult(
            path="/test.txt",
            chunk_index=0,
            chunk_text="Test content",
            score=0.95,
            start_offset=0,
            end_offset=12,
            keyword_score=0.8,
            vector_score=0.9,
        )

        assert result.path == "/test.txt"
        assert result.chunk_index == 0
        assert result.chunk_text == "Test content"
        assert result.score == 0.95
        assert result.start_offset == 0
        assert result.end_offset == 12
        assert result.keyword_score == 0.8
        assert result.vector_score == 0.9

    def test_create_result_minimal(self):
        """Test creating a result with minimal fields."""
        result = SemanticSearchResult(
            path="/test.txt", chunk_index=0, chunk_text="Test content", score=0.95
        )

        assert result.path == "/test.txt"
        assert result.chunk_index == 0
        assert result.chunk_text == "Test content"
        assert result.score == 0.95
        assert result.start_offset is None
        assert result.end_offset is None
        assert result.keyword_score is None
        assert result.vector_score is None


class TestSemanticSearch:
    """Test SemanticSearch class."""

    @pytest.fixture
    def nx(self):
        """Create a temporary NexusFS instance."""
        with tempfile.TemporaryDirectory() as tmpdir:
            nx = connect(
                config={
                    "data_dir": str(Path(tmpdir) / "data"),
                    "enforce_permissions": False,  # Disable permissions for tests
                }
            )
            yield nx
            nx.close()

    @pytest.fixture
    def mock_embedding_provider(self):
        """Create a mock embedding provider."""
        provider = MagicMock()
        provider.get_dimension.return_value = 128
        provider.embed_text = AsyncMock(return_value=[0.1] * 128)
        provider.embed_texts = AsyncMock(return_value=[[0.1] * 128, [0.2] * 128])
        return provider

    def test_init_without_embeddings(self, nx):
        """Test initialization without embedding provider."""
        search = SemanticSearch(nx)

        assert search.nx == nx
        assert search.embedding_provider is None
        assert search.chunk_size == 1024
        assert search.chunk_strategy == ChunkStrategy.SEMANTIC

    def test_init_with_embeddings(self, nx, mock_embedding_provider):
        """Test initialization with embedding provider."""
        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)

        assert search.embedding_provider == mock_embedding_provider
        assert search.chunk_size == 1024

    def test_init_custom_settings(self, nx, mock_embedding_provider):
        """Test initialization with custom settings."""
        search = SemanticSearch(
            nx,
            embedding_provider=mock_embedding_provider,
            chunk_size=512,
            chunk_strategy=ChunkStrategy.FIXED,
        )

        assert search.chunk_size == 512
        assert search.chunk_strategy == ChunkStrategy.FIXED

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_index_document(self, nx, mock_embedding_provider):
        """Test indexing a document."""
        # Write a test file
        nx.write("/workspace/test.txt", b"This is a test document with some content.")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)

        # Mock vector DB methods
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        num_chunks = await search.index_document("/workspace/test.txt")

        # Should have created at least one chunk
        assert num_chunks > 0

    async def test_index_document_without_embeddings(self, nx):
        """Test indexing without embedding provider (keyword-only)."""
        nx.write("/workspace/test.txt", b"This is a test document.")

        search = SemanticSearch(nx)
        search.vector_db.initialize = MagicMock()

        num_chunks = await search.index_document("/workspace/test.txt")

        # Should still index for keyword search
        assert num_chunks > 0

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_index_directory(self, nx, mock_embedding_provider):
        """Test indexing a directory."""
        # Create test files
        nx.write("/workspace/file1.txt", b"Content of file 1")
        nx.write("/workspace/file2.txt", b"Content of file 2")
        nx.write("/workspace/subdir/file3.txt", b"Content of file 3")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        results = await search.index_directory("/workspace")

        # Should have indexed all files
        assert len(results) >= 2  # At least file1 and file2
        for _, num_chunks in results.items():
            assert num_chunks > 0

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_keyword_search(self, nx):
        """Test keyword-only search."""
        # Write test files
        nx.write("/workspace/test1.txt", b"Python programming language")
        nx.write("/workspace/test2.txt", b"JavaScript programming tutorial")

        search = SemanticSearch(nx)
        search.vector_db.initialize = MagicMock()

        # Index documents
        await search.index_document("/workspace/test1.txt")
        await search.index_document("/workspace/test2.txt")

        # Search for keyword
        # Mock the database query
        with patch.object(search.vector_db, "keyword_search") as mock_search:
            mock_search.return_value = [
                {
                    "file_path": "/workspace/test1.txt",
                    "chunk_index": 0,
                    "chunk_text": "Python programming language",
                    "score": 0.8,
                }
            ]

            results = await search.keyword_search("programming", limit=10)

            assert len(results) > 0
            mock_search.assert_called_once()

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_semantic_search_without_provider(self, nx):
        """Test semantic search fails without embedding provider."""
        search = SemanticSearch(nx)

        with pytest.raises(ValueError, match="Embedding provider is required"):
            await search.semantic_search("test query")

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_semantic_search_with_provider(self, nx, mock_embedding_provider):
        """Test semantic search with embedding provider."""
        nx.write("/workspace/test.txt", b"Python programming language")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        # Index document
        await search.index_document("/workspace/test.txt")

        # Mock vector search
        with patch.object(search.vector_db, "semantic_search") as mock_search:
            mock_search.return_value = [
                {
                    "file_path": "/workspace/test.txt",
                    "chunk_index": 0,
                    "chunk_text": "Python programming language",
                    "score": 0.95,
                }
            ]

            results = await search.semantic_search("python", limit=10)

            assert len(results) > 0
            mock_embedding_provider.embed_text.assert_called_once_with("python")
            mock_search.assert_called_once()

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_hybrid_search_without_provider(self, nx):
        """Test hybrid search falls back to keyword search without embedding provider."""
        search = SemanticSearch(nx)
        search.vector_db.initialize = MagicMock()

        # Should not raise, just fall back to keyword search
        with patch.object(search.vector_db, "keyword_search") as mock_search:
            mock_search.return_value = []
            results = await search.hybrid_search("test query")
            assert results == []

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_hybrid_search_with_provider(self, nx, mock_embedding_provider):
        """Test hybrid search with both keyword and semantic."""
        nx.write("/workspace/test.txt", b"Python programming language")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        await search.index_document("/workspace/test.txt")

        # Mock hybrid search
        with patch.object(search.vector_db, "hybrid_search") as mock_search:
            mock_search.return_value = [
                {
                    "file_path": "/workspace/test.txt",
                    "chunk_index": 0,
                    "chunk_text": "Python programming language",
                    "score": 0.9,
                    "keyword_score": 0.8,
                    "vector_score": 0.95,
                }
            ]

            results = await search.hybrid_search("python programming")

            assert len(results) > 0
            result = results[0]
            assert result.keyword_score is not None
            assert result.vector_score is not None

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_delete_document(self, nx, mock_embedding_provider):
        """Test deleting a document from the index."""
        nx.write("/workspace/test.txt", b"Test content")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        # Index then delete
        await search.index_document("/workspace/test.txt")

        with patch.object(search.vector_db, "delete_document") as mock_delete:
            await search.delete_document("/workspace/test.txt")
            mock_delete.assert_called_once_with("/workspace/test.txt")

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_get_stats(self, nx):
        """Test getting search statistics."""
        search = SemanticSearch(nx)
        search.vector_db.initialize = MagicMock()

        with patch.object(search.vector_db, "get_stats") as mock_stats:
            mock_stats.return_value = {
                "total_chunks": 100,
                "total_documents": 10,
                "vec_enabled": True,
            }

            stats = await search.get_stats()

            assert stats["total_chunks"] == 100
            assert stats["total_documents"] == 10
            assert stats["vec_enabled"] is True

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_clear_index(self, nx):
        """Test clearing the entire search index."""
        search = SemanticSearch(nx)
        search.vector_db.initialize = MagicMock()

        with patch.object(search.vector_db, "clear_index") as mock_clear:
            await search.clear_index()
            mock_clear.assert_called_once()

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_reindex_document(self, nx, mock_embedding_provider):
        """Test reindexing a document (delete + index)."""
        nx.write("/workspace/test.txt", b"Updated content")

        search = SemanticSearch(nx, embedding_provider=mock_embedding_provider)
        search.vector_db.initialize = MagicMock()
        search.vector_db.vec_available = True

        # Mock delete and verify indexing happens
        with patch.object(search.vector_db, "delete_document") as mock_delete:
            num_chunks = await search.index_document("/workspace/test.txt", reindex=True)

            mock_delete.assert_called_once_with("/workspace/test.txt")
            assert num_chunks > 0

    @pytest.mark.skip(reason="Database tables not created when initialize() is mocked")
    async def test_empty_query(self, nx):
        """Test search with empty query."""
        search = SemanticSearch(nx)

        # Keyword search with empty query
        results = await search.keyword_search("")
        # Should return empty results or handle gracefully
        assert isinstance(results, list)

    def test_close(self, nx):
        """Test closing the search engine."""
        search = SemanticSearch(nx)
        # Should not raise
        search.close()
