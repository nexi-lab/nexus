"""Glob pattern helpers — pure-Python utilities used by the search tier.

These helpers complement the kernel-side `kernel.sys_glob` syscall and
the kernel-exported `glob_match_bulk` primitive: they handle the
query-side concerns (static-prefix extraction for directory pruning,
include/exclude filter composition, single-pattern matching) that
belong in the search brick rather than as kernel surface.

The Rust-accelerated primitives go through `nexus_runtime` directly:

    from nexus._rust_compat import glob_match_bulk

    matches = glob_match_bulk(["**/*.py"], paths)

These helpers wrap that primitive with the per-call shapes Search
internals need.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


# Glob special characters that indicate a non-static part of the pattern
_GLOB_SPECIAL_CHARS = re.compile(r"[*?\[\]{}]")


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

    from nexus._rust_compat import glob_match_bulk

    matches = glob_match_bulk(patterns, [path])
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

    from nexus._rust_compat import glob_match_bulk

    result = paths

    if include_patterns:
        result = list(glob_match_bulk(include_patterns, result))

    if exclude_patterns:
        excluded = set(glob_match_bulk(exclude_patterns, result))
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
