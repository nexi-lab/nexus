"""Unit tests for glob pattern matching utilities.

Tests the path_matches_pattern function extracted from reactive_subscriptions.
"""

from nexus.core.glob_utils import path_matches_pattern


class TestPathMatchesPattern:
    """Tests for the path_matches_pattern function."""

    def test_simple_glob(self) -> None:
        assert path_matches_pattern("/workspace/main.py", "/workspace/*.py")

    def test_double_star(self) -> None:
        assert path_matches_pattern("/workspace/src/main.py", "/workspace/**/*.py")

    def test_no_match(self) -> None:
        assert not path_matches_pattern("/inbox/msg.txt", "/workspace/*.py")

    def test_question_mark(self) -> None:
        assert path_matches_pattern("/a/b.py", "/a/?.py")

    def test_fnmatch_simple(self) -> None:
        assert path_matches_pattern("/a/b.py", "/a/b.py")

    def test_double_star_matches_deep_paths(self) -> None:
        assert path_matches_pattern("/a/b/c/d/e.txt", "/a/**/*.txt")

    def test_double_star_root(self) -> None:
        assert path_matches_pattern("/a/b.py", "/**/*.py")

    def test_no_match_different_extension(self) -> None:
        assert not path_matches_pattern("/workspace/main.js", "/workspace/*.py")

    def test_star_matches_slash_in_fnmatch_mode(self) -> None:
        """fnmatch.fnmatch treats * as matching everything including /."""
        assert path_matches_pattern("/a/b/c.py", "/a/*.py")

    def test_invalid_regex_returns_false(self) -> None:
        """Invalid regex pattern returns False instead of raising."""
        # Patterns that compile fine as globs but not as regexes are handled
        assert not path_matches_pattern("/a/b.py", "**[invalid")
