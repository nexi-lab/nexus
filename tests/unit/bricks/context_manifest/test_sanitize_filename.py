"""Unit tests for _sanitize_filename (Issue #2130, #10A).

Covers:
- Unicode normalization (lookalike attack prevention)
- Path traversal rejection (../../etc/passwd)
- Empty name → "unnamed"
- Max length capping (>50 chars)
- Only alphanumeric + underscore + hyphen allowed
- Leading/trailing special characters stripped
"""

from __future__ import annotations

import pytest

from nexus.bricks.context_manifest.resolver import _sanitize_filename


@pytest.mark.parametrize(
    "input_name,expected",
    [
        # Normal names
        ("my_file", "my_file"),
        ("hello-world", "hello-world"),
        ("simple123", "simple123"),
        # Spaces and special chars → underscore
        ("my file.py", "my_file_py"),
        ("src/**/*.py", "src______py"),
        ("query with spaces", "query_with_spaces"),
        # Path traversal → sanitized (leading underscores stripped)
        ("../../etc/passwd", "etc_passwd"),
        ("..\\..\\windows", "windows"),
        # Empty → "unnamed"
        ("", "unnamed"),
        # Only special chars → "unnamed"
        ("...", "unnamed"),
        ("___", "unnamed"),
        # Leading/trailing underscores stripped
        ("_hello_", "hello"),
        ("__test__", "test"),
        # Hyphens preserved
        ("my-source-name", "my-source-name"),
    ],
    ids=[
        "normal_underscore",
        "normal_hyphen",
        "alphanumeric",
        "spaces_and_dots",
        "glob_pattern",
        "query_with_spaces",
        "unix_traversal",
        "windows_traversal",
        "empty",
        "only_dots",
        "only_underscores",
        "leading_trailing_underscore",
        "double_underscore",
        "hyphens",
    ],
)
def test_sanitize_filename(input_name: str, expected: str) -> None:
    assert _sanitize_filename(input_name) == expected


class TestMaxLength:
    def test_long_name_truncated_to_50(self) -> None:
        long_name = "a" * 100
        result = _sanitize_filename(long_name)
        assert len(result) == 50
        assert result == "a" * 50

    def test_exactly_50_chars_not_truncated(self) -> None:
        name = "a" * 50
        assert _sanitize_filename(name) == name


class TestUnicodeNormalization:
    def test_unicode_normalized_nfkc(self) -> None:
        # Full-width 'Ａ' (U+FF21) should normalize to 'A'
        result = _sanitize_filename("\uff21\uff22\uff23")
        assert result == "ABC"

    def test_cyrillic_a_treated_as_separate_char(self) -> None:
        # Cyrillic 'а' (U+0430) is alphanumeric, passes through after NFKC
        result = _sanitize_filename("\u0430\u0431\u0432")
        # These are valid alphanumeric chars in Unicode
        assert len(result) > 0
        assert all(c.isalnum() or c in ("_", "-") for c in result)
