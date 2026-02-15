"""Trigram index regression tests with golden corpus (Issue #954).

Verifies the superset invariant: trigram candidates must always be a
superset of brute-force grep matches. No false negatives allowed.
"""

from __future__ import annotations

import os
import re

import pytest

from nexus.core import trigram_fast

CORPUS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "fixtures", "trigram_corpus"
)


def _corpus_files() -> list[str]:
    """List all files in the test corpus."""
    return sorted(
        os.path.join(CORPUS_DIR, name)
        for name in os.listdir(CORPUS_DIR)
        if os.path.isfile(os.path.join(CORPUS_DIR, name))
    )


def _brute_force_grep(
    pattern: str,
    files: list[str],
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict]:
    """Pure-Python brute-force grep for comparison."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error:
        return []

    results = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, start=1):
                    if len(results) >= max_results:
                        return results
                    line = line.rstrip("\n")
                    m = regex.search(line)
                    if m:
                        results.append(
                            {
                                "file": fpath,
                                "line": line_num,
                                "content": line,
                                "match": m.group(0),
                            }
                        )
        except (OSError, UnicodeDecodeError):
            continue

    return results


@pytest.fixture(scope="module")
def module_index_path(tmp_path_factory):
    """Build a shared index for the whole module (avoids rebuilding per test)."""
    files = _corpus_files()
    idx_path = str(tmp_path_factory.mktemp("trigram") / "corpus.trgm")
    success = trigram_fast.build_index(files, idx_path)
    assert success, "Failed to build trigram index for corpus"
    return idx_path


# Test patterns: (pattern, ignore_case)
REGRESSION_PATTERNS = [
    # Literal patterns
    ("hello", False),
    ("Hello", False),
    ("world", False),
    ("search", False),
    ("def ", False),
    ("fn ", False),
    ("import", False),
    ("return", False),
    ("trigram", False),
    ("function", False),
    ("require", False),
    ("express", False),
    # Case-insensitive literals
    ("HELLO", True),
    ("Search", True),
    # Regex patterns
    (r"def \w+", False),
    (r"fn \w+", False),
    (r"hello.*world", False),
    (r"print\(", False),
    (r"\d+", False),
    # Patterns that should match nothing
    ("xyzzy_nonexistent", False),
    ("qqqqq12345", False),
]


class TestSupersetInvariant:
    """Verify trigram search always returns a superset of brute-force results."""

    @pytest.mark.parametrize("pattern,ignore_case", REGRESSION_PATTERNS)
    def test_superset_invariant(self, module_index_path, pattern, ignore_case):
        """Trigram matches must be a superset of brute-force matches.

        The trigram index may return false positives (candidates that don't
        actually match), but must never miss a true match (false negatives).
        """
        files = _corpus_files()

        # Brute-force: ground truth
        bf_results = _brute_force_grep(pattern, files, ignore_case=ignore_case)
        bf_files = {r["file"] for r in bf_results}

        # Trigram: should be superset
        tg_results = trigram_fast.grep(
            module_index_path,
            pattern,
            ignore_case=ignore_case,
            max_results=10000,
        )
        assert tg_results is not None, f"Trigram grep returned None for pattern: {pattern}"
        tg_files = {r["file"] for r in tg_results}

        # Superset invariant: every brute-force match must be in trigram results
        missing = bf_files - tg_files
        assert not missing, (
            f"False negatives for pattern '{pattern}': "
            f"brute-force found matches in {missing} but trigram did not"
        )


class TestResultConsistency:
    """Verify trigram results are consistent with brute-force results."""

    def test_literal_match_content(self, module_index_path):
        """Literal match content should be identical to brute-force."""
        files = _corpus_files()
        pattern = "hello_world"

        bf = _brute_force_grep(pattern, files)
        tg = trigram_fast.grep(module_index_path, pattern, max_results=10000)

        assert tg is not None
        # Same number of matches for literal patterns
        assert len(tg) == len(bf), (
            f"Match count differs: trigram={len(tg)}, brute-force={len(bf)}"
        )

    def test_empty_corpus_builds(self, tmp_path):
        """Building index from empty corpus should work."""
        idx_path = str(tmp_path / "empty.trgm")
        success = trigram_fast.build_index([], idx_path)
        assert success

        results = trigram_fast.grep(idx_path, "anything", max_results=100)
        assert results is not None
        assert len(results) == 0

    def test_stats_match_corpus(self, module_index_path):
        """Index stats should reflect corpus size."""
        stats = trigram_fast.get_stats(module_index_path)
        assert stats is not None

        # We have ~8 files in corpus (some may be filtered as binary)
        files = _corpus_files()
        assert stats["file_count"] <= len(files)
        assert stats["file_count"] > 0
