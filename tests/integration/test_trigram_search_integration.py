"""Integration tests for trigram search strategy in SearchService (Issue #954).

Tests the SearchService.grep() method with the trigram strategy,
including strategy selection, fallback behavior, and index management.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from nexus.core import trigram_fast
from nexus.search.strategies import (
    GREP_TRIGRAM_THRESHOLD,
    SearchStrategy,
)


@pytest.fixture
def corpus_dir():
    """Path to test corpus."""
    return os.path.join(
        os.path.dirname(__file__), "..", "fixtures", "trigram_corpus"
    )


@pytest.fixture
def corpus_files(corpus_dir):
    """List of files in test corpus."""
    return sorted(
        os.path.join(corpus_dir, name)
        for name in os.listdir(corpus_dir)
        if os.path.isfile(os.path.join(corpus_dir, name))
    )


@pytest.fixture
def built_index(tmp_path, corpus_files):
    """Build a trigram index and return its path."""
    idx_path = str(tmp_path / "test.trgm")
    trigram_fast.build_index(corpus_files, idx_path)
    return idx_path


class TestStrategySelection:
    """Test that _select_grep_strategy picks TRIGRAM_INDEX correctly."""

    def test_select_trigram_when_available(self, built_index, tmp_path):
        """Should select TRIGRAM_INDEX when file count exceeds threshold."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        zone_id = "test_zone"
        base_dir = str(tmp_path)

        # Build properly-named index
        corpus_dir = os.path.join(
            os.path.dirname(__file__), "..", "fixtures", "trigram_corpus"
        )
        files = sorted(
            os.path.join(corpus_dir, name)
            for name in os.listdir(corpus_dir)
            if os.path.isfile(os.path.join(corpus_dir, name))
        )
        idx_path = trigram_fast.get_index_path(zone_id, base_dir)
        os.makedirs(os.path.dirname(idx_path), exist_ok=True)
        trigram_fast.build_index(files, idx_path)

        with patch.object(trigram_fast, "get_index_path", return_value=idx_path):
            strategy = service._select_grep_strategy(
                file_count=GREP_TRIGRAM_THRESHOLD + 100,
                cached_text_ratio=0.0,
                zone_id=zone_id,
            )
        assert strategy == SearchStrategy.TRIGRAM_INDEX

    def test_skip_trigram_when_no_index(self):
        """Should not select TRIGRAM_INDEX when index doesn't exist."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        strategy = service._select_grep_strategy(
            file_count=GREP_TRIGRAM_THRESHOLD + 100,
            cached_text_ratio=0.0,
            zone_id="nonexistent_zone",
        )
        assert strategy != SearchStrategy.TRIGRAM_INDEX

    def test_skip_trigram_when_below_threshold(self, built_index):
        """Should not select TRIGRAM_INDEX for small file counts."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        strategy = service._select_grep_strategy(
            file_count=10,
            cached_text_ratio=0.0,
            zone_id="test_zone",
        )
        assert strategy != SearchStrategy.TRIGRAM_INDEX

    def test_cached_text_preferred_over_trigram(self, built_index):
        """CACHED_TEXT strategy should take priority over TRIGRAM_INDEX."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        strategy = service._select_grep_strategy(
            file_count=GREP_TRIGRAM_THRESHOLD + 100,
            cached_text_ratio=0.9,
            zone_id="test_zone",
        )
        assert strategy == SearchStrategy.CACHED_TEXT


class TestTrigramFallback:
    """Test fallback behavior when trigram search fails."""

    def test_try_grep_with_trigram_no_index(self):
        """Should return None when index doesn't exist."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        result = service._try_grep_with_trigram(
            pattern="hello",
            ignore_case=False,
            max_results=100,
            zone_id="nonexistent_zone",
        )
        assert result is None

    def test_try_grep_with_trigram_success(self, built_index):
        """Should return results when index exists."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        def _mock_read(path, context=None):
            """Read from real filesystem for integration test."""
            with open(path, "rb") as f:
                return f.read()

        with patch.object(
            trigram_fast, "get_index_path", return_value=built_index
        ), patch.object(service, "_read", side_effect=_mock_read):
            result = service._try_grep_with_trigram(
                pattern="hello",
                ignore_case=False,
                max_results=100,
                zone_id="test_zone",
            )
        assert result is not None
        assert len(result) > 0


class TestIndexManagement:
    """Test index build/status/invalidate lifecycle."""

    def test_get_index_status_not_built(self):
        """Status should report not_built for missing index."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        status = service.get_trigram_index_status("nonexistent_zone_xyz")
        assert status["status"] == "not_built"

    def test_invalidate_nonexistent_index(self):
        """Invalidating non-existent index should not crash."""
        from nexus.services.search_service import SearchService

        service = SearchService.__new__(SearchService)
        service._gw = None
        service._permission_enforcer = None

        service.invalidate_trigram_index("nonexistent_zone_xyz")
