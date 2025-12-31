"""Unit tests for BM25S search module (Issue #796).

Tests for:
- CodeTokenizer: camelCase, snake_case, PascalCase splitting
- BM25SIndex: indexing, searching, persistence
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest


class TestCodeTokenizer:
    """Tests for code-aware tokenization."""

    @pytest.fixture
    def tokenizer(self):
        """Create a CodeTokenizer instance."""
        from nexus.search.bm25s_search import CodeTokenizer

        return CodeTokenizer()

    def test_split_camel_case(self, tokenizer):
        """Test camelCase splitting."""
        assert tokenizer.split_identifier("getUserName") == ["get", "user", "name"]
        assert tokenizer.split_identifier("parseJSON") == ["parse", "json"]
        assert tokenizer.split_identifier("loadHTTPRequest") == ["load", "http", "request"]

    def test_split_pascal_case(self, tokenizer):
        """Test PascalCase splitting."""
        assert tokenizer.split_identifier("UserName") == ["user", "name"]
        assert tokenizer.split_identifier("HTTPServer") == ["http", "server"]
        assert tokenizer.split_identifier("XMLParser") == ["xml", "parser"]

    def test_split_snake_case(self, tokenizer):
        """Test snake_case splitting."""
        assert tokenizer.split_identifier("get_user_name") == ["get", "user", "name"]
        assert tokenizer.split_identifier("MAX_BUFFER_SIZE") == ["max", "buffer", "size"]

    def test_split_mixed_case(self, tokenizer):
        """Test mixed case identifiers."""
        assert tokenizer.split_identifier("get_userName") == ["get", "user", "name"]
        assert tokenizer.split_identifier("HTTP_Request_Handler") == [
            "http",
            "request",
            "handler",
        ]

    def test_tokenize_code_snippet(self, tokenizer):
        """Test tokenizing a code snippet."""
        code = """
        def getUserName(self):
            return self.first_name + " " + self.lastName
        """
        tokens = tokenizer.tokenize(code)

        # Should include split identifiers
        assert "get" in tokens
        assert "user" in tokens
        assert "name" in tokens
        assert "first" in tokens
        assert "last" in tokens
        assert "return" in tokens

        # Should exclude stopwords
        assert "self" not in tokens
        assert "def" in tokens  # Not a stopword

    def test_tokenize_empty_string(self, tokenizer):
        """Test tokenizing empty string."""
        assert tokenizer.tokenize("") == []

    def test_tokenize_numbers(self, tokenizer):
        """Test that numbers are preserved."""
        tokens = tokenizer.tokenize("user123 version456")
        assert "user" in tokens
        assert "123" in tokens
        assert "version" in tokens
        assert "456" in tokens

    def test_tokenize_batch(self, tokenizer):
        """Test batch tokenization."""
        texts = [
            "getUserName",
            "set_user_name",
            "HTTPRequest",
        ]
        results = tokenizer.tokenize_batch(texts)

        assert len(results) == 3
        assert "get" in results[0]
        assert "set" in results[1]
        assert "http" in results[2]

    def test_min_token_length(self, tokenizer):
        """Test minimum token length filtering."""
        tokens = tokenizer.tokenize("a b c def ghi")

        # Single char tokens should be filtered (min_length=2)
        assert "a" not in tokens
        assert "b" not in tokens
        assert "c" not in tokens
        assert "def" in tokens
        assert "ghi" in tokens

    def test_stopwords_filtering(self, tokenizer):
        """Test stopword filtering."""
        tokens = tokenizer.tokenize("this is a test of the function")

        # Stopwords should be filtered
        assert "this" not in tokens
        assert "is" not in tokens
        assert "the" not in tokens
        assert "of" not in tokens

        # Non-stopwords should be kept
        assert "test" in tokens
        assert "function" in tokens


class TestBM25SIndex:
    """Tests for BM25S index operations."""

    @pytest.fixture
    def temp_index_dir(self):
        """Create a temporary index directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir) / "bm25s"

    @pytest.fixture
    def index(self, temp_index_dir):
        """Create a BM25SIndex instance."""
        from nexus.search.bm25s_search import BM25SIndex

        return BM25SIndex(index_dir=temp_index_dir)

    @pytest.mark.asyncio
    async def test_initialize(self, index):
        """Test index initialization."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        result = await index.initialize()
        assert result is True
        assert index._initialized is True

    @pytest.mark.asyncio
    async def test_index_document(self, index):
        """Test indexing a single document."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        content = """
        def getUserName(self):
            '''Get the user's name from the database.'''
            return self.user_name
        """

        result = await index.index_document(
            path_id="doc1",
            path="/src/user.py",
            content=content,
        )

        assert result is True

    @pytest.mark.asyncio
    async def test_search(self, index):
        """Test searching indexed documents."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        # Index some documents
        docs = [
            ("doc1", "/src/auth.py", "def authenticate_user(): pass"),
            ("doc2", "/src/user.py", "def getUserName(): return self.name"),
            ("doc3", "/src/db.py", "def connect_database(): pass"),
        ]

        for path_id, path, content in docs:
            await index.index_document(path_id, path, content)

        # Force merge to make documents searchable
        await index.rebuild_index()

        # Search for user-related content
        results = await index.search("user name", limit=10)

        assert len(results) > 0
        # user.py should rank higher
        paths = [r.path for r in results]
        assert "/src/user.py" in paths

    @pytest.mark.asyncio
    async def test_search_with_path_filter(self, index):
        """Test searching with path filter."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        # Index documents in different directories
        docs = [
            ("doc1", "/src/auth.py", "def authenticate(): pass"),
            ("doc2", "/tests/test_auth.py", "def test_authenticate(): pass"),
        ]

        for path_id, path, content in docs:
            await index.index_document(path_id, path, content)

        await index.rebuild_index()

        # Search only in /src
        results = await index.search("authenticate", limit=10, path_filter="/src")

        paths = [r.path for r in results]
        assert "/src/auth.py" in paths
        assert "/tests/test_auth.py" not in paths

    @pytest.mark.asyncio
    async def test_bulk_index(self, index):
        """Test bulk indexing."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        docs = [
            ("doc1", "/file1.py", "content one"),
            ("doc2", "/file2.py", "content two"),
            ("doc3", "/file3.py", "content three"),
        ]

        count = await index.index_documents_bulk(docs)
        assert count == 3

        stats = await index.get_stats()
        assert stats["total_documents"] == 3

    @pytest.mark.asyncio
    async def test_delete_document(self, index):
        """Test deleting a document."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        # Index a document
        await index.index_document("doc1", "/test.py", "test content")
        await index.rebuild_index()

        stats = await index.get_stats()
        assert stats["total_documents"] == 1

        # Delete the document
        await index.delete_document("doc1")

        stats = await index.get_stats()
        assert stats["total_documents"] == 0

    @pytest.mark.asyncio
    async def test_clear_index(self, index):
        """Test clearing the index."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        # Index some documents
        await index.index_document("doc1", "/test1.py", "content one")
        await index.index_document("doc2", "/test2.py", "content two")
        await index.rebuild_index()

        # Clear the index
        await index.clear()

        stats = await index.get_stats()
        assert stats["total_documents"] == 0

    @pytest.mark.asyncio
    async def test_get_stats(self, index):
        """Test getting index statistics."""
        from nexus.search.bm25s_search import is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        await index.initialize()

        stats = await index.get_stats()

        assert "available" in stats
        assert "initialized" in stats
        assert "total_documents" in stats
        assert "method" in stats
        assert "k1" in stats
        assert "b" in stats

    @pytest.mark.asyncio
    async def test_persistence(self, temp_index_dir):
        """Test index persistence across instances."""
        from nexus.search.bm25s_search import BM25SIndex, is_bm25s_available

        if not is_bm25s_available():
            pytest.skip("bm25s not installed")

        # Create and populate first index
        index1 = BM25SIndex(index_dir=temp_index_dir)
        await index1.initialize()

        await index1.index_document("doc1", "/test.py", "hello world function")
        await index1.rebuild_index()

        # Create second index pointing to same directory
        index2 = BM25SIndex(index_dir=temp_index_dir)
        await index2.initialize()

        # Should have loaded the documents
        stats = await index2.get_stats()
        assert stats["total_documents"] == 1

        # Search should work
        results = await index2.search("hello", limit=10)
        assert len(results) > 0


class TestBM25SAvailability:
    """Tests for BM25S availability checking."""

    def test_is_bm25s_available(self):
        """Test availability check."""
        from nexus.search.bm25s_search import is_bm25s_available

        # Should return True if bm25s is installed, False otherwise
        result = is_bm25s_available()
        assert isinstance(result, bool)

    def test_get_bm25s_index_singleton(self):
        """Test global singleton pattern."""
        from nexus.search.bm25s_search import get_bm25s_index

        index1 = get_bm25s_index()
        index2 = get_bm25s_index()

        assert index1 is index2
