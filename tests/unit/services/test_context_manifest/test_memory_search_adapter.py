"""Tests for MemorySearchAdapter (Issue #1428).

Integration test with mock Memory:
- Adapter delegates to Memory.search() correctly
- Returns actual search_mode used
- Detects keyword fallback
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from nexus.services.context_manifest.executors.memory_search_adapter import (
    MemorySearchAdapter,
)


class TestMemorySearchAdapter:
    def test_delegates_to_memory_search(self) -> None:
        """Adapter calls Memory.search() with correct parameters."""
        mock_memory = MagicMock()
        mock_memory.search.return_value = [
            {"content": "result1", "score": 0.9},
            {"content": "result2", "score": 0.7},
        ]
        adapter = MemorySearchAdapter(memory=mock_memory)

        results, mode = adapter.search("test query", top_k=5, search_mode="hybrid")

        mock_memory.search.assert_called_once_with(
            query="test query", limit=5, search_mode="hybrid"
        )
        assert len(results) == 2
        assert results[0]["content"] == "result1"

    def test_returns_hybrid_mode_for_varied_scores(self) -> None:
        """When scores vary, search_mode remains 'hybrid'."""
        mock_memory = MagicMock()
        mock_memory.search.return_value = [
            {"content": "r1", "score": 0.95},
            {"content": "r2", "score": 0.72},
        ]
        adapter = MemorySearchAdapter(memory=mock_memory)

        _, mode = adapter.search("q", top_k=10, search_mode="hybrid")

        assert mode == "hybrid"

    def test_detects_keyword_fallback(self) -> None:
        """When all scores are 1.0, detects keyword fallback."""
        mock_memory = MagicMock()
        mock_memory.search.return_value = [
            {"content": "r1", "score": 1.0},
            {"content": "r2", "score": 1.0},
        ]
        adapter = MemorySearchAdapter(memory=mock_memory)

        _, mode = adapter.search("q", top_k=10, search_mode="hybrid")

        assert mode == "keyword"

    def test_keyword_mode_stays_keyword(self) -> None:
        """When search_mode is 'keyword', it stays 'keyword'."""
        mock_memory = MagicMock()
        mock_memory.search.return_value = [
            {"content": "r1", "score": 1.0},
        ]
        adapter = MemorySearchAdapter(memory=mock_memory)

        _, mode = adapter.search("q", top_k=5, search_mode="keyword")

        assert mode == "keyword"

    def test_empty_results_no_fallback_detection(self) -> None:
        """Empty results don't trigger fallback detection."""
        mock_memory = MagicMock()
        mock_memory.search.return_value = []
        adapter = MemorySearchAdapter(memory=mock_memory)

        results, mode = adapter.search("q", top_k=10, search_mode="hybrid")

        assert results == []
        assert mode == "hybrid"

    def test_search_exception_propagates(self) -> None:
        """Exceptions from Memory.search() propagate to caller."""
        mock_memory = MagicMock()
        mock_memory.search.side_effect = RuntimeError("DB down")
        adapter = MemorySearchAdapter(memory=mock_memory)

        with pytest.raises(RuntimeError, match="DB down"):
            adapter.search("q", top_k=10, search_mode="hybrid")
