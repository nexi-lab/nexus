"""Tests for SearchService (Issue #1287, Decision 9A).

Tests cover:
- Initialization with various dependency configurations
- Unimplemented methods: list, glob, grep raise NotImplementedError
- Semantic search: requires initialization
- Permission checking: enforcement toggle
- Path validation: normalization
- Algorithm strategy selection: adaptive strategy enums
- RPC decorators on public methods
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from nexus.services.search_service import (
    GlobStrategy,
    SearchService,
    SearchStrategy,
)


class TestSearchServiceInit:
    """Test SearchService initialization."""

    def test_init_minimal(self, mock_metadata_store):
        """Test service initialization with minimal dependencies."""
        service = SearchService(metadata_store=mock_metadata_store)
        assert service.metadata is mock_metadata_store
        assert service._permission_enforcer is None
        assert service.router is None
        assert service._enforce_permissions is True
        assert service._semantic_search is None
        assert service._async_search is None

    def test_init_with_permission_enforcer(self, mock_metadata_store, mock_permission_enforcer):
        """Test service initialization with permission enforcer."""
        service = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=mock_permission_enforcer,
        )
        assert service._permission_enforcer is mock_permission_enforcer

    def test_init_disable_permissions(self, mock_metadata_store):
        """Test service initialization with permissions disabled."""
        service = SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )
        assert service._enforce_permissions is False

    def test_init_with_record_store(self, mock_metadata_store):
        """Test service initialization with record store."""
        mock_record_store = MagicMock()
        service = SearchService(
            metadata_store=mock_metadata_store,
            record_store=mock_record_store,
        )
        assert service._record_store is mock_record_store

    def test_init_with_default_context(self, mock_metadata_store, operation_context):
        """Test service initialization with default context."""
        service = SearchService(
            metadata_store=mock_metadata_store,
            default_context=operation_context,
        )
        assert service._default_context is operation_context


class TestSearchServiceUnimplemented:
    """Test that unimplemented methods raise NotImplementedError."""

    @pytest.fixture
    def service(self, mock_metadata_store):
        """Create service instance."""
        return SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )

    def test_list_returns_list(self, service, mock_metadata_store):
        """Test that list() returns a list (no longer raises NotImplementedError)."""
        mock_metadata_store.list_files.return_value = []
        result = service.list(path="/")
        assert isinstance(result, list)

    def test_glob_returns_list(self, service, mock_metadata_store):
        """Test that glob() returns a list (no longer raises NotImplementedError)."""
        mock_metadata_store.list_files.return_value = []
        result = service.glob(pattern="*.py")
        assert isinstance(result, list)

    def test_grep_returns_list(self, service, mock_metadata_store):
        """Test that grep() returns a list (no longer raises NotImplementedError)."""
        mock_metadata_store.list_files.return_value = []
        result = service.grep(pattern="TODO")
        assert isinstance(result, list)


class TestSearchServiceSemanticSearch:
    """Test SearchService semantic search methods."""

    @pytest.fixture
    def service(self, mock_metadata_store):
        """Create service instance without semantic search."""
        return SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )

    @pytest.mark.asyncio
    async def test_semantic_search_not_initialized(self, service):
        """Test that semantic_search raises ValueError when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await service.semantic_search(query="test query")

    @pytest.mark.asyncio
    async def test_semantic_search_index_not_initialized(self, service):
        """Test that semantic_search_index raises ValueError when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await service.semantic_search_index()

    @pytest.mark.asyncio
    async def test_semantic_search_stats_not_initialized(self, service):
        """Test that semantic_search_stats raises ValueError when not initialized."""
        with pytest.raises(ValueError, match="not initialized"):
            await service.semantic_search_stats()

    @pytest.mark.asyncio
    async def test_semantic_search_with_async_search(self, mock_metadata_store):
        """Test semantic search delegates to async search when initialized."""
        mock_result = MagicMock()
        mock_result.path = "/test.txt"
        mock_result.chunk_index = 0
        mock_result.chunk_text = "test content"
        mock_result.score = 0.95
        mock_result.start_offset = 0
        mock_result.end_offset = 12
        mock_result.line_start = 1
        mock_result.line_end = 1

        mock_async_search = AsyncMock()
        mock_async_search.search.return_value = [mock_result]

        service = SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )
        service._async_search = mock_async_search

        results = await service.semantic_search(query="test")
        assert len(results) == 1
        assert results[0]["path"] == "/test.txt"
        assert results[0]["score"] == 0.95
        mock_async_search.search.assert_called_once()

    @pytest.mark.asyncio
    async def test_semantic_search_stats_with_async(self, mock_metadata_store):
        """Test stats delegates to async search when initialized."""
        mock_async_search = AsyncMock()
        mock_async_search.get_stats.return_value = {
            "total_chunks": 100,
            "indexed_files": 10,
        }

        service = SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )
        service._async_search = mock_async_search

        stats = await service.semantic_search_stats()
        assert stats["total_chunks"] == 100
        mock_async_search.get_stats.assert_called_once()


class TestSearchServicePermissions:
    """Test SearchService permission checking."""

    def test_check_permission_skipped_when_disabled(self, mock_metadata_store):
        """Test permission check is skipped when enforcement disabled."""
        service = SearchService(
            metadata_store=mock_metadata_store,
            enforce_permissions=False,
        )
        # Should not raise
        service._check_read_permission("/test.txt", None)

    def test_check_permission_skipped_without_enforcer(self, mock_metadata_store):
        """Test permission check is skipped without enforcer."""
        service = SearchService(
            metadata_store=mock_metadata_store,
            permission_enforcer=None,
            enforce_permissions=True,
        )
        # Should not raise (no enforcer = skip check)
        service._check_read_permission("/test.txt", None)


class TestSearchServicePathValidation:
    """Test SearchService._validate_path() method."""

    @pytest.fixture
    def service(self, mock_metadata_store):
        """Create service instance."""
        return SearchService(metadata_store=mock_metadata_store)

    def test_validate_path_absolute(self, service):
        """Test path validation adds leading slash."""
        assert service._validate_path("test.txt") == "/test.txt"

    def test_validate_path_trailing_slash(self, service):
        """Test path validation removes trailing slash."""
        assert service._validate_path("/path/to/dir/") == "/path/to/dir"

    def test_validate_path_root(self, service):
        """Test path validation preserves root path."""
        assert service._validate_path("/") == "/"

    def test_validate_path_already_absolute(self, service):
        """Test path validation preserves already-absolute path."""
        assert service._validate_path("/docs/readme.md") == "/docs/readme.md"


class TestSearchServiceStrategies:
    """Test SearchService adaptive algorithm strategy enums."""

    def test_search_strategy_values(self):
        """Test SearchStrategy enum values are correct."""
        assert SearchStrategy.SEQUENTIAL == "sequential"
        assert SearchStrategy.CACHED_TEXT == "cached_text"
        assert SearchStrategy.RUST_BULK == "rust_bulk"
        assert SearchStrategy.PARALLEL_POOL == "parallel_pool"
        assert SearchStrategy.ZOEKT_INDEX == "zoekt_index"

    def test_glob_strategy_values(self):
        """Test GlobStrategy enum values are correct."""
        assert GlobStrategy.FNMATCH_SIMPLE == "fnmatch_simple"
        assert GlobStrategy.REGEX_COMPILED == "regex_compiled"
        assert GlobStrategy.RUST_BULK == "rust_bulk"
        assert GlobStrategy.DIRECTORY_PRUNED == "directory_pruned"


class TestSearchServiceRPCMethods:
    """Test that SearchService methods have @rpc_expose decorators."""

    @pytest.fixture
    def service(self, mock_metadata_store):
        """Create service instance."""
        return SearchService(metadata_store=mock_metadata_store)

    def test_list_is_rpc_exposed(self, service):
        """Test list has @rpc_expose."""
        assert hasattr(service.list, "_rpc_exposed")

    def test_glob_is_rpc_exposed(self, service):
        """Test glob has @rpc_expose."""
        assert hasattr(service.glob, "_rpc_exposed")

    def test_grep_is_rpc_exposed(self, service):
        """Test grep has @rpc_expose."""
        assert hasattr(service.grep, "_rpc_exposed")

    def test_semantic_search_is_rpc_exposed(self, service):
        """Test semantic_search has @rpc_expose."""
        assert hasattr(service.semantic_search, "_rpc_exposed")

    def test_semantic_search_index_is_rpc_exposed(self, service):
        """Test semantic_search_index has @rpc_expose."""
        assert hasattr(service.semantic_search_index, "_rpc_exposed")

    def test_semantic_search_stats_is_rpc_exposed(self, service):
        """Test semantic_search_stats has @rpc_expose."""
        assert hasattr(service.semantic_search_stats, "_rpc_exposed")

    def test_initialize_semantic_search_is_rpc_exposed(self, service):
        """Test initialize_semantic_search has @rpc_expose."""
        assert hasattr(service.initialize_semantic_search, "_rpc_exposed")
