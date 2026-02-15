"""Tests for nexus.core.trigram_fast (Issue #954).

Tests the Python wrapper around the Rust trigram index, including
build, search, stats, and fallback behavior.
"""

from __future__ import annotations

import os

import pytest

from nexus.core import trigram_fast

# Path to golden test corpus
CORPUS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "fixtures", "trigram_corpus"
)


def _corpus_files() -> list[str]:
    """List all files in the test corpus."""
    files = []
    for name in os.listdir(CORPUS_DIR):
        path = os.path.join(CORPUS_DIR, name)
        if os.path.isfile(path):
            files.append(path)
    return sorted(files)


@pytest.fixture
def index_path(tmp_path):
    """Build a trigram index from the golden corpus and return the path."""
    files = _corpus_files()
    idx_path = str(tmp_path / "test.trgm")
    success = trigram_fast.build_index(files, idx_path)
    assert success, "Failed to build trigram index"
    return idx_path


class TestTrigramAvailability:
    """Test availability and import checks."""

    def test_is_available(self):
        """Rust extension should be available in test environment."""
        assert trigram_fast.is_available()

    def test_trigram_available_flag(self):
        assert trigram_fast.TRIGRAM_AVAILABLE is True


class TestBuildIndex:
    """Test trigram index building."""

    def test_build_trigram_index_from_files(self, tmp_path):
        """Happy path: build index from corpus files."""
        files = _corpus_files()
        idx_path = str(tmp_path / "test.trgm")
        success = trigram_fast.build_index(files, idx_path)
        assert success
        assert os.path.isfile(idx_path)
        assert os.path.getsize(idx_path) > 0

    def test_build_index_empty_list(self, tmp_path):
        """Building from empty file list should still succeed."""
        idx_path = str(tmp_path / "empty.trgm")
        success = trigram_fast.build_index([], idx_path)
        assert success
        assert os.path.isfile(idx_path)

    def test_build_index_nonexistent_files(self, tmp_path):
        """Non-existent files should be silently skipped."""
        idx_path = str(tmp_path / "partial.trgm")
        files = ["/nonexistent/file.txt"] + _corpus_files()
        success = trigram_fast.build_index(files, idx_path)
        assert success


class TestTrigramGrep:
    """Test trigram-based grep search."""

    def test_trigram_grep_literal(self, index_path):
        """Search for a literal pattern should return correct results."""
        results = trigram_fast.grep(index_path, "hello_world", max_results=100)
        assert results is not None
        assert len(results) > 0
        # Should find matches in hello.py and/or duplicate_content.py
        matched_files = {r["file"] for r in results}
        assert any("hello.py" in f for f in matched_files)

    def test_trigram_grep_regex(self, index_path):
        """Regex pattern search should work."""
        results = trigram_fast.grep(index_path, r"def \w+\(", max_results=100)
        assert results is not None
        assert len(results) > 0

    def test_trigram_grep_no_matches(self, index_path):
        """Pattern not in any file should return empty list."""
        results = trigram_fast.grep(
            index_path, "xyzzy_nonexistent_pattern_12345", max_results=100
        )
        assert results is not None
        assert len(results) == 0

    def test_trigram_grep_case_insensitive(self, index_path):
        """Case-insensitive search should find more matches."""
        case_results = trigram_fast.grep(
            index_path, "Hello", ignore_case=False, max_results=100
        )
        icase_results = trigram_fast.grep(
            index_path, "Hello", ignore_case=True, max_results=100
        )
        assert case_results is not None
        assert icase_results is not None
        assert len(icase_results) >= len(case_results)

    def test_trigram_grep_max_results(self, index_path):
        """Should respect max_results limit."""
        results = trigram_fast.grep(index_path, "e", max_results=3)
        assert results is not None
        assert len(results) <= 3

    def test_trigram_grep_invalid_index_path(self):
        """Invalid index path should return None (graceful failure)."""
        results = trigram_fast.grep("/nonexistent/index.trgm", "hello")
        assert results is None


class TestTrigramStats:
    """Test index statistics."""

    def test_get_stats(self, index_path):
        """Should return valid stats for a built index."""
        stats = trigram_fast.get_stats(index_path)
        assert stats is not None
        assert "file_count" in stats
        assert "trigram_count" in stats
        assert "index_size_bytes" in stats
        assert stats["file_count"] > 0
        assert stats["trigram_count"] > 0
        assert stats["index_size_bytes"] > 0

    def test_get_stats_invalid_path(self):
        """Invalid path should return None."""
        stats = trigram_fast.get_stats("/nonexistent/index.trgm")
        assert stats is None


class TestIndexPath:
    """Test index path helpers."""

    def test_get_index_path(self):
        """Should return valid path with .trgm extension."""
        path = trigram_fast.get_index_path("zone_abc")
        assert path.endswith("zone_abc.trgm")

    def test_get_index_path_custom_base(self, tmp_path):
        """Should use custom base directory."""
        path = trigram_fast.get_index_path("myzone", str(tmp_path))
        assert path.startswith(str(tmp_path))
        assert path.endswith("myzone.trgm")

    def test_index_exists_false(self):
        """Should return False for non-existent index."""
        assert not trigram_fast.index_exists("nonexistent_zone_12345")

    def test_index_exists_true(self, index_path):
        """Should return True after building index."""
        # index_path was built by fixture; check with explicit path
        zone_id = "test_zone"
        base_dir = os.path.dirname(index_path)
        # Build a properly named index
        files = _corpus_files()
        proper_path = trigram_fast.get_index_path(zone_id, base_dir)
        trigram_fast.build_index(files, proper_path)
        assert trigram_fast.index_exists(zone_id, base_dir)


class TestCacheInvalidation:
    """Test cache invalidation."""

    def test_invalidate_cache(self, index_path):
        """Invalidating cache should not crash."""
        trigram_fast.invalidate_cache(index_path)
        # After invalidation, grep should still work (re-opens index).
        results = trigram_fast.grep(index_path, "hello", max_results=10)
        assert results is not None
