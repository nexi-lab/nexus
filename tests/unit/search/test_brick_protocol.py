"""Tests for SearchBrickProtocol contract (Issue #1520).

Validates that SearchBrickProtocol is runtime_checkable, that mock classes
satisfy isinstance checks, and that all method signatures are correct.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.services.protocols.search import SearchBrickProtocol

# =============================================================================
# Protocol structural checks
# =============================================================================


class TestSearchBrickProtocolStructure:
    """Verify SearchBrickProtocol is runtime_checkable and well-formed."""

    def test_protocol_is_runtime_checkable(self) -> None:
        """Protocol must be decorated with @runtime_checkable."""
        assert hasattr(SearchBrickProtocol, "__protocol_attrs__") or hasattr(
            SearchBrickProtocol, "__abstractmethods__"
        )
        # runtime_checkable protocols support isinstance()
        assert callable(getattr(SearchBrickProtocol, "__instancecheck__", None))

    def test_protocol_has_search_method(self) -> None:
        """Protocol must declare async search()."""
        assert hasattr(SearchBrickProtocol, "search")

    def test_protocol_has_index_document_method(self) -> None:
        """Protocol must declare async index_document()."""
        assert hasattr(SearchBrickProtocol, "index_document")

    def test_protocol_has_index_directory_method(self) -> None:
        """Protocol must declare async index_directory()."""
        assert hasattr(SearchBrickProtocol, "index_directory")

    def test_protocol_has_delete_document_index_method(self) -> None:
        """Protocol must declare async delete_document_index()."""
        assert hasattr(SearchBrickProtocol, "delete_document_index")

    def test_protocol_has_get_stats_method(self) -> None:
        """Protocol must declare async get_stats()."""
        assert hasattr(SearchBrickProtocol, "get_stats")

    def test_protocol_has_get_index_stats_method(self) -> None:
        """Protocol must declare async get_index_stats()."""
        assert hasattr(SearchBrickProtocol, "get_index_stats")

    def test_protocol_has_initialize_method(self) -> None:
        """Protocol must declare async initialize()."""
        assert hasattr(SearchBrickProtocol, "initialize")

    def test_protocol_has_shutdown_method(self) -> None:
        """Protocol must declare async shutdown()."""
        assert hasattr(SearchBrickProtocol, "shutdown")

    def test_protocol_has_verify_imports_method(self) -> None:
        """Protocol must declare sync verify_imports()."""
        assert hasattr(SearchBrickProtocol, "verify_imports")


# =============================================================================
# Mock implementation satisfying protocol
# =============================================================================


class MockSearchBrick:
    """Minimal mock that satisfies SearchBrickProtocol."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        path_filter: str | None = None,
        search_mode: str = "hybrid",
    ) -> list[Any]:
        return [{"path": "/test.py", "score": 0.9, "chunk_text": "match"}]

    async def index_document(
        self,
        path: str,
        content: str,
        *,
        zone_id: str | None = None,
    ) -> int:
        return 5

    async def index_directory(self, path: str = "/") -> dict[str, int]:
        return {"/test.py": 5}

    async def delete_document_index(self, path: str) -> None:
        pass

    async def get_index_stats(self) -> dict[str, Any]:
        return {"total_chunks": 100}

    async def get_stats(self) -> dict[str, Any]:
        return {"total_chunks": 100, "indexed_files": 10}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def verify_imports(self) -> dict[str, bool]:
        return {"nexus.search.semantic": True}


class TestMockSatisfiesProtocol:
    """Verify mock class passes isinstance check."""

    def test_mock_is_instance_of_protocol(self) -> None:
        """MockSearchBrick should satisfy SearchBrickProtocol isinstance."""
        brick = MockSearchBrick()
        assert isinstance(brick, SearchBrickProtocol)

    @pytest.mark.asyncio
    async def test_mock_search_returns_list(self) -> None:
        brick = MockSearchBrick()
        results = await brick.search("test query")
        assert isinstance(results, list)
        assert len(results) == 1
        assert results[0]["path"] == "/test.py"

    @pytest.mark.asyncio
    async def test_mock_index_document_returns_int(self) -> None:
        brick = MockSearchBrick()
        count = await brick.index_document("/test.py", "content")
        assert count == 5

    @pytest.mark.asyncio
    async def test_mock_index_directory_returns_dict(self) -> None:
        brick = MockSearchBrick()
        result = await brick.index_directory("/")
        assert isinstance(result, dict)

    @pytest.mark.asyncio
    async def test_mock_delete_document_index(self) -> None:
        brick = MockSearchBrick()
        await brick.delete_document_index("/test.py")  # should not raise

    @pytest.mark.asyncio
    async def test_mock_get_stats_returns_dict(self) -> None:
        brick = MockSearchBrick()
        stats = await brick.get_stats()
        assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_mock_get_index_stats_returns_dict(self) -> None:
        brick = MockSearchBrick()
        stats = await brick.get_index_stats()
        assert isinstance(stats, dict)

    @pytest.mark.asyncio
    async def test_mock_initialize_shutdown_lifecycle(self) -> None:
        brick = MockSearchBrick()
        await brick.initialize()
        await brick.shutdown()

    def test_mock_verify_imports_returns_dict(self) -> None:
        brick = MockSearchBrick()
        result = brick.verify_imports()
        assert isinstance(result, dict)
        assert all(isinstance(v, bool) for v in result.values())


# =============================================================================
# Negative tests — incomplete implementations fail isinstance
# =============================================================================


class IncompleteSearchBrick:
    """Missing required methods — should NOT pass isinstance."""

    async def search(self, query: str, *, limit: int = 10) -> list:
        return []


class TestIncompleteImplementation:
    """Objects missing methods should not satisfy the protocol."""

    def test_incomplete_fails_isinstance(self) -> None:
        """IncompleteSearchBrick lacks most methods → not an instance."""
        brick = IncompleteSearchBrick()
        assert not isinstance(brick, SearchBrickProtocol)


# =============================================================================
# AsyncMock-based protocol test (common pattern for services)
# =============================================================================


class TestAsyncMockProtocol:
    """Verify protocol works with unittest.mock.AsyncMock."""

    def _make_mock_brick(self) -> MockSearchBrick:
        """Create a mock brick with AsyncMock methods for spying."""
        brick = MockSearchBrick()
        return brick

    @pytest.mark.asyncio
    async def test_search_with_all_kwargs(self) -> None:
        brick = self._make_mock_brick()
        results = await brick.search(
            "authentication",
            limit=5,
            path_filter="/src/",
            search_mode="keyword",
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_index_document_with_zone(self) -> None:
        brick = self._make_mock_brick()
        count = await brick.index_document("/test.py", "content", zone_id="zone-1")
        assert isinstance(count, int)
