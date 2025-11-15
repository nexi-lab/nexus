"""Fast glob pattern matching using Rust acceleration.

This module provides a high-performance glob matching function that uses the Rust
nexus_fast library for pattern matching, achieving 10-20x speedup over
the pure Python implementation using regex/fnmatch.

Falls back to None if Rust extension is not available.
"""

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
