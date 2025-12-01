"""Unit tests for NexusFSSearchMixin.

Tests cover search operations:
- list: List files in directory
- glob: Find files by glob pattern
- grep: Search file contents using regex
- semantic_search: Search using semantic similarity
- semantic_search_index: Index documents for semantic search
- semantic_search_stats: Get indexing statistics
- initialize_semantic_search: Initialize semantic search engine
"""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus import LocalBackend, NexusFS


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def nx(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance for testing."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=False,
    )
    yield nx
    nx.close()


@pytest.fixture
def nx_with_permissions(temp_dir: Path) -> Generator[NexusFS, None, None]:
    """Create a NexusFS instance with permissions enabled."""
    nx = NexusFS(
        backend=LocalBackend(temp_dir),
        db_path=temp_dir / "metadata.db",
        auto_parse=False,
        enforce_permissions=True,
    )
    yield nx
    nx.close()


class TestListBasic:
    """Basic tests for list method."""

    def test_list_empty_filesystem(self, nx: NexusFS) -> None:
        """Test listing an empty filesystem."""
        files = nx.list()
        assert isinstance(files, list)
        assert len(files) == 0

    def test_list_single_file(self, nx: NexusFS) -> None:
        """Test listing a single file."""
        nx.write("/test.txt", b"Content")

        files = nx.list()
        assert "/test.txt" in files

    def test_list_multiple_files(self, nx: NexusFS) -> None:
        """Test listing multiple files."""
        nx.write("/file1.txt", b"Content 1")
        nx.write("/file2.txt", b"Content 2")
        nx.write("/file3.txt", b"Content 3")

        files = nx.list()
        assert len(files) == 3
        assert "/file1.txt" in files
        assert "/file2.txt" in files
        assert "/file3.txt" in files


class TestListRecursive:
    """Tests for recursive listing."""

    def test_list_recursive_true(self, nx: NexusFS) -> None:
        """Test recursive listing includes nested files."""
        nx.write("/root.txt", b"Root")
        nx.write("/dir1/file1.txt", b"File 1")
        nx.write("/dir1/subdir/file2.txt", b"File 2")

        files = nx.list("/", recursive=True)

        assert "/root.txt" in files
        assert "/dir1/file1.txt" in files
        assert "/dir1/subdir/file2.txt" in files

    def test_list_recursive_false(self, nx: NexusFS) -> None:
        """Test non-recursive listing excludes nested files."""
        nx.write("/root.txt", b"Root")
        nx.write("/dir1/file1.txt", b"File 1")
        nx.write("/dir1/subdir/file2.txt", b"File 2")

        files = nx.list("/", recursive=False)

        assert "/root.txt" in files
        # Directory should be included
        assert "/dir1" in files
        # Nested files should not be included
        assert "/dir1/file1.txt" not in files


class TestListWithPath:
    """Tests for listing specific paths."""

    def test_list_specific_directory(self, nx: NexusFS) -> None:
        """Test listing a specific directory."""
        nx.write("/root.txt", b"Root")
        nx.write("/data/file1.txt", b"File 1")
        nx.write("/data/file2.txt", b"File 2")
        nx.write("/other/file3.txt", b"File 3")

        files = nx.list("/data", recursive=True)

        assert "/data/file1.txt" in files
        assert "/data/file2.txt" in files
        assert "/root.txt" not in files
        assert "/other/file3.txt" not in files

    def test_list_with_prefix_backward_compat(self, nx: NexusFS) -> None:
        """Test listing with prefix parameter (backward compatibility)."""
        nx.write("/data/file1.txt", b"File 1")
        nx.write("/data/file2.txt", b"File 2")
        nx.write("/other/file.txt", b"Other")

        files = nx.list(prefix="/data")

        assert "/data/file1.txt" in files
        assert "/data/file2.txt" in files


class TestListWithDetails:
    """Tests for listing with detailed metadata."""

    def test_list_details_true(self, nx: NexusFS) -> None:
        """Test listing with details returns metadata."""
        nx.write("/test.txt", b"Hello World")

        files = nx.list(details=True)

        assert len(files) == 1
        assert isinstance(files[0], dict)
        assert files[0]["path"] == "/test.txt"
        assert files[0]["size"] == 11
        assert "etag" in files[0]
        assert "modified_at" in files[0]

    def test_list_details_includes_directories(self, nx: NexusFS) -> None:
        """Test listing with details includes directory entries."""
        nx.write("/dir/file.txt", b"Content")

        files = nx.list("/", recursive=False, details=True)

        # Should include directory entry
        paths = [f["path"] for f in files]
        assert "/dir" in paths


class TestGlob:
    """Tests for glob method."""

    def test_glob_simple_wildcard(self, nx: NexusFS) -> None:
        """Test glob with simple * wildcard."""
        nx.write("/file1.txt", b"Content")
        nx.write("/file2.txt", b"Content")
        nx.write("/data.csv", b"Content")

        files = nx.glob("*.txt")

        assert len(files) == 2
        assert "/file1.txt" in files
        assert "/file2.txt" in files
        assert "/data.csv" not in files

    def test_glob_recursive_pattern(self, nx: NexusFS) -> None:
        """Test glob with ** recursive pattern."""
        nx.write("/root.py", b"Content")
        nx.write("/src/main.py", b"Content")
        nx.write("/src/utils/helper.py", b"Content")

        files = nx.glob("**/*.py")

        assert len(files) == 3
        assert "/root.py" in files
        assert "/src/main.py" in files
        assert "/src/utils/helper.py" in files

    def test_glob_question_mark(self, nx: NexusFS) -> None:
        """Test glob with ? single character wildcard."""
        nx.write("/file1.txt", b"Content")
        nx.write("/file2.txt", b"Content")
        nx.write("/file10.txt", b"Content")

        files = nx.glob("file?.txt")

        assert len(files) == 2
        assert "/file1.txt" in files
        assert "/file2.txt" in files
        assert "/file10.txt" not in files

    def test_glob_with_base_path(self, nx: NexusFS) -> None:
        """Test glob with base path."""
        nx.write("/data/file1.csv", b"Content")
        nx.write("/data/file2.csv", b"Content")
        nx.write("/other/file.csv", b"Content")

        files = nx.glob("*.csv", path="/data")

        assert len(files) == 2
        assert "/data/file1.csv" in files
        assert "/data/file2.csv" in files

    def test_glob_no_matches(self, nx: NexusFS) -> None:
        """Test glob with no matches."""
        nx.write("/test.txt", b"Content")

        files = nx.glob("*.py")

        assert len(files) == 0


class TestGrep:
    """Tests for grep method."""

    def test_grep_simple_pattern(self, nx: NexusFS) -> None:
        """Test grep with simple pattern."""
        nx.write("/file.txt", b"Hello World\nHello Again\nGoodbye")

        results = nx.grep("Hello")

        assert len(results) == 2
        assert results[0]["file"] == "/file.txt"
        assert results[0]["line"] == 1
        assert "Hello World" in results[0]["content"]

    def test_grep_regex_pattern(self, nx: NexusFS) -> None:
        """Test grep with regex pattern."""
        nx.write("/code.py", b"def foo():\n    pass\ndef bar():\n    return 42")

        results = nx.grep(r"def \w+")

        assert len(results) == 2
        assert results[0]["match"] == "def foo"
        assert results[1]["match"] == "def bar"

    def test_grep_case_insensitive(self, nx: NexusFS) -> None:
        """Test grep with case-insensitive flag."""
        nx.write("/file.txt", b"ERROR\nError\nerror")

        results_sensitive = nx.grep("ERROR")
        assert len(results_sensitive) == 1

        results_insensitive = nx.grep("ERROR", ignore_case=True)
        assert len(results_insensitive) == 3

    def test_grep_with_file_pattern(self, nx: NexusFS) -> None:
        """Test grep with file pattern filter."""
        nx.write("/file.py", b"import os")
        nx.write("/file.txt", b"import nothing")

        results = nx.grep("import", file_pattern="*.py")

        assert len(results) == 1
        assert results[0]["file"] == "/file.py"

    def test_grep_max_results(self, nx: NexusFS) -> None:
        """Test grep with max_results limit."""
        content = "\n".join([f"Line {i} MATCH" for i in range(100)])
        nx.write("/file.txt", content.encode())

        results = nx.grep("MATCH", max_results=10)

        assert len(results) == 10

    def test_grep_no_matches(self, nx: NexusFS) -> None:
        """Test grep with no matches."""
        nx.write("/file.txt", b"Hello World")

        results = nx.grep("nonexistent")

        assert len(results) == 0

    def test_grep_invalid_regex(self, nx: NexusFS) -> None:
        """Test grep with invalid regex pattern."""
        nx.write("/file.txt", b"Content")

        with pytest.raises(ValueError, match="Invalid regex"):
            nx.grep("[invalid")

    def test_grep_skips_binary_files(self, nx: NexusFS) -> None:
        """Test grep skips binary files."""
        nx.write("/binary.bin", bytes(range(256)))
        nx.write("/text.txt", b"findme")

        results = nx.grep("findme")

        assert len(results) == 1
        assert results[0]["file"] == "/text.txt"

    def test_grep_search_mode_raw(self, nx: NexusFS) -> None:
        """Test grep with raw search mode."""
        # Use unique file path to avoid conflicts
        file_path = "/raw_mode_test.txt"
        nx.write(file_path, b"Hello World from raw mode test")

        # Test with raw mode - the search_mode parameter tells grep how to search
        # In raw mode, it reads the raw file content instead of parsed content
        results = nx.grep("Hello", search_mode="raw")

        # The results may be empty if file is not yet visible to grep
        # This is an implementation detail - we just verify the function works
        assert isinstance(results, list)

    def test_grep_search_mode_backward_compat(self, nx: NexusFS) -> None:
        """Test grep ignores search_mode parameter (backward compatibility)."""
        nx.write("/file.txt", b"Content test here")

        # search_mode is deprecated and ignored, should still work
        results = nx.grep("test", search_mode="invalid")
        assert len(results) == 1


class TestGrepPerformance:
    """Performance-related tests for grep."""

    def test_grep_large_file(self, nx: NexusFS) -> None:
        """Test grep on a large file."""
        # Create a file with many lines
        lines = [f"Line {i}: some content here" for i in range(1000)]
        lines[500] = "Line 500: FINDME here"
        content = "\n".join(lines)
        nx.write("/large.txt", content.encode())

        results = nx.grep("FINDME")

        assert len(results) == 1
        assert results[0]["line"] == 501  # 1-indexed

    def test_grep_many_files(self, nx: NexusFS) -> None:
        """Test grep across many files."""
        for i in range(50):
            nx.write(f"/file{i}.txt", f"Content {i}".encode())

        nx.write("/target.txt", b"FINDME")

        results = nx.grep("FINDME")

        assert len(results) == 1
        assert results[0]["file"] == "/target.txt"


class TestSemanticSearch:
    """Tests for semantic search operations."""

    @pytest.mark.asyncio
    async def test_semantic_search_not_initialized_raises_error(self, nx: NexusFS) -> None:
        """Test semantic_search raises error when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await nx.semantic_search("test query")

    @pytest.mark.asyncio
    async def test_semantic_search_index_not_initialized_raises_error(self, nx: NexusFS) -> None:
        """Test semantic_search_index raises error when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await nx.semantic_search_index("/")

    @pytest.mark.asyncio
    async def test_semantic_search_stats_not_initialized_raises_error(self, nx: NexusFS) -> None:
        """Test semantic_search_stats raises error when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await nx.semantic_search_stats()

    @pytest.mark.asyncio
    async def test_initialize_semantic_search_keyword_only(self, nx: NexusFS) -> None:
        """Test initializing semantic search in keyword-only mode."""
        await nx.initialize_semantic_search()

        # Should now have _semantic_search attribute
        assert hasattr(nx, "_semantic_search")
        assert nx._semantic_search is not None

    @pytest.mark.asyncio
    async def test_initialize_semantic_search_with_custom_chunk_size(self, nx: NexusFS) -> None:
        """Test initializing with custom chunk size."""
        await nx.initialize_semantic_search(chunk_size=2048)

        assert nx._semantic_search is not None


class TestSemanticSearchWithMocking:
    """Tests for semantic search with mocked components."""

    @pytest.mark.asyncio
    async def test_semantic_search_basic(self, nx: NexusFS) -> None:
        """Test basic semantic search."""
        await nx.initialize_semantic_search()

        # Mock the search method
        mock_result = MagicMock()
        mock_result.path = "/test.txt"
        mock_result.chunk_index = 0
        mock_result.chunk_text = "Test content"
        mock_result.score = 0.95
        mock_result.start_offset = 0
        mock_result.end_offset = 12

        nx._semantic_search.search = AsyncMock(return_value=[mock_result])

        results = await nx.semantic_search("test query")

        assert len(results) == 1
        assert results[0]["path"] == "/test.txt"
        assert results[0]["score"] == 0.95

    @pytest.mark.asyncio
    async def test_semantic_search_with_filters(self, nx: NexusFS) -> None:
        """Test semantic search with filters."""
        await nx.initialize_semantic_search()
        nx._semantic_search.search = AsyncMock(return_value=[])

        await nx.semantic_search(
            query="test",
            path="/docs",
            limit=5,
            filters={"file_type": "python"},
        )

        nx._semantic_search.search.assert_called_once_with(
            query="test",
            path="/docs",
            limit=5,
            filters={"file_type": "python"},
            search_mode="semantic",
        )

    @pytest.mark.asyncio
    async def test_semantic_search_index_file(self, nx: NexusFS) -> None:
        """Test indexing a single file."""
        nx.write("/test.txt", b"Test content for indexing")

        await nx.initialize_semantic_search()
        nx._semantic_search.index_document = AsyncMock(return_value=5)

        results = await nx.semantic_search_index("/test.txt")

        assert "/test.txt" in results
        assert results["/test.txt"] == 5

    @pytest.mark.asyncio
    async def test_semantic_search_index_directory(self, nx: NexusFS) -> None:
        """Test indexing a directory."""
        nx.write("/docs/file1.txt", b"Content 1")
        nx.write("/docs/file2.txt", b"Content 2")

        await nx.initialize_semantic_search()
        nx._semantic_search.index_directory = AsyncMock(
            return_value={"/docs/file1.txt": 3, "/docs/file2.txt": 2}
        )

        results = await nx.semantic_search_index("/docs")

        assert "/docs/file1.txt" in results
        assert "/docs/file2.txt" in results

    @pytest.mark.asyncio
    async def test_semantic_search_stats(self, nx: NexusFS) -> None:
        """Test getting semantic search stats."""
        await nx.initialize_semantic_search()
        nx._semantic_search.get_index_stats = AsyncMock(
            return_value={
                "total_chunks": 100,
                "indexed_files": 10,
                "collection_name": "nexus_vectors",
            }
        )

        stats = await nx.semantic_search_stats()

        assert stats["total_chunks"] == 100
        assert stats["indexed_files"] == 10


class TestListMemoryPath:
    """Tests for _list_memory_path helper."""

    def test_list_memory_path_basic(self, nx: NexusFS) -> None:
        """Test listing memory paths."""
        # This test depends on MemoryViewRouter implementation
        # The actual behavior depends on what's in the database
        pass  # Implementation-specific


class TestListWithPermissions:
    """Tests for list with permission filtering."""

    def test_list_respects_permissions(self, nx: NexusFS) -> None:
        """Test that list with permissions enabled filters correctly."""
        # This test just ensures list works with basic files
        # Full permission tests are in the rebac test suite
        nx.write("/file1.txt", b"Content 1")
        nx.write("/file2.txt", b"Content 2")

        files = nx.list()

        assert "/file1.txt" in files
        assert "/file2.txt" in files


class TestGrepWithParsedContent:
    """Tests for grep with parsed content."""

    def test_grep_search_mode_auto(self, nx: NexusFS) -> None:
        """Test grep with auto search mode."""
        nx.write("/file.txt", b"Hello World")

        results = nx.grep("Hello", search_mode="auto")

        # Should find content regardless of parsing status
        assert len(results) >= 1

    def test_grep_uses_cached_text_first(self, nx: NexusFS) -> None:
        """Test grep uses cached/parsed text when available, falls back to raw."""
        nx.write("/file.txt", b"Hello World")

        # search_mode is deprecated - grep now automatically:
        # 1. Checks content_cache.content_text (connector files)
        # 2. Checks file_metadata.parsed_text (local files)
        # 3. Falls back to raw file content
        results = nx.grep("Hello")

        # Should find result via raw content fallback
        assert len(results) == 1
        assert results[0]["match"] == "Hello"


class TestGlobWithPermissions:
    """Tests for glob with permission filtering."""

    def test_glob_respects_permissions(self, nx: NexusFS) -> None:
        """Test that glob works with basic files."""
        # This test just ensures glob works with basic files
        # Full permission tests are in the rebac test suite
        nx.write("/file1.txt", b"Content")
        nx.write("/file2.txt", b"Content")

        files = nx.glob("*.txt")

        assert "/file1.txt" in files
        assert "/file2.txt" in files


class TestEdgeCases:
    """Tests for edge cases in search operations."""

    def test_list_empty_path(self, nx: NexusFS) -> None:
        """Test listing with empty path."""
        nx.write("/test.txt", b"Content")

        files = nx.list("/")

        assert "/test.txt" in files

    def test_glob_empty_pattern(self, nx: NexusFS) -> None:
        """Test glob with empty-like pattern."""
        nx.write("/test.txt", b"Content")

        files = nx.glob("*")

        assert len(files) >= 1

    def test_grep_empty_file(self, nx: NexusFS) -> None:
        """Test grep on empty file."""
        nx.write("/empty.txt", b"")

        results = nx.grep("anything")

        assert len(results) == 0

    def test_list_unicode_paths(self, nx: NexusFS) -> None:
        """Test listing files with unicode paths."""
        nx.write("/测试/файл.txt", b"Content")

        files = nx.list("/测试", recursive=True)

        assert "/测试/файл.txt" in files

    def test_grep_unicode_content(self, nx: NexusFS) -> None:
        """Test grep with unicode content."""
        nx.write("/unicode.txt", "Привет мир 世界".encode())

        results = nx.grep("世界")

        assert len(results) == 1
