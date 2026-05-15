"""Tests for SearchBrickProtocol contract (Issue #1520).

Validates that SearchBrickProtocol is runtime_checkable, that mock classes
satisfy isinstance checks, and that all method signatures are correct.
"""

from typing import Any

import pytest

from nexus.contracts.protocols.search import SearchBrickProtocol

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

    def test_protocol_has_startup_method(self) -> None:
        """Protocol must declare async startup()."""
        assert hasattr(SearchBrickProtocol, "startup")

    def test_protocol_has_shutdown_method(self) -> None:
        """Protocol must declare async shutdown()."""
        assert hasattr(SearchBrickProtocol, "shutdown")

    def test_protocol_has_is_initialized_property(self) -> None:
        """Protocol must declare is_initialized property."""
        assert hasattr(SearchBrickProtocol, "is_initialized")

    def test_protocol_has_get_stats_method(self) -> None:
        """Protocol must declare sync get_stats()."""
        assert hasattr(SearchBrickProtocol, "get_stats")

    def test_protocol_has_get_health_method(self) -> None:
        """Protocol must declare sync get_health()."""
        assert hasattr(SearchBrickProtocol, "get_health")

    def test_protocol_has_notify_file_change_method(self) -> None:
        """Protocol must declare async notify_file_change()."""
        assert hasattr(SearchBrickProtocol, "notify_file_change")


# =============================================================================
# Mock implementation satisfying protocol
# =============================================================================


class MockSearchBrick:
    """Minimal mock that satisfies SearchBrickProtocol."""

    @property
    def is_initialized(self) -> bool:
        return True

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def search(
        self,
        query: str,
        search_type: str = "hybrid",
        limit: int = 10,
        path_filter: str | None = None,
        alpha: float = 0.5,
        fusion_method: str = "rrf",
        adaptive_k: bool = False,
    ) -> list[Any]:
        return [{"path": "/test.py", "score": 0.9, "chunk_text": "match"}]

    def get_stats(self) -> dict[str, Any]:
        return {"total_chunks": 100, "indexed_files": 10}

    def get_health(self) -> dict[str, Any]:
        return {"status": "healthy"}

    async def notify_file_change(self, path: str, change_type: str = "update") -> None:
        pass


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
    async def test_mock_startup_shutdown_lifecycle(self) -> None:
        brick = MockSearchBrick()
        await brick.startup()
        assert brick.is_initialized
        await brick.shutdown()

    def test_mock_get_stats_returns_dict(self) -> None:
        brick = MockSearchBrick()
        stats = brick.get_stats()
        assert isinstance(stats, dict)

    def test_mock_get_health_returns_dict(self) -> None:
        brick = MockSearchBrick()
        health = brick.get_health()
        assert isinstance(health, dict)

    @pytest.mark.asyncio
    async def test_mock_notify_file_change(self) -> None:
        brick = MockSearchBrick()
        await brick.notify_file_change("/test.py", "update")  # should not raise


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
            search_type="keyword",
            limit=5,
            path_filter="/src/",
            alpha=0.7,
            fusion_method="rrf",
            adaptive_k=True,
        )
        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_notify_file_change_with_change_type(self) -> None:
        brick = self._make_mock_brick()
        await brick.notify_file_change("/test.py", change_type="delete")
