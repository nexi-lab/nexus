"""Tests for MemoryQueryExecutor (Issue #1428).

Covers:
1. Happy path: search returns results, verify data shape
2. Empty results: search returns [], verify ok with empty list
3. Template variables in query
4. Search exception → SourceResult.error()
5. search_mode exposed in metadata (hybrid vs keyword fallback)
6. elapsed_ms positive
7. source_type and source_name correct
"""

from __future__ import annotations

from typing import Any

import pytest

from nexus.services.context_manifest.executors.memory_query import MemoryQueryExecutor
from nexus.services.context_manifest.models import MemoryQuerySource


# ---------------------------------------------------------------------------
# Stub implementations
# ---------------------------------------------------------------------------


class StubMemorySearch:
    """Stub MemorySearch that returns configurable results."""

    def __init__(
        self,
        results: list[dict[str, Any]] | None = None,
        search_mode: str = "hybrid",
        error: Exception | None = None,
    ) -> None:
        self._results = results if results is not None else []
        self._search_mode = search_mode
        self._error = error
        self.last_query: str | None = None
        self.last_top_k: int | None = None
        self.last_search_mode: str | None = None

    def search(
        self, query: str, top_k: int, search_mode: str
    ) -> tuple[list[dict[str, Any]], str]:
        self.last_query = query
        self.last_top_k = top_k
        self.last_search_mode = search_mode
        if self._error is not None:
            raise self._error
        return self._results, self._search_mode


def _make_source(query: str = "test query", top_k: int = 10, **kw: Any) -> MemoryQuerySource:
    return MemoryQuerySource(query=query, top_k=top_k, **kw)


def _sample_results() -> list[dict[str, Any]]:
    return [
        {"content": "Python is great", "score": 0.95, "memory_type": "fact"},
        {"content": "Use pytest for testing", "score": 0.87, "memory_type": "preference"},
        {"content": "Database schema v2", "score": 0.72, "memory_type": "knowledge"},
    ]


# ---------------------------------------------------------------------------
# Test 1: Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_search_returns_results(self) -> None:
        """Search returns results, verify data shape."""
        results = _sample_results()
        stub = StubMemorySearch(results=results, search_mode="hybrid")
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source("find auth code"), {})

        assert result.status == "ok"
        assert result.data["total"] == 3
        assert result.data["results"] == results
        assert result.data["query"] == "find auth code"
        assert result.data["search_mode"] == "hybrid"
        assert result.data["top_k"] == 10

    @pytest.mark.asyncio
    async def test_search_passes_top_k(self) -> None:
        """top_k is forwarded to the search backend."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        await executor.execute(_make_source("q", top_k=5), {})

        assert stub.last_top_k == 5


# ---------------------------------------------------------------------------
# Test 2: Empty results
# ---------------------------------------------------------------------------


class TestEmptyResults:
    @pytest.mark.asyncio
    async def test_empty_results_ok(self) -> None:
        """Search returns [] → ok with empty list."""
        stub = StubMemorySearch(results=[], search_mode="hybrid")
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source("obscure query"), {})

        assert result.status == "ok"
        assert result.data["results"] == []
        assert result.data["total"] == 0


# ---------------------------------------------------------------------------
# Test 3: Template variables in query
# ---------------------------------------------------------------------------


class TestTemplateVariables:
    @pytest.mark.asyncio
    async def test_template_resolved_in_query(self) -> None:
        """{{task.description}} in query is resolved before searching."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        variables = {"task.description": "implement auth module"}
        result = await executor.execute(
            _make_source("relevant to {{task.description}}"), variables
        )

        assert result.status == "ok"
        assert stub.last_query == "relevant to implement auth module"
        assert result.data["query"] == "relevant to implement auth module"

    @pytest.mark.asyncio
    async def test_template_failure_returns_error(self) -> None:
        """Missing template variable → error result."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(
            _make_source("query about {{task.description}}"), {}
        )

        assert result.status == "error"
        assert "template" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Test 4: Search exception → error
# ---------------------------------------------------------------------------


class TestSearchException:
    @pytest.mark.asyncio
    async def test_search_exception_returns_error(self) -> None:
        """Exception during search → SourceResult.error()."""
        stub = StubMemorySearch(error=RuntimeError("DB connection lost"))
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source("test"), {})

        assert result.status == "error"
        assert "DB connection lost" in result.error_message


# ---------------------------------------------------------------------------
# Test 5: search_mode exposed in metadata
# ---------------------------------------------------------------------------


class TestSearchMode:
    @pytest.mark.asyncio
    async def test_hybrid_mode_reported(self) -> None:
        """Hybrid search mode is reported in result metadata."""
        stub = StubMemorySearch(results=_sample_results(), search_mode="hybrid")
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source(), {})

        assert result.data["search_mode"] == "hybrid"

    @pytest.mark.asyncio
    async def test_keyword_fallback_reported(self) -> None:
        """Keyword fallback mode is reported correctly."""
        stub = StubMemorySearch(results=_sample_results(), search_mode="keyword")
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source(), {})

        assert result.data["search_mode"] == "keyword"


# ---------------------------------------------------------------------------
# Test 6: elapsed_ms positive
# ---------------------------------------------------------------------------


class TestElapsedMs:
    @pytest.mark.asyncio
    async def test_elapsed_ms_positive(self) -> None:
        """elapsed_ms is always positive."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source(), {})

        assert result.elapsed_ms > 0


# ---------------------------------------------------------------------------
# Test 7: source_type and source_name correct
# ---------------------------------------------------------------------------


class TestSourceMetadata:
    @pytest.mark.asyncio
    async def test_source_type_correct(self) -> None:
        """source_type is 'memory_query'."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source("my query"), {})

        assert result.source_type == "memory_query"

    @pytest.mark.asyncio
    async def test_source_name_is_query(self) -> None:
        """source_name is the original query string (pre-template)."""
        stub = StubMemorySearch(results=[])
        executor = MemoryQueryExecutor(memory_search=stub)

        result = await executor.execute(_make_source("find all bugs"), {})

        assert result.source_name == "find all bugs"
