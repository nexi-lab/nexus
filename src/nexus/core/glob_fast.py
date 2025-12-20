"""Fast glob pattern matching using Rust acceleration.

This module provides high-performance glob matching functions that use the Rust
nexus_fast library for pattern matching, achieving 10-20x speedup over
the pure Python implementation using regex/fnmatch.

Falls back to Python fnmatch if Rust extension is not available.

Functions:
    glob_match_bulk: Match paths against multiple patterns (returns None if Rust unavailable)
    glob_match: Check if a single path matches any pattern (always returns a value)
    glob_filter: Filter paths by include/exclude patterns (always returns a value)
    is_available: Check if Rust acceleration is available
"""

import fnmatch
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
