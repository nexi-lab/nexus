"""Fast glob pattern matching using Rust acceleration.

This module provides high-performance glob matching functions that use the Rust
nexus_fast library for pattern matching, achieving 10-20x speedup over
the pure Python implementation using regex/fnmatch.

Falls back to Python fnmatch if Rust extension is not available.

Functions:
    glob_match_bulk: Match paths against multiple patterns (returns None if Rust unavailable)
    glob_match: Check if a single path matches any pattern (always returns a value)
    glob_filter: Filter paths by include/exclude patterns (always returns a value)
    extract_static_prefix: Extract static directory prefix from glob pattern
    is_available: Check if Rust acceleration is available
"""

import fnmatch
import re
from collections.abc import Callable

# Try to import Rust extension
RUST_AVAILABLE = False
_rust_glob_match_bulk: Callable[[list[str], list[str]], list[str]] | None = None

try:
    from nexus_fast import glob_match_bulk as _rust_glob_match_bulk  # type: ignore[no-redef]

    RUST_AVAILABLE = True
except ImportError:
    pass


def glob_match_bulk(
    patterns: list[str],
    paths: list[str],
) -> list[str] | None:
    """
    Fast bulk glob pattern matching using Rust.

    Args:
        patterns: List of glob patterns to match (e.g., ["**/*.py", "*.txt"])
        paths: List of file paths to match against patterns

    Returns:
        List of paths that match any of the patterns (OR semantics)
        Returns None if Rust extension is not available

    Examples:
        >>> glob_match_bulk(["**/*.py"], ["/src/main.py", "/README.md"])
        ["/src/main.py"]

        >>> glob_match_bulk(["*.txt", "*.md"], ["/foo.txt", "/bar.py", "/baz.md"])
        ["/foo.txt", "/baz.md"]
    """
    if not RUST_AVAILABLE or _rust_glob_match_bulk is None:
        return None

    try:
        result: list[str] = _rust_glob_match_bulk(patterns, paths)
        return result
    except Exception:
        # If Rust glob fails for any reason, return None to fallback to Python
        return None


def is_available() -> bool:
    """Check if Rust glob is available."""
    return RUST_AVAILABLE


def glob_match(path: str, patterns: list[str]) -> bool:
    """
    Check if a path matches any of the given glob patterns.

    Uses Rust acceleration if available, falls back to Python fnmatch.

    Args:
        path: File path to check
        patterns: List of glob patterns to match against

    Returns:
        True if path matches any pattern, False otherwise

    Examples:
        >>> glob_match("/src/main.py", ["*.py", "*.txt"])
        True
        >>> glob_match("/README.md", ["*.py", "*.txt"])
        False
    """
    if not patterns:
        return False

    # Try Rust first
    if RUST_AVAILABLE and _rust_glob_match_bulk is not None:
        try:
            matches = _rust_glob_match_bulk(patterns, [path])
            return len(matches) > 0
        except Exception:
            pass  # Fall through to Python

    # Python fallback
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)


def glob_filter(
    paths: list[str],
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
) -> list[str]:
    """
    Filter paths by include and exclude glob patterns.

    Uses Rust acceleration if available, falls back to Python fnmatch.

    Args:
        paths: List of file paths to filter
        include_patterns: If provided, only paths matching ANY pattern are included
        exclude_patterns: If provided, paths matching ANY pattern are excluded

    Returns:
        Filtered list of paths

    Examples:
        >>> glob_filter(["/a.py", "/b.txt", "/c.py"], include_patterns=["*.py"])
        ["/a.py", "/c.py"]
        >>> glob_filter(["/a.py", "/test_b.py"], exclude_patterns=["test_*"])
        ["/a.py"]
    """
    if not paths:
        return []

    result = paths

    # Apply include filter
    if include_patterns:
        if RUST_AVAILABLE and _rust_glob_match_bulk is not None:
            try:
                result = list(_rust_glob_match_bulk(include_patterns, result))
            except Exception:
                # Python fallback for include
                result = [
                    p for p in result if any(fnmatch.fnmatch(p, pat) for pat in include_patterns)
                ]
        else:
            result = [p for p in result if any(fnmatch.fnmatch(p, pat) for pat in include_patterns)]

    # Apply exclude filter
    if exclude_patterns:
        if RUST_AVAILABLE and _rust_glob_match_bulk is not None:
            try:
                # Get paths that match exclude patterns
                excluded = set(_rust_glob_match_bulk(exclude_patterns, result))
                result = [p for p in result if p not in excluded]
            except Exception:
                # Python fallback for exclude
                result = [
                    p
                    for p in result
                    if not any(fnmatch.fnmatch(p, pat) for pat in exclude_patterns)
                ]
        else:
            result = [
                p for p in result if not any(fnmatch.fnmatch(p, pat) for pat in exclude_patterns)
            ]

    return result


# Glob special characters that indicate a non-static part of the pattern
_GLOB_SPECIAL_CHARS = re.compile(r"[*?\[\]{}]")


def extract_static_prefix(pattern: str) -> str:
    """
    Extract the static directory prefix from a glob pattern.

    This identifies the longest path prefix that contains no glob wildcards,
    enabling directory-level pruning during glob operations. For example,
    when searching for "src/components/**/*.tsx", we can limit the search
    to just the "src/components/" directory instead of the entire tree.

    Args:
        pattern: Glob pattern (e.g., "src/**/*.py", "lib/utils/*.ts")

    Returns:
        Static directory prefix with trailing slash, or empty string if no
        static prefix exists (e.g., pattern starts with wildcard)

    Examples:
        >>> extract_static_prefix("src/components/**/*.tsx")
        "src/components/"

        >>> extract_static_prefix("src/**/*.py")
        "src/"

        >>> extract_static_prefix("lib/utils/helpers.py")
        "lib/utils/"

        >>> extract_static_prefix("**/*.py")
        ""

        >>> extract_static_prefix("*.py")
        ""

        >>> extract_static_prefix("src/[ab]/*.py")
        "src/"

        >>> extract_static_prefix("/workspace/project/src/**/*.py")
        "/workspace/project/src/"
    """
    if not pattern:
        return ""

    # Split pattern into path segments
    segments = pattern.split("/")

    # Find the longest prefix of segments with no glob characters
    static_segments: list[str] = []

    for segment in segments:
        # Stop at the first segment containing glob special characters
        if _GLOB_SPECIAL_CHARS.search(segment):
            break
        static_segments.append(segment)

    if not static_segments:
        return ""

    # If all segments are static (no wildcards in pattern at all),
    # return the parent directory, not the full path.
    # This is because the last segment might be a file, not a directory.
    # e.g., "lib/utils/helpers.py" -> "lib/utils/"
    if len(static_segments) == len(segments):
        if len(static_segments) > 1:
            static_segments = static_segments[:-1]
        else:
            # Single segment with no wildcards (e.g., "file.py")
            # No directory to prune to
            return ""

    # Build the prefix path
    # Handle leading / for absolute paths
    prefix = "/".join(static_segments)

    # Add trailing slash to indicate directory (if we have any prefix)
    if prefix and not prefix.endswith("/"):
        prefix += "/"

    return prefix
