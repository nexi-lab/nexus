"""Glob pattern helpers — pure-Python utilities used by the search tier.

These helpers complement the kernel-side `kernel.sys_glob` syscall and
the kernel-exported `glob_match_bulk` primitive: they handle the
query-side concerns (static-prefix extraction for directory pruning,
include/exclude filter composition, single-pattern matching) that
belong in the search brick rather than as kernel surface.

The Rust-accelerated primitives go through `nexus._rust_compat`:

    from nexus._rust_compat import glob_match_bulk

    matches = glob_match_bulk(["**/*.py"], paths)

These helpers wrap that primitive with the per-call shapes Search
internals need.
"""

from __future__ import annotations

import fnmatch
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# Glob special characters that indicate a non-static part of the pattern
_GLOB_SPECIAL_CHARS = re.compile(r"[*?\[\]{}]")


def _match_python(path: str, pattern: str) -> bool:
    path_for_match = path[1:] if path.startswith("/") else path
    pattern_for_match = pattern[1:] if pattern.startswith("/") else pattern
    return fnmatch.fnmatch(path_for_match, pattern_for_match)


def _brace_span(pattern: str) -> tuple[int, int] | None:
    in_class = False
    escaped = False
    open_idx: int | None = None

    for idx, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[":
            in_class = True
            continue
        if char == "]":
            in_class = False
            continue
        if in_class:
            continue
        if char == "{" and open_idx is None:
            open_idx = idx
        elif char == "}" and open_idx is not None:
            return open_idx, idx
    return None


def _split_brace_alternates(body: str) -> list[str]:
    parts: list[str] = []
    start = 0
    in_class = False
    escaped = False

    for idx, char in enumerate(body):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[":
            in_class = True
            continue
        if char == "]":
            in_class = False
            continue
        if char == "," and not in_class:
            parts.append(body[start:idx])
            start = idx + 1
    parts.append(body[start:])
    return parts


def _expand_brace_alternates(pattern: str) -> list[str]:
    span = _brace_span(pattern)
    if span is None:
        return [pattern]

    start, end = span
    choices = _split_brace_alternates(pattern[start + 1 : end])
    if len(choices) <= 1:
        return [pattern]

    prefix = pattern[:start]
    suffixes = _expand_brace_alternates(pattern[end + 1 :])
    return [f"{prefix}{choice}{suffix}" for choice in choices for suffix in suffixes]


def _glob_match_bulk_or_python(patterns: list[str], paths: list[str]) -> list[str]:
    from nexus._rust_compat import glob_match_bulk

    if glob_match_bulk is not None:
        return list(glob_match_bulk(patterns, paths))
    expanded_patterns = [p for pattern in patterns for p in _expand_brace_alternates(pattern)]
    return [
        path for path in paths if any(_match_python(path, pattern) for pattern in expanded_patterns)
    ]


def glob_match(path: str, patterns: list[str]) -> bool:
    """Check if a path matches any of the given glob patterns.

    Examples:
        >>> glob_match("/src/main.py", ["*.py", "*.txt"])
        True
        >>> glob_match("/README.md", ["*.py", "*.txt"])
        False
    """
    if not patterns:
        return False

    matches = _glob_match_bulk_or_python(patterns, [path])
    return len(matches) > 0


def glob_filter(
    paths: list[str],
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """Filter paths by include and exclude glob patterns.

    Examples:
        >>> glob_filter(["/a.py", "/b.txt", "/c.py"], include_patterns=["*.py"])
        ["/a.py", "/c.py"]
        >>> glob_filter(["/a.py", "/test_b.py"], exclude_patterns=["test_*"])
        ["/a.py"]
    """
    if not paths:
        return []

    result = paths

    if include_patterns:
        result = _glob_match_bulk_or_python(include_patterns, result)

    if exclude_patterns:
        excluded = set(_glob_match_bulk_or_python(exclude_patterns, result))
        result = [p for p in result if p not in excluded]

    return result


def extract_static_prefix(pattern: str) -> str:
    """Extract the static directory prefix from a glob pattern.

    Identifies the longest path prefix that contains no glob wildcards,
    enabling directory-level pruning during glob operations.

    Examples:
        >>> extract_static_prefix("src/components/**/*.tsx")
        "src/components/"
        >>> extract_static_prefix("**/*.py")
        ""
        >>> extract_static_prefix("/workspace/project/src/**/*.py")
        "/workspace/project/src/"
    """
    if not pattern:
        return ""

    segments = pattern.split("/")

    static_segments: list[str] = []
    for segment in segments:
        if _GLOB_SPECIAL_CHARS.search(segment):
            break
        static_segments.append(segment)

    if not static_segments:
        return ""

    if len(static_segments) == len(segments):
        if len(static_segments) > 1:
            static_segments = static_segments[:-1]
        else:
            return ""

    prefix = "/".join(static_segments)
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return prefix


def is_simple_pattern(pattern: str) -> bool:
    """Check if a glob pattern is simple (no ** recursive matching).

    Simple patterns can be matched efficiently with fnmatch without
    needing regex compilation (Issue #929).

    Examples:
        >>> is_simple_pattern("*.py")
        True
        >>> is_simple_pattern("**/*.py")
        False
    """
    return "**" not in pattern
