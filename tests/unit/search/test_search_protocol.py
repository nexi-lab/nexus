"""Contract tests for SearchBrickProtocol (Issue #1520).

Tests verify that search implementations satisfy the SearchBrickProtocol
contract. Validates protocol conformance, method signatures, and behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.services.protocols.search import SearchBrickProtocol

# =============================================================================
# Mock implementations for protocol testing
# =============================================================================


class MockSearchBrick:
    """Complete mock implementation satisfying SearchBrickProtocol."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        path_filter: str | None = None,
        search_mode: str = "hybrid",
    ) -> list[Any]:
        if not query:
            return []
        return [{"path": f"/doc{i}.txt", "score": 0.9} for i in range(min(limit, 3))]

    async def index_document(self, path: str, content: str, *, zone_id: str | None = None) -> int:
        return 0

    async def get_stats(self) -> dict[str, Any]:
        return {"total_chunks": 100, "indexed_files": 10}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def verify_imports(self) -> dict[str, bool]:
        return {"nexus.search.fusion": True, "nexus.search.bm25s_search": False}


class IncompleteNoSearch:
    """Mock missing search() method."""

    async def index_document(self, path: str, content: str, *, zone_id: str | None = None) -> int:
        return 0

    async def get_stats(self) -> dict[str, Any]:
        return {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def verify_imports(self) -> dict[str, bool]:
        return {}


class IncompleteNoVerifyImports:
    """Mock missing verify_imports() method."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 10,
        path_filter: str | None = None,
        search_mode: str = "hybrid",
    ) -> list[Any]:
        return []

    async def index_document(self, path: str, content: str, *, zone_id: str | None = None) -> int:
        return 0

    async def get_stats(self) -> dict[str, Any]:
        return {}

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


# =============================================================================
# Category 1: Protocol Conformance (3 tests)
# =============================================================================


class TestProtocolConformance:
    """Verify runtime_checkable protocol conformance."""

    def test_complete_implementation_satisfies_protocol(self) -> None:
        mock = MockSearchBrick()
        assert isinstance(mock, SearchBrickProtocol)

    def test_missing_search_does_not_satisfy_protocol(self) -> None:
        mock = IncompleteNoSearch()
        assert not isinstance(mock, SearchBrickProtocol)

    def test_missing_verify_imports_does_not_satisfy_protocol(self) -> None:
        mock = IncompleteNoVerifyImports()
        assert not isinstance(mock, SearchBrickProtocol)


# =============================================================================
# Category 2: Method Signature Tests (5 tests)
# =============================================================================


class TestMethodSignatures:
    """Verify protocol methods accept correct args and return correct types."""

    @pytest.mark.asyncio
    async def test_search_returns_list(self) -> None:
        result = await MockSearchBrick().search("test query")
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_search_accepts_keyword_args(self) -> None:
        result = await MockSearchBrick().search(
            "test", limit=5, path_filter="/docs/", search_mode="semantic"
        )
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_index_document_returns_int(self) -> None:
        result = await MockSearchBrick().index_document("/test.txt", "content")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_get_stats_returns_dict(self) -> None:
        result = await MockSearchBrick().get_stats()
        assert isinstance(result, dict)

    def test_verify_imports_returns_dict_str_bool(self) -> None:
        result = MockSearchBrick().verify_imports()
        assert isinstance(result, dict)
        for key, val in result.items():
            assert isinstance(key, str)
            assert isinstance(val, bool)


# =============================================================================
# Category 3: Contract Behavior Tests (4 tests)
# =============================================================================


class TestContractBehavior:
    """Verify expected behavior from the protocol contract."""

    @pytest.mark.asyncio
    async def test_search_empty_query_returns_empty_list(self) -> None:
        result = await MockSearchBrick().search("")
        assert result == []

    @pytest.mark.asyncio
    async def test_search_respects_limit(self) -> None:
        result = await MockSearchBrick().search("test", limit=2)
        assert len(result) <= 2

    @pytest.mark.asyncio
    async def test_get_stats_contains_expected_keys(self) -> None:
        stats = await MockSearchBrick().get_stats()
        assert "total_chunks" in stats
        assert "indexed_files" in stats

    def test_verify_imports_values_are_all_bool(self) -> None:
        result = MockSearchBrick().verify_imports()
        assert len(result) > 0
        for key, val in result.items():
            assert isinstance(val, bool), f"Value for '{key}' is not bool: {type(val)}"


# =============================================================================
# Category 4: Integration with Real Classes (3 tests)
# =============================================================================

PROTOCOL_METHODS = [
    "search",
    "index_document",
    "get_stats",
    "initialize",
    "shutdown",
    "verify_imports",
]


class TestRealClassStructure:
    """Structural checks that real classes have all protocol methods."""

    def test_semantic_search_has_all_protocol_methods(self) -> None:
        from nexus.search.semantic import SemanticSearch

        for method in PROTOCOL_METHODS:
            assert hasattr(SemanticSearch, method), f"SemanticSearch missing {method}"
            assert callable(getattr(SemanticSearch, method))

    def test_async_semantic_search_has_all_protocol_methods(self) -> None:
        from nexus.search.async_search import AsyncSemanticSearch

        for method in PROTOCOL_METHODS:
            assert hasattr(AsyncSemanticSearch, method), f"AsyncSemanticSearch missing {method}"
            assert callable(getattr(AsyncSemanticSearch, method))

    def test_search_daemon_has_all_protocol_methods(self) -> None:
        from nexus.search.daemon import SearchDaemon

        for method in PROTOCOL_METHODS:
            assert hasattr(SearchDaemon, method), f"SearchDaemon missing {method}"
            assert callable(getattr(SearchDaemon, method))
