"""Unit tests for MemorySearchAdapter fallback detection (Issue #2130, #11A).

Covers:
- Normal hybrid search → returns original mode
- All scores 1.0 → detected as keyword fallback
- Mixed scores → returns original mode
- Empty results → returns original mode
- Semantic mode with fallback detection
"""

from __future__ import annotations

from typing import Any

from nexus.bricks.context_manifest.executors.memory_search_adapter import (
    MemorySearchAdapter,
)


class StubMemory:
    """Stub Memory that returns preconfigured results."""

    def __init__(self, results: list[dict[str, Any]]) -> None:
        self._results = results
        self.last_call: dict[str, Any] = {}

    def search(self, query: str, limit: int, search_mode: str) -> list[dict[str, Any]]:
        self.last_call = {
            "query": query,
            "limit": limit,
            "search_mode": search_mode,
        }
        return self._results


class TestHybridModeFallbackDetection:
    """Detect when hybrid search silently falls back to keyword."""

    def test_mixed_scores_returns_hybrid(self) -> None:
        memory = StubMemory(
            [
                {"content": "a", "score": 0.95},
                {"content": "b", "score": 0.72},
                {"content": "c", "score": 0.45},
            ]
        )
        adapter = MemorySearchAdapter(memory)
        results, actual_mode = adapter.search("test", top_k=3, search_mode="hybrid")

        assert actual_mode == "hybrid"
        assert len(results) == 3

    def test_all_scores_one_detected_as_keyword(self) -> None:
        memory = StubMemory(
            [
                {"content": "a", "score": 1.0},
                {"content": "b", "score": 1.0},
            ]
        )
        adapter = MemorySearchAdapter(memory)
        results, actual_mode = adapter.search("test", top_k=2, search_mode="hybrid")

        assert actual_mode == "keyword"
        assert len(results) == 2

    def test_empty_results_returns_original_mode(self) -> None:
        memory = StubMemory([])
        adapter = MemorySearchAdapter(memory)
        results, actual_mode = adapter.search("test", top_k=5, search_mode="hybrid")

        assert actual_mode == "hybrid"
        assert len(results) == 0

    def test_single_result_score_one_detected_as_keyword(self) -> None:
        memory = StubMemory([{"content": "a", "score": 1.0}])
        adapter = MemorySearchAdapter(memory)
        _, actual_mode = adapter.search("test", top_k=1, search_mode="hybrid")

        assert actual_mode == "keyword"


class TestSemanticModeFallback:
    """Same fallback detection applies to semantic mode."""

    def test_semantic_all_ones_detected_as_keyword(self) -> None:
        memory = StubMemory([{"content": "x", "score": 1.0}, {"content": "y", "score": 1.0}])
        adapter = MemorySearchAdapter(memory)
        _, actual_mode = adapter.search("test", top_k=2, search_mode="semantic")

        assert actual_mode == "keyword"

    def test_semantic_mixed_scores_returns_semantic(self) -> None:
        memory = StubMemory([{"content": "x", "score": 0.9}, {"content": "y", "score": 0.3}])
        adapter = MemorySearchAdapter(memory)
        _, actual_mode = adapter.search("test", top_k=2, search_mode="semantic")

        assert actual_mode == "semantic"


class TestKeywordModePassthrough:
    """Keyword mode should never trigger fallback detection."""

    def test_keyword_mode_all_ones_stays_keyword(self) -> None:
        memory = StubMemory([{"content": "a", "score": 1.0}, {"content": "b", "score": 1.0}])
        adapter = MemorySearchAdapter(memory)
        _, actual_mode = adapter.search("test", top_k=2, search_mode="keyword")

        assert actual_mode == "keyword"


class TestDelegation:
    """Verify adapter correctly delegates to underlying memory."""

    def test_passes_correct_params(self) -> None:
        memory = StubMemory([])
        adapter = MemorySearchAdapter(memory)
        adapter.search("find auth", top_k=7, search_mode="hybrid")

        assert memory.last_call == {
            "query": "find auth",
            "limit": 7,
            "search_mode": "hybrid",
        }
