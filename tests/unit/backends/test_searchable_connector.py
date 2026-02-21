"""SearchableConnector protocol tests (Issue #2367, Decision 9A).

Tests a mock implementation that satisfies the SearchableConnector protocol:
search(), index(), remove_from_index(). Covers edge cases like empty queries,
no results, filters, and limit=0.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Mock SearchableConnector Implementation
# ---------------------------------------------------------------------------


class MockSearchableBackend:
    """In-memory searchable backend for testing."""

    def __init__(self) -> None:
        self._index: dict[str, dict[str, Any]] = {}

    def search(
        self,
        query: str,
        *,
        filters: dict[str, Any] | None = None,
        limit: int = 10,
        context: Any = None,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for key, entry in self._index.items():
            if query.lower() in entry["content"].lower():
                if filters and not all(
                    entry.get("metadata", {}).get(k) == v for k, v in filters.items()
                ):
                    continue
                results.append({"key": key, **entry})
        return results[:limit]

    def index(
        self,
        key: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        context: Any = None,
    ) -> None:
        self._index[key] = {"content": content, "metadata": metadata or {}}

    def remove_from_index(
        self,
        key: str,
        context: Any = None,
    ) -> None:
        self._index.pop(key, None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSearchableConnectorProtocol:
    """Verify MockSearchableBackend satisfies SearchableConnector."""

    def test_isinstance_check(self) -> None:
        from nexus.core.protocols.connector import SearchableConnector

        backend = MockSearchableBackend()
        assert isinstance(backend, SearchableConnector)

    def test_index_and_search(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "Hello world")
        backend.index("doc2", "Goodbye world")

        results = backend.search("Hello")
        assert len(results) == 1
        assert results[0]["key"] == "doc1"

    def test_search_empty_query(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "some content")

        # Empty string matches everything (contained in all strings)
        results = backend.search("")
        assert len(results) == 1

    def test_search_no_results(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "Hello world")

        results = backend.search("nonexistent")
        assert results == []

    def test_search_with_filters(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "Hello world", metadata={"type": "greeting"})
        backend.index("doc2", "Hello universe", metadata={"type": "farewell"})

        results = backend.search("Hello", filters={"type": "greeting"})
        assert len(results) == 1
        assert results[0]["key"] == "doc1"

    def test_search_with_limit(self) -> None:
        backend = MockSearchableBackend()
        for i in range(20):
            backend.index(f"doc{i}", f"content {i}")

        results = backend.search("content", limit=5)
        assert len(results) == 5

    def test_search_limit_zero(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "Hello")

        results = backend.search("Hello", limit=0)
        assert results == []

    def test_remove_from_index(self) -> None:
        backend = MockSearchableBackend()
        backend.index("doc1", "Hello world")

        results = backend.search("Hello")
        assert len(results) == 1

        backend.remove_from_index("doc1")

        results = backend.search("Hello")
        assert results == []

    def test_remove_nonexistent_key(self) -> None:
        """remove_from_index on missing key should not raise."""
        backend = MockSearchableBackend()
        backend.remove_from_index("nonexistent")  # No exception
