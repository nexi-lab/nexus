"""Fast grep implementation using Rust acceleration.

This module provides a high-performance grep function that uses the Rust
nexus_fast library for regex matching, achieving 50-100x speedup over
the pure Python implementation.

Falls back to None if Rust extension is not available.
"""

from collections.abc import Callable
from typing import Any

# Try to import Rust extension
RUST_AVAILABLE = False
_rust_grep_bulk: Callable[..., list[dict[str, Any]]] | None = None

try:
    from nexus._nexus_fast import grep_bulk as _rust_grep_bulk  # type: ignore[no-redef]

    RUST_AVAILABLE = True
except ImportError:
    try:
        # Fallback to external nexus_fast package
        from nexus_fast import grep_bulk as _rust_grep_bulk  # type: ignore[no-redef]

        RUST_AVAILABLE = True
    except ImportError:
        pass


def grep_bulk(
    pattern: str,
    file_contents: dict[str, bytes],
    ignore_case: bool = False,
    max_results: int = 1000,
) -> list[dict[str, Any]] | None:
    """
    Fast bulk grep using Rust.

    Args:
        pattern: Regex pattern to search for
        file_contents: Dict mapping file paths to their content bytes
        ignore_case: Whether to ignore case in pattern matching
        max_results: Maximum number of results to return

    Returns:
        List of match dicts with keys: file, line, content, match
        Returns None if Rust extension is not available

    Each match dict contains:
        - file: File path
        - line: Line number (1-indexed)
        - content: Full line content
        - match: The matched text
    """
    if not RUST_AVAILABLE or _rust_grep_bulk is None:
        return None

    try:
        result: list[dict[str, Any]] = _rust_grep_bulk(
            pattern, file_contents, ignore_case, max_results
        )
        return result
    except Exception:
        # If Rust grep fails for any reason, return None to fallback to Python
        return None


def is_available() -> bool:
    """Check if Rust grep is available."""
    return RUST_AVAILABLE
